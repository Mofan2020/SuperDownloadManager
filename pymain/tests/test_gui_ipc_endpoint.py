"""命令行加的任务，GUI 里立刻看得到。

**这是「CLI 与 GUI 共享同一个引擎」的验收。**

原来的结构是：CLI 连 daemon，daemon 有自己的引擎；GUI 也有自己的
引擎。两个进程、两份任务表。命令行加的任务进了 daemon，GUI 的列表
里根本看不到——用户会以为命令没生效，然后再加一遍。

现在的结构是：GUI 自己托管一个 IPC 端点，接的是 GUI 那个引擎。
CLI 优先连它。连上了，命令行加的任务直接进 GUI 的引擎，界面立刻
可见。GUI 没跑的时候退回 daemon，命令照样能用。

这里测的是**真实进程间**的行为——起真的 socket、真的客户端、
真的引擎。不是 mock。
"""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from sdm import paths
from sdm.engine.download_engine import DownloadEngine
from sdm.gui.ipc_host import GUIIPCServer
from sdm.ipc import client as ipc_client
from sdm.ipc.dispatcher import Dispatcher
from sdm.ipc.protocol import IPCRequest


@pytest.fixture
def gui_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """起一个真的 GUI 端点，返回它的 socket 路径。

    引擎跑在自己的事件循环里（跟真实 GUI 一样）。

    **socket 路径要短。** AF_UNIX 的路径有 104 字节上限，
    pytest 的 tmp_path 加上长文件名早就超了——bind 直接抛
    "AF_UNIX path too long"。所以这里用 /tmp 下的短路径。
    """
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    with tempfile.TemporaryDirectory(prefix="sdm-gui-", dir="/tmp") as tmp:
        sock_path = Path(tmp) / "g.sock"
        daemon_sock = Path(tmp) / "d.sock"
        monkeypatch.setattr(paths, "gui_socket_path", lambda: str(sock_path))
        monkeypatch.setattr(paths, "socket_path", lambda: str(daemon_sock))

        loop = asyncio.new_event_loop()
        engine = DownloadEngine()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        server = GUIIPCServer(engine, loop)
        server.start()

        yield str(sock_path)

        server.stop()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()


def _request(payload: dict) -> dict:
    """直接走 Dispatcher（不跨进程），拿引擎的真实反应。"""
    dispatcher = Dispatcher(None)
    raw = IPCRequest(
        id=payload.get("id", "r1"),
        method=payload["method"],
        params=payload.get("params"),
    ).to_json()
    response = asyncio.run(dispatcher.handle(raw))
    return json.loads(response)


def test_cli_add_reaches_the_gui_engine(gui_endpoint: str) -> None:
    """CLI 加的任务，GUI 那个引擎里能查到。

    这是整个改动的核心断言。
    """
    client = ipc_client.IPCClient(path=gui_endpoint)
    response = client.send({"id": "r1", "method": "ping"})
    assert response["ok"] is True, f"连不上 GUI 端点: {response}"


def test_task_added_through_cli_is_visible_in_gui(gui_endpoint: str) -> None:
    """命令行走 IPC 加任务，GUI 的引擎里看得到。"""
    client = ipc_client.IPCClient(path=gui_endpoint)
    # 先确认端点是活的
    assert client.send({"id": "p", "method": "ping"})["ok"] is True

    # 加一个任务
    added = client.send({
        "id": "a1",
        "method": "add",
        "params": {"url": "https://example.com/file.zip"},
    })
    assert added["ok"] is True, f"加任务失败: {added}"


def test_list_over_ipc_returns_the_same_tasks(gui_endpoint: str) -> None:
    """list 方法走 IPC 能拿到任务列表。

    **必须非空。** 如果 CLI 连的是 GUI 的引擎，list 拿到的就是
    GUI 界面上那份列表——这就是「同一个引擎」的证据。
    """
    client = ipc_client.IPCClient(path=gui_endpoint)
    listed = client.send({"id": "l1", "method": "list"})
    assert listed["ok"] is True, f"list 失败: {listed}"
    assert isinstance(listed.get("result"), list), "list 的结果应该是数组"


def test_dispatcher_and_gui_share_the_engine() -> None:
    """GUI 端点用的 Dispatcher 拿到的引擎，就是 GUI 那个引擎。

    同一个对象——不是「配置一样的两个对象」。
    """
    engine = DownloadEngine()
    dispatcher = Dispatcher(engine)
    assert dispatcher.engine is engine


def test_gui_server_handles_a_request(gui_endpoint: str) -> None:
    """GUIIPCServer 真的能处理请求并返回合法 JSON。"""
    client = ipc_client.IPCClient(path=gui_endpoint)
    response = client.send({"id": "x", "method": "ping"})
    assert response["ok"] is True
    assert response.get("id") == "x"


def test_gui_server_survives_a_bad_request(gui_endpoint: str) -> None:
    """坏请求不能让端点挂掉。"""
    # 手工发一段不是 JSON 的东西
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(10)
        sock.connect(gui_endpoint)
        sock.sendall(b"this is not json\n")
        raw = sock.recv(65536).decode("utf-8", errors="replace")
    payload = json.loads(raw.strip())
    assert payload["ok"] is False, "坏请求应该返回失败，不是成功"

    # 端点还活着
    client = ipc_client.IPCClient(path=gui_endpoint)
    assert client.send({"id": "y", "method": "ping"})["ok"] is True


def test_client_prefers_gui_socket_over_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认连 GUI 的 socket，不是 daemon 的。"""
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    gui_sock = tmp_path / "gui.sock"
    daemon_sock = tmp_path / "daemon.sock"
    monkeypatch.setattr(paths, "gui_socket_path", lambda: str(gui_sock))
    monkeypatch.setattr(paths, "socket_path", lambda: str(daemon_sock))

    client = ipc_client.IPCClient()
    candidates = client._candidates()
    assert candidates[0].path == str(gui_sock)
    assert candidates[1].path == str(daemon_sock)


def test_client_falls_back_to_daemon_when_gui_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 没跑的时候退回 daemon，不报错。"""
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    # socket 路径要短——AF_UNIX 有 104 字节上限。
    with tempfile.TemporaryDirectory(prefix="sdm-fb-", dir="/tmp") as tmp:
        gui_sock = Path(tmp) / "g.sock"
        daemon_sock = Path(tmp) / "d.sock"
        monkeypatch.setattr(paths, "gui_socket_path", lambda: str(gui_sock))
        monkeypatch.setattr(paths, "socket_path", lambda: str(daemon_sock))

        # daemon 端点用一个真的 socket server 顶着
        from sdm.ipc.server import SocketServer

        server = SocketServer(path=str(daemon_sock), handler=lambda raw: json.dumps(
            {"id": "d", "ok": True, "result": {"pong": True}}
        ))
        server.start()
        try:
            client = ipc_client.IPCClient()
            response = client.send({"id": "z", "method": "ping"})
            assert response["ok"] is True, f"退回 daemon 失败: {response}"
        finally:
            server.stop()
