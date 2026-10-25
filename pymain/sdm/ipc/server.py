"""IPC 服务器：行分隔 JSON over Unix socket（Windows 用 TCP 回环）。

对应 Swift 版 Daemon/IPC/SocketServer.swift。

**Swift v1.1.10 修过一个死锁**：原来用
`FileHandle.read(upToCount:)`，那个调用会一直等到读满请求的字节数
或对端关闭——客户端只发了一行就等回复，服务器在等更多字节，
两边互等。

Python 里用 `socket.makefile().readline()` 读行，天然按行边界，
没有这个问题。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import paths
from ..logging_setup import CAT_IPC, get_logger
from ..version import SOCKET_PORT_WINDOWS

log = get_logger()


class SocketServer:
    """行分隔 JSON 的 IPC 服务器。"""

    def __init__(
        self,
        path: str | None = None,
        port: int | None = None,
        handler: Callable[[str], str] | None = None,
    ) -> None:
        self.path = path or paths.socket_path()
        self.port = port
        self.handler = handler
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._is_windows = paths.is_windows()

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        """绑定并开始服务。阻塞在后台线程里。"""
        if self._is_windows:
            self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", self.port or SOCKET_PORT_WINDOWS))
        else:
            sock_path = Path(self.path).expanduser()
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            # 清掉上次没清干净的 socket，否则 bind 会失败
            try:
                sock_path.unlink()
            except OSError:
                pass
            self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server.bind(str(sock_path))
            os.chmod(sock_path, 0o600)

        self._server.listen(16)
        self._server.settimeout(0.5)
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="sdm-ipc", daemon=True)
        self._thread.start()
        if self._is_windows:
            endpoint = f"127.0.0.1:{self.port or SOCKET_PORT_WINDOWS}"
        else:
            endpoint = self.path
        log.info(CAT_IPC, f"IPC 服务器已启动: {endpoint}")

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self._is_windows:
            try:
                Path(self.path).expanduser().unlink()
            except OSError:
                pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client: socket.socket) -> None:
        try:
            with client, client.makefile("rwb") as stream:
                for raw_line in stream:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        response = self.handler(line) if self.handler else ""
                    except Exception as err:
                        response = json.dumps({"id": "?", "ok": False, "error": str(err)})
                    stream.write((response + "\n").encode("utf-8"))
                    stream.flush()
        except (OSError, ValueError):
            pass


class AsyncSocketServer:
    """asyncio 版的 IPC 服务器，供守护进程用。

    handler 可以是同步函数也可以是协程函数——守护进程的分发器
    要查数据库、跑异步控制，所以协程是必须的。
    """

    def __init__(
        self,
        path: str | None = None,
        port: int | None = None,
        handler: Callable[[str], Any] | None = None,
    ) -> None:
        self.path = path or paths.socket_path()
        self.port = port
        self.handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._is_windows = paths.is_windows()

    async def start(self) -> None:
        if self._is_windows:
            self._server = await asyncio.start_server(
                self._handle,
                "127.0.0.1",
                self.port or SOCKET_PORT_WINDOWS,
            )
        else:
            sock_path = Path(self.path).expanduser()
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                sock_path.unlink()
            except OSError:
                pass
            self._server = await asyncio.start_unix_server(self._handle, path=str(sock_path))
            os.chmod(sock_path, 0o600)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if not self._is_windows:
            try:
                Path(self.path).expanduser().unlink()
            except OSError:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    response = ""
                    if self.handler is not None:
                        result = self.handler(text)
                        if asyncio.iscoroutine(result):
                            result = await result
                        response = result
                except Exception as err:
                    response = json.dumps({"id": "?", "ok": False, "error": str(err)})
                writer.write((response + "\n").encode("utf-8"))
                await writer.drain()
        except (OSError, ConnectionResetError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
