"""IPC 服务器测试：backlog（F167）与协议基本行为。

F167：Swift 版 `Daemon/IPC/SocketServer.swift:65` 用
`Darwin.listen(fd, 16)`，backlog 是 16。Python 版之前同步路径
写了 16，但生产走的异步路径没设，用的是 asyncio 默认的 100。
这个测试锁住两条路径都是 16。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.ipc import server as ipc_server


def test_backlog_constant_matches_swift():
    """backlog 常量是 16，跟 Swift 的 Darwin.listen(fd, 16) 一致。"""
    assert ipc_server.BACKLOG == 16


def test_sync_server_uses_backlog_constant(monkeypatch, tmp_path):
    """同步 SocketServer 的 listen 用的是常量，不是写死的数字。"""
    recorded: list[int] = []

    class _FakeSocket:
        def __init__(self, *args, **kwargs):
            pass

        def bind(self, path):
            # 真的建出 socket 文件——start() 后面要 chmod 它。
            Path(path).touch()

        def listen(self, backlog):
            recorded.append(backlog)

        def settimeout(self, value):
            pass

        def accept(self):
            # 后台线程会调它。抛异常让它退出，别在测试输出里
            # 留未处理的线程异常。
            raise OSError("测试用 fake，停止接受连接")

        def close(self):
            pass

    monkeypatch.setattr(ipc_server.socket, "socket", lambda *a, **k: _FakeSocket())
    monkeypatch.setattr(ipc_server, "SOCKET_PORT_WINDOWS", 7955)
    # 强制走 unix 分支——is_windows 在 __init__ 里就判定了，
    # 必须在构造前 patch。
    monkeypatch.setattr(ipc_server.paths, "is_windows", lambda: False)

    sock_path = tmp_path / "sdm-test.sock"
    srv = ipc_server.SocketServer(str(sock_path))
    try:
        srv.start()
    except Exception:
        # 线程启动失败不影响断言——我们要看的是 listen 的参数
        pass
    finally:
        try:
            srv.stop()
        except Exception:
            pass

    assert recorded == [ipc_server.BACKLOG]


def test_async_server_uses_backlog_constant(monkeypatch, tmp_path):
    """异步 AsyncSocketServer 的 listen 也用 BACKLOG。

    这是生产路径（daemon 走这个）。之前没设 backlog，用的是
    asyncio 默认的 100，跟 Swift 不一致。
    """
    recorded: dict[str, int] = {}

    class _FakeServer:
        def __await__(self):
            async def _noop():
                return self
            return _noop().__await__()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    def fake_start_server(handler, host, port, **kwargs):
        recorded["tcp"] = kwargs.get("backlog")
        return _FakeServer()

    def fake_start_unix_server(handler, path=None, **kwargs):
        recorded["unix"] = kwargs.get("backlog")
        # 真的建出 socket 文件——start() 后面要对它 chmod。
        Path(path).touch()
        return _FakeServer()

    monkeypatch.setattr(ipc_server.asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(
        ipc_server.asyncio, "start_unix_server", fake_start_unix_server
    )
    # 强制走 unix 分支。必须在构造前 patch——AsyncSocketServer
    # 在 __init__ 里就把 is_windows() 的结果存下来了。
    monkeypatch.setattr(ipc_server.paths, "is_windows", lambda: False)

    sock_path = tmp_path / "sdm-test-async.sock"
    srv = ipc_server.AsyncSocketServer(str(sock_path))

    async def _run():
        await srv.start()

    asyncio.run(_run())

    assert recorded.get("unix") == ipc_server.BACKLOG


def test_async_server_tcp_uses_backlog_constant(monkeypatch):
    """Windows 分支（TCP 回环）也要用 BACKLOG。"""
    recorded: dict[str, int] = {}

    class _FakeServer:
        def __await__(self):
            async def _noop():
                return self
            return _noop().__await__()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    def fake_start_server(handler, host, port, **kwargs):
        recorded["tcp"] = kwargs.get("backlog")
        return _FakeServer()

    monkeypatch.setattr(ipc_server.asyncio, "start_server", fake_start_server)
    monkeypatch.setattr(ipc_server.paths, "is_windows", lambda: True)

    srv = ipc_server.AsyncSocketServer(port=7955)

    async def _run():
        await srv.start()

    asyncio.run(_run())

    assert recorded.get("tcp") == ipc_server.BACKLOG
