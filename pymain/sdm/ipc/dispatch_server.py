"""浏览器扩展 dispatch 服务。

对应 Swift 版 Engine/DispatchServer.swift。

Firefox / Safari 扩展把下载链接 POST/GET 到
`http://127.0.0.1:7955/add?url=...&filename=...&referer=...`，
由这里交给引擎排队。

只绑回环地址。
"""

from __future__ import annotations

import json
import socket
import threading
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from ..logging_setup import CAT_NET, get_logger

log = get_logger()

DISPATCH_PORT = 7955


class DispatchServer:
    """给浏览器扩展用的回环 HTTP 服务。"""

    def __init__(
        self,
        port: int = DISPATCH_PORT,
        on_add: Callable[[str, str | None, str | None], None] | None = None,
    ) -> None:
        self.port = port
        self.on_add = on_add
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._server is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", self.port))
        except OSError as err:
            server.close()
            raise OSError(f"扩展 dispatch 端口 {self.port} 绑定失败: {err}") from err
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="sdm-dispatch", daemon=True)
        self._thread.start()
        log.info(CAT_NET, f"扩展 dispatch 服务已启动: 127.0.0.1:{self.port}")

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

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            buffer = b""
            while b"\r\n\r\n" not in buffer and len(buffer) < 64 * 1024:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            response = self._respond(buffer.decode("utf-8", errors="replace"))
            client.sendall(response)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _respond(self, request: str) -> bytes:
        first_line = request.split("\n", 1)[0].strip()
        parts = first_line.split(" ")
        if len(parts) < 2:
            return self._http(400, '{"ok":false,"error":"bad request"}')

        target = parts[1]
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/ping", "/ping/"):
            return self._http(200, '{"ok":true,"result":{"pong":true}}')

        if path in ("/add", "/add/"):
            url = (query.get("url") or [""])[0]
            if not url:
                return self._http(400, '{"ok":false,"error":"missing url"}')
            filename = (query.get("filename") or [""])[0] or None
            referer = (query.get("referer") or [""])[0] or None
            if self.on_add is not None:
                try:
                    self.on_add(url, filename, referer)
                except Exception as err:  # noqa: BLE001
                    log.error(CAT_NET, f"扩展派单失败: {err}")
                    return self._http(500, '{"ok":false,"error":"dispatch failed"}')
            body = json.dumps(
                {"ok": True, "result": {"accepted": True, "url": url}},
                ensure_ascii=False,
            )
            return self._http(200, body)

        return self._http(404, '{"ok":false,"error":"not found"}')

    @staticmethod
    def _http(code: int, body: str) -> bytes:
        reason = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}.get(code, "OK")
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            f"Connection: close\r\n"
            f"Access-Control-Allow-Origin: *\r\n"
            f"\r\n"
        ).encode("ascii")
        return head + payload
