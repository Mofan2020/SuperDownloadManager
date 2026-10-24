"""本地 Range 服务器：测试用。

在本地起一个支持 Range 的 HTTP 服务器，给引擎测试当下载源。
和 Swift 版测试里内嵌的 Python 脚本是同一个东西，这里提成模块
方便 pytest 复用。
"""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import tempfile
import threading
import time
from pathlib import Path


class RangeFileHandler(http.server.BaseHTTPRequestHandler):
    """支持 Range 的文件服务器。"""

    #: HTTP/1.1。BaseHTTPRequestHandler 默认是 1.0，而 1.0 不支持
    #: keep-alive——每个响应后服务器就关连接。httpx 会以为连接还能用，
    #: 于是出现「peer closed connection without sending complete
    #: message body」这种假故障，重试一轮又一轮。
    #: 真实服务器都是 1.1，这里对齐。
    protocol_version = "HTTP/1.1"

    def end_headers(self) -> None:
        """每个响应都声明 Connection: close。

        原因：keep-alive 下一条连接会被复用，而测试里多个分片
        抢同一条连接时，httpx 的连接池和服务器的空闲线程
        会互相等待——表现就是「所有响应都发完了，客户端还在
        select 上等」。显式声明关闭，语义最清楚。
        """
        self.send_header("Connection", "close")
        self.close_connection = True
        super().end_headers()

    payload_path: Path = Path()
    total_bytes: int = 0
    #: 每个 4KB 块之间的延迟，用来模拟慢速连接
    chunk_delay: float = 0.0
    #: True = 每个块之间都延迟；False = 只在发第一块前延迟一次
    #: （后者用来测并发：每分片耗时 = 一次延迟，串行 N 倍、并发 1 倍）
    delay_per_block: bool = True
    #: 是否无视 Range 直接返回 200
    ignore_range: bool = False
    #: 是否返回 403
    force_status: int = 0

    def log_message(self, *args):  # noqa: A002 — 静默
        pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 的接口
        if self.force_status:
            self.send_response(self.force_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        total = self.total_bytes
        if self.ignore_range:
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(self.payload_path, "rb") as fh:
                while True:
                    block = fh.read(256 * 1024)
                    if not block:
                        break
                    try:
                        self.wfile.write(block)
                    except (BrokenPipeError, ConnectionResetError):
                        return
            return

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            start_str, _, end_str = spec.partition("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else total - 1
            end = min(end, total - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            self._send_range(start, length)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(total))
        self.end_headers()
        self._send_range(0, total)

    def _send_range(self, start: int, length: int) -> None:
        with open(self.payload_path, "rb") as fh:
            fh.seek(start)
            sent = 0
            block_size = 4096
            first = True
            while sent < length:
                block = fh.read(min(block_size, length - sent))
                if not block:
                    break
                try:
                    self.wfile.write(block)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                sent += len(block)
                if self.chunk_delay:
                    # delay_per_block=False 时：只在第一块**之前**延迟一次，
                    # 之后各块之间不延迟。这样每分片耗时 = 一次延迟，
                    # 串行 N 倍、并发 1 倍，适合测并发。
                    if self.delay_per_block:
                        time.sleep(self.chunk_delay)
                    elif not first:
                        pass
                    else:
                        time.sleep(self.chunk_delay)
                    first = False


class ThreadedRangeServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    #: listen backlog。socketserver 的默认是 5——8 个分片同时建连接
    #: 就会溢出，连接被瞬时 RST，表现为「并发变成了串行 + 莫名重试」。
    #: 真实服务器的 backlog 都是 128+，这里对齐。
    request_queue_size = 128


class RangeServer:
    """起一个本地 Range 服务器，测试用。"""

    def __init__(
        self,
        total_bytes: int = 1024 * 1024,
        chunk_delay: float = 0.0,
        delay_per_block: bool = True,
        ignore_range: bool = False,
        force_status: int = 0,
        content_disposition: str | None = None,
    ) -> None:
        self.total_bytes = total_bytes
        self.chunk_delay = chunk_delay
        self.delay_per_block = delay_per_block
        self.ignore_range = ignore_range
        self.force_status = force_status
        self.content_disposition = content_disposition

        self._tmpdir = tempfile.TemporaryDirectory(prefix="sdm-test-")
        self.payload_path = Path(self._tmpdir.name) / "payload.bin"
        # 固定内容，方便校验合并后的文件是否一致
        with open(self.payload_path, "wb") as fh:
            block = bytes(range(256)) * 16
            written = 0
            while written < total_bytes:
                chunk = block[: min(len(block), total_bytes - written)]
                fh.write(chunk)
                written += len(chunk)

        handler = type(
            "BoundRangeHandler",
            (RangeFileHandler,),
            {
                "payload_path": self.payload_path,
                "total_bytes": self.total_bytes,
                "chunk_delay": chunk_delay,
                "delay_per_block": delay_per_block,
                "ignore_range": ignore_range,
                "force_status": force_status,
            },
        )
        self._server = ThreadedRangeServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/payload.bin"

    def expected_bytes(self) -> bytes:
        return self.payload_path.read_bytes()

    def stop(self) -> None:
        self._server.shutdown()
        # HTTP/1.1 keep-alive 下，服务器线程可能还挂在等下一个
        # 请求。server_close() 会等它们退出，所以先把「还开着的
        # 连接」标记掉，让 handler 立刻收尾。
        self._server.__shutdown_request = True
        try:
            self._server.server_close()
        except OSError:
            pass
        self._tmpdir.cleanup()

    def __enter__(self) -> "RangeServer":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
