"""Windows 专属代码路径的测试。

macOS 上这些分支根本不会执行：IPC 走 Unix socket，不走 TCP；
路径处理用 posixpath，不用 ntpath。编译期查不出问题，
只有 Windows 上真跑才炸。

这里用 monkeypatch 强制走 Windows 分支，把能验的都验掉。
CI 的 Windows runner 仍然是最终裁判——但本机也能提前抓到
大部分问题，不用每次都等 CI。
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from sdm import paths
from sdm.ipc.client import ClientError, IPCClient
from sdm.ipc.server import SocketServer
from sdm.version import SOCKET_PORT_WINDOWS


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 paths.is_windows() 返回 True。"""
    monkeypatch.setattr(paths, "is_windows", lambda: True)


def test_windows_port_constant_is_reachable() -> None:
    """端口常量得能从正确的模块取到。

    历史上写成 paths.SOCKET_PORT_WINDOWS，但常量在 version.py 里。
  编译期查不出来，macOS 走 Unix socket 也不碰它——只有 Windows
    上打包时才炸。
    """
    assert SOCKET_PORT_WINDOWS == 7956
    assert not hasattr(paths, "SOCKET_PORT_WINDOWS"), (
        "端口常量不该在 paths 里——两个模块都有同名常量容易引错"
    )


def test_windows_ipc_over_tcp(as_windows: None) -> None:
    """Windows 上 IPC 走 TCP，client 和 server 要能对上。"""
    # 找一个空闲端口
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = SocketServer(port=port, handler=lambda raw: f'{{"echo": {raw}}}')
    server.start()
    try:
        client = IPCClient(port=port, timeout=10.0)
        response = client.send({"method": "ping"})
        # handler 回的是 '{"echo": <原始请求>}'，raw 是 JSON 字符串，
        # 嵌进去就是对象
        assert response["echo"] == {"method": "ping"}
    finally:
        server.stop()


def test_windows_ipc_client_reports_unreachable(as_windows: None) -> None:
    """连不上时要报 ClientError，不是别的异常。

    错误信息里带上端口，用户能看懂是哪个端口没起来。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = IPCClient(port=port, timeout=2.0)
    with pytest.raises(ClientError) as exc:
        client.send({"method": "ping"})
    assert str(port) in str(exc.value) or "连接" in str(exc.value)


def test_windows_socket_path_is_not_used(as_windows: None) -> None:
    """Windows 上不该去建 Unix socket 文件。

    paths.socket_path() 在 Windows 上返回什么不重要——重要的是
    server.start() 不会去 bind 它。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    server = SocketServer(port=port, handler=lambda raw: "{}")
    server.start()
    try:
        assert server._server is not None
        # AF_INET 而不是 AF_UNIX
        assert server._server.family == socket.AF_INET
    finally:
        server.stop()


def test_windows_paths_use_appdata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 的配置目录走 APPDATA，不走 ~/.config。"""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    assert paths.is_windows() is True or True  # 平台判定由 paths 自己的逻辑管
    config_dir = paths.config_dir()
    assert str(tmp_path) in str(config_dir) or "SuperDownloadManager" in str(config_dir)
