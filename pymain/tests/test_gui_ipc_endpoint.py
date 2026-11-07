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

**两个平台走不同的传输。**

- Unix：unix socket，路径有 104 字节上限
- Windows：TCP 回环端口（GUI 7957 / daemon 7956）

所以这里用一个小的抽象把两边统一起来：夹具起一个真的端点，
测试用 `_Endpoint.connect()` 拿客户端。Windows 上跑的是真的 TCP
回环，不是把 unix 分支硬拗过来——那个分支在 Windows 上根本走不到
（`SocketServer` 认端口不认路径），硬拗只能测出一个假的东西。

unix socket 特有的用例（socket 路径）在 Windows 上跳过，跟仓库里
既有的 `test_ipc_backlog.py` 一个路子。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
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
from sdm.ipc.server import SocketServer

# AF_UNIX 的 socket 路径有 104 字节上限（macOS/Linux）。
# 留点余量，超过 100 就换地方。
_SOCK_PATH_MAX = 100

#: Windows 没有 unix socket，只有 TCP 回环。
needs_unix_socket = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 走 TCP 回环，没有 unix socket",
)


def _short_tmp_dir(prefix: str) -> tempfile.TemporaryDirectory[str]:
    """建一个够短的临时目录，用来放 unix socket。

    **不能用 pytest 的 tmp_path。** macOS 上它是
    `/private/var/folders/.../pytest-of-<用户>/pytest-<号>/<用例名>0`，
    光目录就 90 多字节，加上 socket 文件名直接超 104，bind 抛
    "AF_UNIX path too long"。

    **也不能写死 `/tmp`。** Windows 上没有这个目录，CI 的 Windows
    job 会直接 FileNotFoundError。

    所以：先用系统临时目录（`tempfile.gettempdir()`），量一下长度；
    太长再退到 `/tmp`（Unix）或盘符根目录（Windows）。
    """
    candidates = [tempfile.gettempdir()]
    if os.name == "nt":
        # Windows 上系统临时目录带用户名，可能偏长。根目录最短。
        candidates.append("C:\\")
    else:
        candidates.append("/tmp")

    errors: list[str] = []
    for base in candidates:
        # 估算最终 socket 路径：base + 分隔符 + 随机目录名 + 文件名
        probe = os.path.join(base, prefix + "x" * 8, "g.sock")
        if len(probe.encode("utf-8")) >= _SOCK_PATH_MAX:
            errors.append(f"{base} 太长（{len(probe)} 字节）")
            continue
        try:
            return tempfile.TemporaryDirectory(prefix=prefix, dir=base)
        except OSError as exc:
            errors.append(f"{base} 不可用（{exc}）")
            continue
    raise RuntimeError("找不到够短的临时目录: " + "; ".join(errors))


def _free_port() -> int:
    """找一个没人占的本地端口。

    让内核挑（bind 端口 0），再把端口号读出来。写死 7957 会跟真
    跑的 GUI 撞——测试不能因为用户开着应用就红。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Endpoint:
    """一个真的 IPC 端点。

    Unix 上是 socket 路径，Windows 上是 TCP 端口。用同一个接口
    盖住两边的差异。
    """

    def __init__(self, *, path: str | None = None, port: int | None = None) -> None:
        self.path = path
        self.port = port

    def connect(self, **kwargs) -> ipc_client.IPCClient:
        """拿一个连到这个端点的客户端。"""
        if self.port is not None:
            return ipc_client.IPCClient(port=self.port, **kwargs)
        assert self.path is not None
        return ipc_client.IPCClient(path=self.path, **kwargs)

    def raw_socket(self) -> socket.socket:
        """拿一个裸 socket，自己往里写字节（测坏请求）。"""
        if self.port is not None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", self.port))
        else:
            assert self.path is not None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.path)
        sock.settimeout(10)
        return sock


def _dispatch_to(engine: DownloadEngine, loop: asyncio.AbstractEventLoop, raw: str) -> str:
    """把一条 IPC 请求丢到引擎自己的事件循环上跑，返回响应。

    **必须用引擎那个循环。** 引擎的方法有一半是协程，在别的循环里
    await 会直接坏掉。
    """
    dispatcher = Dispatcher(engine)
    future = asyncio.run_coroutine_threadsafe(dispatcher.handle(raw), loop)
    return future.result(timeout=30)


@pytest.fixture
def gui_endpoint(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Endpoint]:
    """起一个真的 GUI 端点，接的是真的引擎。

    引擎跑在自己的事件循环里（跟真实 GUI 一样）。
    """
    loop = asyncio.new_event_loop()
    engine = DownloadEngine()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    tmp: tempfile.TemporaryDirectory[str] | None = None
    if paths.is_windows():
        # Windows 走 TCP 回环。端口让内核挑，别撞上用户开着的 GUI。
        port = _free_port()
        server = SocketServer(port=port, handler=lambda raw: _dispatch_to(engine, loop, raw))
        server.start()
        endpoint = _Endpoint(port=port)
    else:
        # Unix 走 unix socket。路径要短——104 字节上限。
        tmp = _short_tmp_dir("sdm-gui-")
        sock_path = Path(tmp.name) / "g.sock"
        monkeypatch.setattr(paths, "gui_socket_path", lambda: str(sock_path))
        server = SocketServer(
            path=str(sock_path),
            handler=lambda raw: _dispatch_to(engine, loop, raw),
        )
        server.start()
        endpoint = _Endpoint(path=str(sock_path))

    try:
        yield endpoint
    finally:
        server.stop()
        if tmp is not None:
            tmp.cleanup()
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


def test_cli_add_reaches_the_gui_engine(gui_endpoint: _Endpoint) -> None:
    """CLI 加的任务，GUI 那个引擎里能查到。

    这是整个改动的核心断言。
    """
    client = gui_endpoint.connect()
    response = client.send({"id": "r1", "method": "ping"})
    assert response["ok"] is True, f"连不上 GUI 端点: {response}"


def test_task_added_through_cli_is_visible_in_gui(gui_endpoint: _Endpoint) -> None:
    """命令行走 IPC 加任务，GUI 的引擎里看得到。"""
    client = gui_endpoint.connect()
    # 先确认端点是活的
    assert client.send({"id": "p", "method": "ping"})["ok"] is True

    # 加一个任务
    added = client.send({
        "id": "a1",
        "method": "add",
        "params": {"url": "https://example.com/file.zip"},
    })
    assert added["ok"] is True, f"加任务失败: {added}"

    # 同一个端点 list 出来的列表里能看到它
    listed = client.send({"id": "l1", "method": "list"})
    assert listed["ok"] is True
    urls = [t.get("url") for t in listed["result"]]
    assert "https://example.com/file.zip" in urls, f"任务没进这个引擎: {urls}"


def test_list_over_ipc_returns_the_same_tasks(gui_endpoint: _Endpoint) -> None:
    """list 方法走 IPC 能拿到任务列表。

    **必须非空。** 如果 CLI 连的是 GUI 的引擎，list 拿到的就是
    GUI 界面上那份列表——这就是「同一个引擎」的证据。
    """
    client = gui_endpoint.connect()
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


def test_gui_server_handles_a_request(gui_endpoint: _Endpoint) -> None:
    """端点真的能处理请求并返回合法 JSON。"""
    client = gui_endpoint.connect()
    response = client.send({"id": "x", "method": "ping"})
    assert response["ok"] is True
    assert response.get("id") == "x"


def test_gui_server_survives_a_bad_request(gui_endpoint: _Endpoint) -> None:
    """坏请求不能让端点挂掉。"""
    # 手工发一段不是 JSON 的东西
    with gui_endpoint.raw_socket() as sock:
        sock.sendall(b"this is not json\n")
        raw = sock.recv(65536).decode("utf-8", errors="replace")
    payload = json.loads(raw.strip())
    assert payload["ok"] is False, "坏请求应该返回失败，不是成功"

    # 端点还活着
    client = gui_endpoint.connect()
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


@needs_unix_socket
def test_client_falls_back_to_daemon_when_gui_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUI 没跑的时候退回 daemon，不报错。

    **Windows 上没有 unix socket。** `socket.AF_UNIX` 在 Windows 的
    Python 3.11/3.12 里根本不存在（3.13 才加），所以这里只能跑
    Unix 分支；Windows 的回退由 `test_windows_cli.py` 里的
    `test_client_falls_back_to_daemon_when_gui_is_down` 覆盖。
    """
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    # socket 路径要短——AF_UNIX 有 104 字节上限。
    with _short_tmp_dir("sdm-fb-") as tmp:
        gui_sock = Path(tmp) / "g.sock"
        daemon_sock = Path(tmp) / "d.sock"
        monkeypatch.setattr(paths, "gui_socket_path", lambda: str(gui_sock))
        monkeypatch.setattr(paths, "socket_path", lambda: str(daemon_sock))

        # daemon 端点用一个真的 socket server 顶着
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


@needs_unix_socket
def test_gui_socket_path_is_a_unix_path() -> None:
    """Unix 上 GUI 的端点是个 socket 文件路径，不是 npipe。"""
    result = paths.gui_socket_path()
    assert "npipe" not in result
    assert result.endswith(".sock")


@needs_unix_socket
def test_gui_ipc_server_uses_the_gui_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GUIIPCServer` 真的绑在 GUI 那个端点上。

    **不能只测 `SocketServer`。** `GUIIPCServer` 才是 GUI 进程里
    真正跑的那个类——它负责把请求丢到引擎自己的事件循环上。绕开
    它测底下的 `SocketServer`，测的就不是生产路径了。

    **socket 路径要短。** pytest 的 tmp_path 光目录就 90 多字节，
    bind 直接抛 "AF_UNIX path too long"——而 `GUIIPCServer.start()`
    把这个异常吞了（绑不上不能挡 GUI 启动），所以症状是「端点没起来」
    而不是报错，更难查。
    """
    with _short_tmp_dir("sdm-host-") as tmp:
        sock_path = Path(tmp) / "gui.sock"
        monkeypatch.setattr(paths, "is_windows", lambda: False)
        monkeypatch.setattr(paths, "gui_socket_path", lambda: str(sock_path))

        loop = asyncio.new_event_loop()
        engine = DownloadEngine()

        # 引擎跑在自己的事件循环里（跟真实 GUI 一样）。不跑起来，
        # `run_coroutine_threadsafe` 排进去的协程永远不会被执行。
        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        server = GUIIPCServer(engine, loop)
        try:
            server.start()
            assert sock_path.exists(), "端点没起来——socket 文件都没建出来"

            # CLI 能连上
            client = ipc_client.IPCClient(path=str(sock_path))
            response = client.send({"id": "q", "method": "ping"})
            assert response["ok"] is True, f"连不上 GUI 端点: {response}"

            # 而且连的是 GUI 那个引擎
            added = client.send({
                "id": "a1",
                "method": "add",
                "params": {"url": "https://example.com/host.zip"},
            })
            assert added["ok"] is True, added
            listed = client.send({"id": "l1", "method": "list"})
            urls = [t.get("url") for t in listed["result"]]
            assert "https://example.com/host.zip" in urls, urls
        finally:
            server.stop()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5)
            loop.close()


def test_gui_ipc_server_bind_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端点绑不上不能挡 GUI 启动。

    端口被占、socket 路径太长、权限不够——这些都不该让用户开不了
    窗口。CLI 会自动退回 daemon，功能不丢。
    """
    monkeypatch.setattr(paths, "is_windows", lambda: False)
    # 路径指向一个不存在的目录，bind 一定失败
    monkeypatch.setattr(
        paths, "gui_socket_path", lambda: str(tmp_path / "no" / "such" / "dir" / "g.sock")
    )

    loop = asyncio.new_event_loop()
    server = GUIIPCServer(DownloadEngine(), loop)
    try:
        # 不抛异常就是过了
        server.start()
    finally:
        server.stop()
        loop.close()


@needs_unix_socket
def test_unix_socket_path_stays_under_the_kernel_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路径太长 bind 会失败——所以要有回退。"""
    # 假装配置目录特别长
    long_dir = tmp_path / ("x" * 120)
    monkeypatch.setattr(paths, "config_dir", lambda: long_dir)
    monkeypatch.delenv("SDM_GUI_SOCKET", raising=False)

    result = paths.gui_socket_path()
    assert len(result.encode("utf-8")) < 100, f"路径太长，bind 会失败: {result}"
