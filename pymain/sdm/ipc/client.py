"""IPC 客户端：连到守护进程发一条请求。

对应 Swift 版 CLI/SDMDM.swift 里的 UDSClient。

Windows 没有 Unix socket，用 TCP 回环端口。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .. import paths
from ..logging_setup import CAT_IPC, get_logger

log = get_logger()


class ClientError(Exception):
    pass


class IPCClient:
    """IPC 客户端。一次调用 = 一次请求 + 一次响应。"""

    def __init__(self, path: str | None = None, port: int | None = None, timeout: float = 30.0) -> None:
        self.path = path or paths.socket_path()
        self.port = port
        self.timeout = timeout
        self._is_windows = paths.is_windows()

    def send(self, payload: dict) -> dict:
        """发一个请求，返回解析后的响应字典。"""
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        data = self._exchange(raw)
        try:
            return json.loads(data)
        except json.JSONDecodeError as err:
            raise ClientError(f"响应解析失败: {err}") from err

    def _exchange(self, payload: bytes) -> str:
        if self._is_windows:
            return self._exchange_tcp(payload)
        return self._exchange_unix(payload)

    def _exchange_unix(self, payload: bytes) -> str:
        sock_path = Path(self.path).expanduser()
        if not sock_path.exists():
            raise ClientError(f"守护进程不可达（{self.path} 不存在），sdmd 在运行吗？")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(sock_path))
            sock.sendall(payload + b"\n")
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            if not buffer:
                raise ClientError("守护进程没有响应")
            return buffer.decode("utf-8").strip()
        except socket.timeout as err:
            raise ClientError("守护进程超时未响应") from err
        except OSError as err:
            raise ClientError(f"连接守护进程失败: {err}") from err
        finally:
            sock.close()

    def _exchange_tcp(self, payload: bytes) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(("127.0.0.1", self.port or paths.SOCKET_PORT_WINDOWS))
            sock.sendall(payload + b"\n")
            buffer = b""
            while not buffer.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            if not buffer:
                raise ClientError("守护进程没有响应")
            return buffer.decode("utf-8").strip()
        except socket.timeout as err:
            raise ClientError("守护进程超时未响应") from err
        except OSError as err:
            raise ClientError(f"连接守护进程失败: {err}") from err
        finally:
            sock.close()
