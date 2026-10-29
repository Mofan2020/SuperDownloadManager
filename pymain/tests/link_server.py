"""换链 / 续传测试用的本地服务器。

要能确定性地模拟三件事：

1. **链接中途失效**——`serve_limit` 以下的字节正常发，以上的请求
   一律 403。用它代替「等一会儿再切 403」：本地网上文件瞬间就
   下完了，靠时序根本来不及。
2. **服务器每次给不同的 ETag**——`rotate_etag`。CDN 换节点、
   换签名 URL 都会这样，用它验证「续传不能被 ETag 打死」。
3. **记录每个请求的区间**——用来断言「新链接有没有重下旧字节」。
   这是「续传」唯一诚实的判据：只看进度条涨不涨，从头重下也能
   涨满。

探测请求（`Range: bytes=0-0`）和取样请求（带 `X-SDM-Verify` 头）
都不算下载，不记进 `served`——不然断言会被自己的校验请求搅浑。
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import time

# **复用 range_server 里那套已经在 Windows CI 上踩过坑的加固基类。**
#
# - `QuietErrorMixin`：客户端中途断开时，标准库会把 traceback 打到
#   stderr；pytest 退出那一刻打就撞上解释器关闭，整个进程 abort。
# - `BoundedThreadJoinMixin`：关服务器时带超时地等处理线程退出。
#   处理线程持有 socket，Windows 上不等的話删不干净。
#
# 自己另写一份服务器 = 把这些坑重新踩一遍。
from .range_server import BoundedThreadJoinMixin, QuietErrorMixin

__all__ = ["LinkServer"]


class _Handler(http.server.BaseHTTPRequestHandler):
    """支持 Range 的服务器，可运行时切换状态码和 ETag。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # pragma: no cover - 测试里别刷屏
        pass

    def do_GET(self) -> None:
        srv: LinkServer = self.server.linker  # type: ignore[attr-defined]
        if srv.status != 200:
            self.send_response(srv.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        total = len(srv.payload)
        rng = self.headers.get("Range")
        start = 0
        end = total - 1
        status = 200
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            head, _, tail = spec.partition("-")
            start = int(head) if head else 0
            end = int(tail) if tail else total - 1
            end = min(end, total - 1)
            status = 206

        # **只发前 `serve_limit` 字节，超出就是链接失效。**
        limit = srv.serve_limit
        if limit is not None and start >= limit:
            # **等「下面那段」下完再判失效。**
            #
            # 两个分片是并行下的：要是直接 403，另一个分片会被
            # 连带取消，可能一个字节都没落盘——那就测不出「续传」
            # 了（样本为空 → 判定退回 ETag → 结果随机）。
            srv.limit_reached.wait(timeout=10)
            # 再给客户端一点时间把字节写进文件。
            time.sleep(0.5)
            self.send_response(srv.fail_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = max(0, end - start + 1)
        if limit is not None:
            length = min(length, limit - start)
        body = srv.payload[start:start + length]

        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        etag = srv.next_etag()
        if etag:
            self.send_header("ETag", etag)
        if status == 206:
            self.send_header(
                "Content-Range", f"bytes {start}-{start + len(body) - 1}/{total}"
            )
        self.end_headers()
        if body:
            self.wfile.write(body)
        if limit is not None and start + len(body) >= limit:
            # 「下面那段」已经发完了——可以判失效了。
            srv.limit_reached.set()

        # **只把「下载请求」记进 served。**
        is_probe = start == 0 and end == 0 and total > 1
        is_verify = self.headers.get("X-SDM-Verify") == "1"
        if not is_probe and not is_verify:
            srv.record(start, len(body))


class _Threaded(
    BoundedThreadJoinMixin,
    QuietErrorMixin,
    socketserver.ThreadingMixIn,
    http.server.HTTPServer,
):
    allow_reuse_address = True
    request_queue_size = 128
    daemon_threads = True


class LinkServer:
    """换链 / 续传测试用的服务器。"""

    def __init__(
        self,
        payload: bytes,
        etag: str | None = '"v1"',
        rotate_etag: bool = False,
        serve_limit: int | None = None,
        fail_status: int = 403,
    ) -> None:
        self.payload = payload
        self.etag = etag
        self.rotate_etag = rotate_etag
        #: 只发这么多字节，之后的请求一律失败（模拟链接中途失效）。
        self.serve_limit = serve_limit
        #: 「失效线」以上的请求返回什么状态码。403 = 链接失效，
        #: 404 = 文件没了（普通失败）。
        self.fail_status = fail_status
        self.status = 200
        self.served: list[tuple[int, int]] = []
        self._etag_seq = 0
        self._lock = threading.Lock()
        #: 「失效线」以下的内容发完了没有。
        self.limit_reached = threading.Event()
        self._server = _Threaded(("127.0.0.1", 0), _Handler)
        self._server.linker = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/payload.bin"

    def next_etag(self) -> str | None:
        if not self.rotate_etag:
            return self.etag
        with self._lock:
            self._etag_seq += 1
            return f'"rot-{self._etag_seq}"'

    def record(self, start: int, count: int) -> None:
        with self._lock:
            self.served.append((start, count))

    @property
    def total_sent(self) -> int:
        with self._lock:
            return sum(count for _, count in self.served)

    def ranges(self) -> list[tuple[int, int]]:
        """服务器发过的所有区间（闭区间）。"""
        with self._lock:
            return [(start, start + count - 1) for start, count in self.served if count]

    def reset(self) -> None:
        with self._lock:
            self.served = []

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
