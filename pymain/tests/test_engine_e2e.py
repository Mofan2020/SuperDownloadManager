"""引擎端到端测试。

用本地 Range 服务器当下载源，验证完整链路：
探测 → 分片 → 并发下载 → 合并 → sidecar。

**这些是诚实的集成检查。** 「42 个测试通过」只能证明各零件
能用；整条链路没接上也照样能全绿——Swift 版就是这么交出去的，
用户点开才发现下不动。
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdm.engine.download_flow import DownloadFlow, FlowError, FlowFailure
from sdm.engine.file_assembler import FileAssembler
from sdm.engine.progress_channel import ProgressChannel
from sdm.engine.range_planner import RangeStrategy
from tests.conftest import run_async
from tests.range_server import QuietErrorMixin, RangeServer


@pytest.fixture
def server():
    srv = RangeServer(total_bytes=256 * 1024)
    yield srv
    srv.stop()


def test_single_chunk_download_matches_source(tmp_path, server):
    """一个分片下完，文件内容必须和源字节完全一致。"""

    async def _inner():
        dest = tmp_path / "out.bin"
        flow = DownloadFlow(
            url=server.url,
            destination=dest,
            strategy=RangeStrategy(initial_chunk_size=4096, default_chunks=1, max_chunks=1),
            parallelism_cap=1,
        )
        await flow.run()
        assert dest.exists()
        assert dest.read_bytes() == server.expected_bytes()

    run_async(_inner)


def test_multi_chunk_reassembly_is_byte_exact(tmp_path, server):
    """多分片并发下完，合并后的字节必须和源完全一致。

    这里是「分片并行写 + 合并」最容易出错的地方——偏移错一位
    就会导致整个文件损坏，但文件大小看着是对的。
    """

    async def _inner():
        dest = tmp_path / "out.bin"
        flow = DownloadFlow(
            url=server.url,
            destination=dest,
            strategy=RangeStrategy(initial_chunk_size=4096, default_chunks=8, max_chunks=16),
            parallelism_cap=8,
        )
        await flow.run()
        data = dest.read_bytes()
        assert len(data) == server.total_bytes
        assert data == server.expected_bytes()

    run_async(_inner)


def test_chunks_run_in_parallel_not_serially(tmp_path):
    """分片必须真并发。

    每个分片耗时 = 一次 0.15s 延迟（服务器只在第一块前延迟）。
    串行 = 8 × 0.15 = 1.2 秒，并发 ≈ 0.15 秒。

    阈值取 0.9 秒：串行是 1.2 秒，仍能区分；
    但留了余量——CI 机器负载高时不会误报。
    """

    async def _inner():
        srv = RangeServer(total_bytes=8 * 64 * 1024, chunk_delay=0.15, delay_per_block=False)
        try:
            dest = tmp_path / "parallel.bin"
            flow = DownloadFlow(
                url=srv.url,
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
            await flow.run()
            elapsed = time.monotonic() - started
            assert elapsed < 0.9, f"分片像是串行跑的：8×0.15s=1.2s，实际 {elapsed:.2f}s"
            assert dest.read_bytes() == srv.expected_bytes()
        finally:
            srv.stop()

    run_async(_inner)


def test_missing_content_length_is_rejected(tmp_path):
    """服务器不给 Content-Length 时必须明确失败，不是静默卡住。"""
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for _ in range(32):
                    self.wfile.write(b"a" * 4096)
            except Exception:
                pass

    class Server(QuietErrorMixin, socketserver.ThreadingMixIn, socketserver.TCPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    async def _inner():
        flow = DownloadFlow(
            url=f"http://127.0.0.1:{port}/x.bin",
            destination=tmp_path / "nolength.bin",
            parallelism_cap=1,
        )
        with pytest.raises(FlowError) as excinfo:
            await flow.run()
        assert excinfo.value.kind == FlowFailure.NO_CONTENT_LENGTH.value

    try:
        run_async(_inner)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_resume_continues_from_existing_partials(tmp_path, server):
    """暂停后恢复要从断点继续，不能从头重下。

    做法：先下到一半停掉，确认分片文件留在盘上，再跑一次，
    最后校验字节完全正确。
    """

    async def _inner():
        # 用带延迟的服务器，保证下载持续足够长，取消发生在中途。
        slow = RangeServer(total_bytes=256 * 1024, chunk_delay=0.02, delay_per_block=True)
        try:
            dest = tmp_path / "resume.bin"
            strategy = RangeStrategy(initial_chunk_size=4096, default_chunks=4, max_chunks=8)

            flow = DownloadFlow(
                url=slow.url,
                destination=dest,
                strategy=strategy,
                parallelism_cap=4,
            )
            task = asyncio.create_task(flow.run())
            await asyncio.sleep(0.2)
            flow.cancel()
            try:
                await asyncio.wait_for(task, timeout=3)
            except (TimeoutError, asyncio.CancelledError, FlowError):
                pass

            from sdm.engine import work_dir

            partials = list(work_dir.work_root().glob("*.sdm.partial-*"))
            assert partials, "取消后分片文件应该保留，否则续传无从谈起"
            # 取消后不该有合并好的目标文件——那是「下完了」的状态
            assert not dest.exists(), "取消时不该产出目标文件"
            # 下载目录里不该留下任何辅助文件。
            assert not list(tmp_path.glob("*.sdm.*")), "下载目录被辅助文件污染了"

            flow2 = DownloadFlow(
                url=slow.url,
                destination=dest,
                strategy=strategy,
                parallelism_cap=4,
            )
            await flow2.run()
            data = dest.read_bytes()
            assert len(data) == slow.total_bytes
            assert data == slow.expected_bytes()
        finally:
            slow.stop()

    run_async(_inner)


def test_sidecar_written_after_success(tmp_path, server):
    """下载完成后要写 sidecar，供续传和换链接验证用。"""
    import json

    async def _inner():
        dest = tmp_path / "meta.bin"
        flow = DownloadFlow(
            url=server.url,
            destination=dest,
            strategy=RangeStrategy(initial_chunk_size=4096, default_chunks=2, max_chunks=4),
            parallelism_cap=2,
        )
        await flow.run()

        from sdm.engine import work_dir

        sidecar = work_dir.sidecar_path(dest)
        assert sidecar.exists(), "sidecar 必须存在"
        # sidecar 不住在下载目录里。
        assert not list(tmp_path.glob("*.sdm.*")), "下载目录被辅助文件污染了"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["content_length"] == server.total_bytes
        # **分片数量不再等于「目标分片数」。**
        #
        # 动态分段里分片数会超过目标——超出来的那些就是给空闲连接
        # 准备的活干（不然下载末尾会长出很长的尾巴）。所以这里断言的
        # 是「分片完整覆盖整个文件」，不是「有几片」。
        chunks = data["chunks"]
        assert chunks, "sidecar 里必须有分片布局"
        assert chunks[0]["start"] == 0
        assert chunks[-1]["end"] == server.total_bytes
        # 首尾相接、不能有空洞也不能重叠。
        for previous, current in itertools.pairwise(chunks):
            assert current["start"] == previous["end"], "分片之间有空洞或重叠"
        assert all(c["bytes_done"] == c["end"] - c["start"] for c in chunks)

    run_async(_inner)


def test_expired_link_is_detected(tmp_path):
    """403 要被识别成「链接失效」，而不是普通失败。"""

    async def _inner():
        srv = RangeServer(total_bytes=64 * 1024, force_status=403)
        try:
            flow = DownloadFlow(
                url=srv.url,
                destination=tmp_path / "expired.bin",
                strategy=RangeStrategy(initial_chunk_size=4096, default_chunks=1, max_chunks=1),
                parallelism_cap=1,
            )
            with pytest.raises(FlowError) as excinfo:
                await flow.run()
            assert excinfo.value.kind == FlowFailure.EXPIRED_LINK.value
        finally:
            srv.stop()

    run_async(_inner)


def test_progress_is_monotonic(tmp_path, server):
    """进度必须单调上升。

    Swift v1.0.5-v1.0.6 报的是单分片累计量，进度条在 33%/66%/100%
    之间乱窜。这里锁住「只增不减」这个性质。
    """

    async def _inner():
        dest = tmp_path / "progress.bin"
        channel = ProgressChannel()
        seen: list[int] = []

        async def consume():
            async for value in channel.stream():
                seen.append(value)

        consumer = asyncio.create_task(consume())
        flow = DownloadFlow(
            url=server.url,
            destination=dest,
            strategy=RangeStrategy(initial_chunk_size=4096, default_chunks=4, max_chunks=8),
            parallelism_cap=4,
            progress_channel=channel,
        )
        await flow.run()
        await asyncio.wait_for(consumer, timeout=3)

        for i in range(1, len(seen)):
            assert seen[i] >= seen[i - 1], f"进度必须单调不减，实际 {seen}"
        assert seen, "应该至少收到一次进度上报"
        assert seen[-1] == server.total_bytes

    run_async(_inner)


def test_assembler_merges_at_correct_offsets(tmp_path):
    """分片合并必须落在正确偏移上。

    4 个分片各 100KB，第 i 个分片全填字节 i。合并后第 i 个
    分片区间的首字节必须等于 i。
    """
    dest = tmp_path / "assembled.bin"
    assembler = FileAssembler(dest)
    per_chunk = 100 * 1024
    for chunk_id in range(4):
        assembler.open_chunk(chunk_id, chunk_id * per_chunk)
        assembler.append(bytes([chunk_id & 0xFF]) * per_chunk, chunk_id)
    assembler.finalize([0, 1, 2, 3])

    data = dest.read_bytes()
    assert len(data) == 4 * per_chunk
    for chunk_id in range(4):
        assert data[chunk_id * per_chunk] == chunk_id


def test_bytes_written_tracks_disk_not_server(tmp_path):
    """bytes_written 要反映真正落盘的量。

    重连时 Range 头按这个值推算——用「服务器发过的量」会在
    丢包时多下一段重复数据。
    """
    dest = tmp_path / "counted.bin"
    assembler = FileAssembler(dest)
    assembler.open_chunk(0, 0)
    assembler.append(b"x" * 1000, 0)
    assert assembler.bytes_written(0) == 1000
    assembler.append(b"y" * 500, 0)
    assert assembler.bytes_written(0) == 1500
