"""IPC 客户端：连到守护进程发一条请求。

对应 Swift 版 CLI/SDMDM.swift 里的 UDSClient。

Windows 没有 Unix socket，用 TCP 回环端口。
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from .. import paths
from ..logging_setup import get_logger
from ..version import SOCKET_PORT_WINDOWS

log = get_logger()

#: 响应大小上限。1 MB。
#:
#: 守护进程是本机进程，正常响应只有几 KB。但 socket 另一端要是
#: 出了问题（半个 daemon、别的进程占了这个 socket、恶意进程
#: 抢占路径），没有上限的话客户端会一直往内存里灌，直到把机器
#: 的内存吃光。Swift 版 CLI/SDMDM.swift:406 也是这个上限。
MAX_RESPONSE_BYTES = 1024 * 1024


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
            return self._read_response(sock)
        except TimeoutError as err:
            raise ClientError("守护进程超时未响应") from err
        except OSError as err:
            raise ClientError(f"连接守护进程失败: {err}") from err
        finally:
            sock.close()

    def _exchange_tcp(self, payload: bytes) -> str:
        target = ("127.0.0.1", self.port or SOCKET_PORT_WINDOWS)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 建连用短超时，守护进程没跑时要立刻报，而不是干等
        # 30 秒。Windows 上连没人监听的端口不一定会立刻拒绝，
        # 有防火墙时会一路等到超时。
        sock.settimeout(min(self.timeout, 3.0))
        try:
            try:
                sock.connect(target)
            except TimeoutError as err:
                raise ClientError(
                    f"连接守护进程超时（127.0.0.1:{target[1]}）：sdmd 在运行吗？"
                ) from err
            # 连上了，接下来是正常请求，用完整超时
            sock.settimeout(self.timeout)
            sock.sendall(payload + b"\n")
            return self._read_response(sock)
        except TimeoutError as err:
            raise ClientError("守护进程超时未响应") from err
        except OSError as err:
            raise ClientError(f"连接守护进程失败: {err}") from err
        finally:
            sock.close()

    @staticmethod
    def _read_response(sock: socket.socket) -> str:
        """读到换行为止，返回响应正文。

        超过 1 MB 直接报错——不静默截断。截断出来的 JSON 解析会
        报一个跟真实原因无关的错（"响应解析失败"），排查时容易
        被带偏。
        """
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > MAX_RESPONSE_BYTES:
                raise ClientError(
                    f"守护进程响应超过上限（{MAX_RESPONSE_BYTES // 1024} KB）"
                )
        if not buffer:
            raise ClientError("守护进程没有响应")
        return buffer.decode("utf-8").strip()
