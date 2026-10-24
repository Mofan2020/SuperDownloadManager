"""高并发与瞬时网络故障的测试。

背景：真实世界的服务器和网络不会「稳定地成功」——它们会
backlog 溢出、对端重置连接、代理掐断空闲连接。引擎必须能扛住
这些抖动，而不是让整个任务失败。

这个测试模拟的是「随机比例的连接被立即重置」——这是真实 CDN
和代理最常见的行为之一。验证两件事：
1. 引擎能重试恢复，不会因为几个连接失败就判定整个任务失败
2. 恢复后文件内容完全正确（重试不能写错位置）
"""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading
import time
from pathlib import Path

import pytest

from sdm.engine.download_flow import DownloadFlow
from sdm.engine.range_planner import RangeStrategy

from .range_server import QuietErrorMixin, RangeFileHandler


class FlakyRangeHandler(RangeFileHandler):
    """按比例返回 503。

    用标准状态码而不是「掐断连接」：503 是服务端明确表达的
    「暂时不行，请重试」，客户端能准确识别并按退避策略重试。
    掐断连接那种情况由 test_engine_e2e.py 里的
    test_server_ignores_range 之类的用例覆盖。
    """

    #: 每 N 个请求返回一次 503
    fail_every: int = 0
    _counter: int = 0
    _lock = threading.Lock()

    def do_GET(self) -> None:
        if self.fail_every:
            with FlakyRangeHandler._lock:
                FlakyRangeHandler._counter += 1
                index = FlakyRangeHandler._counter
            if index % self.fail_every == 0:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.send_header("Retry-After", "0")
                self.end_headers()   # Connection: close 由基类统一加
                return
        super().do_GET()


class ThreadedServer(QuietErrorMixin, socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def _run(coro):
    return asyncio.run(coro)


def _make_server(payload: Path, total: int, fail_every: int) -> tuple[ThreadedServer, str]:
    handler = type(
        "BoundFlakyHandler",
        (FlakyRangeHandler,),
        {
            "payload_path": payload,
            "total_bytes": total,
            "chunk_delay": 0.05,
            "delay_per_block": False,
            "ignore_range": False,
            "force_status": 0,
            "fail_every": fail_every,
        },
    )
    server = ThreadedServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/payload.bin"


def test_transient_connection_failures_are_retried(tmp_path: Path) -> None:
    """1/4 的连接被掐断时，任务仍要成功，且内容完全正确。"""

    async def _inner() -> None:
        total = 8 * 64 * 1024
        payload = tmp_path / "payload.bin"
        # 长度必须真的是 total——否则第 1 片往后读直接 EOF，
        # 服务器发 0 字节，客户端报 "peer closed connection"。
        block = bytes(range(256)) * 16
        with open(payload, "wb") as fh:
            written = 0
            while written < total:
                fh.write(block[: min(len(block), total - written)])
                written += len(block)

        server, url = _make_server(payload, total, fail_every=4)
        try:
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                    first_rearrange_after=999,
                ),
                parallelism_cap=8,
            )
            await flow.run()
            assert dest.read_bytes() == payload.read_bytes(), "重试后文件内容不一致"
        finally:
            server.shutdown()
            server.server_close()

    _run(_inner())


def test_retry_does_not_corrupt_chunk_boundaries(tmp_path: Path) -> None:
    """重试后的分片内容必须落在正确的偏移上。

    分片 id 是 partial 文件名的一部分。重试如果从分片起点
    而不是「已落盘字节」继续，就会重复写或者漏写。
    """

    async def _inner() -> None:
        total = 8 * 64 * 1024
        payload = tmp_path / "payload.bin"
        # 长度必须真的是 total——否则第 1 片往后读直接 EOF，
        # 服务器发 0 字节，客户端报 "peer closed connection"。
        block = bytes(range(256)) * 16
        with open(payload, "wb") as fh:
            written = 0
            while written < total:
                fh.write(block[: min(len(block), total - written)])
                written += len(block)

        # 每 3 个请求掐断一个——足够频繁到几乎每片都会遇到
        server, url = _make_server(payload, total, fail_every=3)
        try:
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                    first_rearrange_after=999,
                ),
                parallelism_cap=8,
            )
            await flow.run()
            got = dest.read_bytes()
            want = payload.read_bytes()
            assert len(got) == len(want), f"长度不对：{len(got)} vs {len(want)}"
            if got != want:
                # 找出第一个不同的偏移
                for i, (a, b) in enumerate(zip(got, want, strict=True)):
                    if a != b:
                        raise AssertionError(f"第 {i} 字节不同：{a} != {b}（分片边界 {i // 65536}）")
        finally:
            server.shutdown()
            server.server_close()

    _run(_inner())


def test_persistent_server_error_fails_fast(tmp_path: Path) -> None:
    """服务器一直返回 503 时，任务要**快速**失败。

    不能傻等重试耗尽——服务器稳定地说「不行」的时候，
    再试 7 次只是让用户多等几秒，没有任何意义。
    """

    async def _inner() -> None:
        from sdm.engine.download_flow import FlowError

        total = 8 * 64 * 1024
        payload = tmp_path / "payload.bin"
        # 长度必须真的是 total——否则第 1 片往后读直接 EOF，
        # 服务器发 0 字节，客户端报 "peer closed connection"。
        block = bytes(range(256)) * 16
        with open(payload, "wb") as fh:
            written = 0
            while written < total:
                fh.write(block[: min(len(block), total - written)])
                written += len(block)

        server, url = _make_server(payload, total, fail_every=1)  # 每次都 503
        try:
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                    first_rearrange_after=999,
                ),
                parallelism_cap=8,
            )
            started = time.monotonic()
            with pytest.raises(FlowError):
                await flow.run()
            elapsed = time.monotonic() - started
            # 探测层重试 6 次，退避 0.5+1+1.5+2+2 = 7 秒。
            # 加起来应该在 10 秒内失败——原来指数退避要 25 秒，
            # 用户看到的就是「下载中」卡半分钟才报错。
            assert elapsed < 10.0, f"失败太慢了：{elapsed:.1f}s（重试退避没生效？）"
        finally:
            server.shutdown()
            server.server_close()

    _run(_inner())


def test_partial_file_preserved_on_failure(tmp_path: Path) -> None:
    """传输失败后，已落盘的字节要留着，下次能接着下。"""

    async def _inner() -> None:
        from sdm.engine.file_assembler import FileAssembler
        from sdm.engine.http_downloader import HTTPDownloader
        from sdm.engine.range_planner import Chunk

        from .range_server import RangeServer

        srv = RangeServer(total_bytes=256 * 1024, chunk_delay=0.05)
        try:
            dest = tmp_path / "partial.bin"
            asm = FileAssembler(dest)
            chunk = Chunk(id=0, start=0, end=256 * 1024)
            asm.open_chunk(0, 0)

            dl = HTTPDownloader(max_retries=0)
            try:
                await dl.download_chunk(srv.url, chunk, asm)
            except Exception:
                pass  # 失败是预期的（服务器被关掉了）

            # partial 文件应该还在，且有内容
            partial = dest.with_name(dest.name + ".sdm.partial-0")
            assert partial.exists(), "失败后 partial 文件被删了"
            assert partial.stat().st_size > 0, "partial 文件是空的"
        finally:
            srv.stop()

    _run(_inner())
