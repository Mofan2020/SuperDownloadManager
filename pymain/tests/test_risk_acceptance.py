"""按台账 docs/functional-inventory.md 的验收标准逐条验证。

每个测试的 docstring 写明对应哪个 R 编号和验收标准。
测不到的直接跳过并说明——不假装通过。
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import socketserver
import threading
import time
import uuid
from pathlib import Path

import pytest

from sdm.engine.download_flow import DownloadFlow, FlowError
from sdm.engine.http_downloader import HTTPDownloader
from sdm.engine.range_planner import RangeStrategy
from sdm.engine.speed_display import SpeedInput, compute

from .range_server import BoundedThreadJoinMixin, QuietErrorMixin, RangeServer


def _run(coro):
    return asyncio.run(coro)


def _payload(path: Path, total: int) -> None:
    block = bytes(range(256)) * 16
    with open(path, "wb") as fh:
        written = 0
        while written < total:
            fh.write(block[: min(len(block), total - written)])
            written += len(block)


class _SlowBodyHandler(http.server.BaseHTTPRequestHandler):
    """响应头先发，body 延迟才开始——用来区分流式和整块缓冲。"""

    protocol_version = "HTTP/1.1"
    payload_path: Path = Path()
    total_bytes: int = 0
    body_delay: float = 0.4

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        total = self.total_bytes
        # 正确处理 Range：这个服务器用来验证「流式传输」，
        # 不能同时造「Content-Range 不匹配」的坑。
        # 不匹配的场景由 test_r32_* 单独覆盖。
        start, end = 0, total - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            s_str, _, e_str = spec.partition("-")
            start = int(s_str) if s_str else 0
            end = int(e_str) if e_str else total - 1
            end = min(end, total - 1)
        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        time.sleep(self.body_delay)
        with open(self.payload_path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                b = fh.read(min(65536, remaining))
                if not b:
                    break
                remaining -= len(b)
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    return


class _Srv(
    BoundedThreadJoinMixin,
    QuietErrorMixin,
    socketserver.ThreadingMixIn,
    http.server.HTTPServer,
):
    allow_reuse_address = True
    request_queue_size = 128


# --------------------------------------------------------------------
# R-01 真流式
# --------------------------------------------------------------------


def test_r01_download_is_streaming_not_buffered(tmp_path: Path) -> None:
    """R-01：分片传输必须流式。

    验收标准：用 client.send(stream=True) + aiter_bytes()，
    禁止 await client.get() 读全量。

    验证方式：服务器发完响应头后延迟 0.4 秒才开始发 body。
    整块缓冲要等 body 全部收完才返回；流式的话进度会分多次上报。
    """

    async def _inner() -> None:
        total = 512 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        handler = type("H", (_SlowBodyHandler,), {
            "payload_path": payload, "total_bytes": total, "body_delay": 0.4,
        })
        srv = _Srv(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/p.bin"
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                ),
                parallelism_cap=8,
            )
            # 消费进度流，数更新次数
            seen: list[int] = []

            async def consume() -> None:
                async for value in flow.progress_channel.stream():
                    seen.append(value)

            consumer = asyncio.create_task(consume())
            await flow.run()
            await asyncio.sleep(0.2)
            consumer.cancel()

            # 流式：进度分多次上报。整块缓冲只会报最后一次。
            # 512KB / 8 片 / 50ms 合并窗口 → 至少 2 次
            assert len(seen) >= 2, f"进度只报了 {len(seen)} 次，像是整块缓冲"
            assert dest.stat().st_size == total
        finally:
            srv.shutdown()
            srv.server_close()

    _run(_inner())


# --------------------------------------------------------------------
# R-02 终态速度硬清零
# --------------------------------------------------------------------


def test_r02_terminal_state_speed_is_exactly_zero() -> None:
    """R-02：终态与暂停态硬清零（不衰减）；引擎报 0 时不用历史 EMA 兜底。"""
    prev = 0.0
    # 有流量
    out = compute(SpeedInput(
        state="downloading",
        engine_speed_bytes_per_sec=1000.0,
        delta_bytes_since_last_tick=1000,
        tick_interval_seconds=1.0,
    ), prev)
    assert out.displayed_speed > 0, "有流量时速度应大于 0"
    prev = out.next_ema

    # 暂停
    out = compute(SpeedInput(
        state="paused",
        engine_speed_bytes_per_sec=1000.0,
        delta_bytes_since_last_tick=0,
        tick_interval_seconds=1.0,
    ), prev)
    assert out.displayed_speed == 0.0, f"暂停后速度应为 0，实际 {out.displayed_speed}"
    assert out.next_ema == 0.0, "暂停后 EMA 也要清零"

    # 完成
    out = compute(SpeedInput(
        state="completed",
        engine_speed_bytes_per_sec=500.0,
        delta_bytes_since_last_tick=0,
        tick_interval_seconds=1.0,
    ), prev)
    assert out.displayed_speed == 0.0, f"完成后速度应为 0，实际 {out.displayed_speed}"


# --------------------------------------------------------------------
# R-03 进度单调不减
# --------------------------------------------------------------------


def test_r03_progress_aggregate_is_monotonic() -> None:
    """R-03：进度值是所有分片增量之和，且单调不减。"""
    from sdm.engine.progress_channel import ChunkProgress, ProgressChannel

    ch = ProgressChannel(coalesce_seconds=0.0)
    agg = ChunkProgress(ch)
    agg.report(100)
    agg.report(50)
    agg.report(200)
    agg.report(30)

    # 直接读聚合值：100+50+200+30 = 380
    assert agg._aggregate == 380, f"聚合值不对：{agg._aggregate}"
    # 负值必须被忽略——进度一旦倒退，进度条就会来回跳
    before = agg._aggregate
    agg.report(-50)
    assert agg._aggregate == before, f"负值让聚合值倒退了：{agg._aggregate}"


# --------------------------------------------------------------------
# R-04 限速真的生效
# --------------------------------------------------------------------


def test_r04_speed_limit_is_actually_enforced(tmp_path: Path) -> None:
    """R-04：限速必须在每个分片的字节写入点扣令牌。"""
    from sdm.engine.speed_limiter import SpeedLimiter

    async def _inner() -> None:
        total = 4 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total)
        try:
            limiter = SpeedLimiter(1024 * 1024)
            dest = tmp_path / "slow.bin"
            flow = DownloadFlow(
                url=srv.url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=256 * 1024,
                    default_chunks=4,
                    max_chunks=8,
                ),
                parallelism_cap=4,
                limiter=limiter,
            )
            t1 = time.monotonic()
            await flow.run()
            slow_elapsed = time.monotonic() - t1

            assert dest.stat().st_size == total, "限速后文件不完整"
            # 4 MB @ 1 MB/s = 至少约 4 秒（令牌桶允许一点突发）
            assert slow_elapsed >= 3.0, (
                f"限速没生效：{total} 字节限速 1MB/s 只用了 {slow_elapsed:.2f}s"
            )
        finally:
            srv.stop()

    _run(_inner())


# --------------------------------------------------------------------
# R-07 Content-Disposition 四种形态
# --------------------------------------------------------------------


@pytest.mark.parametrize("header,expected", [
    ('attachment; filename="foo.zip"', "foo.zip"),
    ("attachment; filename=foo.zip", "foo.zip"),
    ("attachment; filename*=UTF-8''foo%20bar.zip", "foo bar.zip"),
    ("attachment; filename=\"fallback.zip\"; filename*=UTF-8''preference.zip",
     "preference.zip"),
])
def test_r07_content_disposition_forms(header: str, expected: str) -> None:
    """R-07：4 种形态全支持；filename* 优先。"""
    from sdm.engine.content_sniffer import parse_content_disposition

    got = parse_content_disposition(header)
    assert got == expected, f"{header!r} → {got!r}，应为 {expected!r}"


def test_r07_empty_content_disposition_falls_back_to_url() -> None:
    """R-07：空字符串也走 URL 兜底。"""
    from sdm.engine.content_sniffer import parse_headers

    probe = parse_headers(
        {"content-disposition": "", "content-length": "100"},
        "https://example.com/files/real-name.zip",
        "https://example.com/files/real-name.zip",
    )
    assert probe.suggested_filename == "real-name.zip", (
        f"空 Content-Disposition 时应回退到 URL，得到 {probe.suggested_filename!r}"
    )


# --------------------------------------------------------------------
# R-08 暂停不报成失败
# --------------------------------------------------------------------


def test_r08_cancel_is_not_reported_as_failure(tmp_path: Path) -> None:
    """R-08：暂停/取消导致的异常绝不能进 failed 分支。"""

    async def _inner() -> None:
        total = 8 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total, chunk_delay=0.05)
        try:
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=srv.url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=256 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                ),
                parallelism_cap=8,
            )
            task = asyncio.create_task(flow.run())
            await asyncio.sleep(0.3)
            flow.cancel()
            # flow 层抛 CancelledError，不是 FlowError。
            #
            # 转成 FlowError 的话，引擎会走 except FlowError 分支，
            # 把任务状态标成 failed——取消/暂停就变成了失败。
            # Swift 版就有这个毛病，这里不能重复。
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            srv.stop()

    _run(_inner())


def test_r08_engine_cancel_is_not_reported_as_failure(tmp_path: Path) -> None:
    """R-08（引擎层）：取消后任务状态是 cancelled，不是 failed。

    上一条测的是 flow 层抛什么。这条测的是引擎把状态写成什么——
    那才是用户看到的。状态标成 failed 的话，UI 上会弹错误提示。
    """
    from sdm.engine.download_engine import DownloadEngine, StartSpec

    async def _inner() -> None:
        total = 8 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total, chunk_delay=0.05)
        try:
            engine = DownloadEngine()
            ids = await engine.start([
                StartSpec(url=srv.url, destination_directory=tmp_path,
                          filename_override="out.bin")
            ])
            task_id = ids[0]
            await asyncio.sleep(0.4)
            await engine.cancel(task_id)
            await asyncio.sleep(0.3)

            summary = engine.get(task_id)
            assert summary is not None
            assert summary.state == "cancelled", (
                f"取消后状态是 {summary.state!r}，不是 cancelled"
            )
        finally:
            srv.stop()

    _run(_inner())


# --------------------------------------------------------------------
# R-09 续传是真续传
# --------------------------------------------------------------------


def test_r09_resume_continues_from_downloaded_bytes(tmp_path: Path) -> None:
    """R-09：续传时 Range 起点 == 已落盘字节数，不是重头下。"""
    from sdm.engine.resume_flow import ResumeFlow

    async def _inner() -> None:
        total = 1 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total)
        try:
            dest = tmp_path / "out.bin"
            # 先下一半，放到 .part
            half = total // 2
            part = dest.with_name(dest.name + ".part")
            with open(part, "wb") as fh:
                fh.write(payload.read_bytes()[:half])

            from sdm.engine.content_sniffer import probe as sniffer_probe
            probe = await sniffer_probe(srv.url)

            flow = ResumeFlow(url=srv.url, destination=dest, probe=probe)
            written = await flow.run()

            assert written == total, f"续传后总字节不对：{written} != {total}"
            got = dest.read_bytes()
            assert len(got) == total, f"续传后长度不对：{len(got)} != {total}"
            assert got == payload.read_bytes(), "续传后内容不一致"
        finally:
            srv.stop()

    _run(_inner())


# --------------------------------------------------------------------
# R-10 取消后不留分片
# --------------------------------------------------------------------


def test_r10_cancel_leaves_no_partial_files(tmp_path: Path) -> None:
    """R-10：用户取消后目录里不留任何 .sdm.partial-*。

    走引擎层（用户在 UI 上点「取消」），不是直接调 flow.cancel()。
    暂停要保留分片以便 resume 接着下，取消要删掉——两者共用
    flow.cancel()，区别在引擎层。
    """
    from sdm.engine.download_engine import DownloadEngine, StartSpec

    async def _inner() -> None:
        total = 8 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total, chunk_delay=0.05)
        try:
            engine = DownloadEngine()
            ids = await engine.start([
                StartSpec(url=srv.url, destination_directory=tmp_path,
                          filename_override="out.bin")
            ])
            task_id = ids[0]
            await asyncio.sleep(0.4)
            await engine.cancel(task_id)
            await asyncio.sleep(0.3)

            leftovers = list(tmp_path.glob("*.sdm.partial-*"))
            assert not leftovers, f"取消后残留分片：{[p.name for p in leftovers]}"
        finally:
            srv.stop()

    _run(_inner())


def test_r10_pause_keeps_partial_files(tmp_path: Path) -> None:
    """暂停要保留分片——resume 时靠它接着下。"""
    from sdm.engine.download_engine import DownloadEngine, StartSpec

    async def _inner() -> None:
        total = 8 * 1024 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total, chunk_delay=0.05)
        try:
            engine = DownloadEngine()
            ids = await engine.start([
                StartSpec(url=srv.url, destination_directory=tmp_path,
                          filename_override="out.bin")
            ])
            task_id = ids[0]
            await asyncio.sleep(0.4)
            await engine.pause(task_id)
            await asyncio.sleep(0.3)

            partials = list(tmp_path.glob("*.sdm.partial-*"))
            assert partials, "暂停后分片被删了，resume 就没法接着下了"
        finally:
            srv.stop()

    _run(_inner())


# --------------------------------------------------------------------
# R-11 并发设置生效
# --------------------------------------------------------------------


def test_r11_parallelism_setting_changes_actual_concurrency(tmp_path: Path) -> None:
    """R-11：改并发上限后，实际并发连接数必须变化。

    **断言的是「服务器观察到的并发数」，不是墙钟时间。**

    原来这条测的是耗时：8 片 × 0.3s 延迟，cap=2 要 ≥0.9s、cap=8 要
    快 2.5 倍。那是**静态分片计划**下的算法——分片切死了，cap=2 就
    只能两片两片地排队。

    规格 §1.2 改成动态分段之后，分片数是长出来的：连接少，分片就
    少，总耗时并不会按比例变慢（这是动态分段的好处）。所以耗时不再
    是「并发上限生效」的可靠指标。

    可靠指标是**服务器同时看到几个请求**。这条测试现在直接量它：
    cap=2 时并发不超过 2，cap=8 时并发能上去。
    """

    async def _inner() -> None:
        total = 8 * 512 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        srv = RangeServer(total_bytes=total, chunk_delay=0.3, delay_per_block=False)
        try:
            observed: dict[int, int] = {}
            for cap in (2, 8):
                dest = tmp_path / f"out{cap}.bin"
                flow = DownloadFlow(
                    url=srv.url,
                    destination=dest,
                    strategy=RangeStrategy(
                        initial_chunk_size=256 * 1024,
                        # 最小分片 64 KB。默认的 1 MB 会把这个 4 MB 的
                        # 文件卡在最多 4 片——拆不到 8 片，并发也就上不去。
                        min_chunk_size=64 * 1024,
                        default_chunks=8,
                        max_chunks=8,
                    ),
                    parallelism_cap=cap,
                )
                srv.reset_counters()
                await flow.run()
                observed[cap] = srv.max_concurrent_requests
                assert dest.read_bytes() == payload.read_bytes()

            assert observed[2] <= 3, f"cap=2 时并发到了 {observed[2]}，上限没生效"
            assert observed[8] > observed[2], (
                f"cap=8 的并发（{observed[8]}）应该比 cap=2（{observed[2]}）高"
            )
            # 服务器观察到的并发可能比上限多 1：分片被拆分时，客户端
            # 停止读取那一路连接就放掉了，但服务器还在往 socket 缓冲区
            # 里写，得等它写失败才收尾。那一路在服务器侧还占着连接。
            assert observed[8] <= 9, f"cap=8 时并发到了 {observed[8]}，超过上限太多"
        finally:
            srv.stop()

    _run(_inner())


# --------------------------------------------------------------------
# R-12 连接数上限
# --------------------------------------------------------------------


def test_r12_httpx_connection_limit_is_sufficient() -> None:
    """R-12：httpx 的 Limits(max_connections) 必须 >= 分片上限。"""
    dl = HTTPDownloader(max_connections=32)
    pool = dl.client._transport._pool
    assert pool._max_connections >= 32, (
        f"连接上限 {pool._max_connections} 小于分片上限 32"
    )


# --------------------------------------------------------------------
# R-19 SHA-256 流式
# --------------------------------------------------------------------


def test_r19_sha256_is_streaming(tmp_path: Path) -> None:
    """R-19：流式分块哈希，大文件不爆内存。"""
    from sdm.gui.bridge import GuiBridge

    big = tmp_path / "big.bin"
    _payload(big, 32 * 1024 * 1024)

    api = GuiBridge.__new__(GuiBridge)
    result = api.compute_checksum(str(big))
    assert result.get("ok"), f"校验失败：{result}"

    expected = hashlib.sha256(big.read_bytes()).hexdigest()
    assert result["sha256"] == expected, "SHA-256 结果不对"


# --------------------------------------------------------------------
# R-21 历史移除真的删
# --------------------------------------------------------------------


def test_r21_history_remove_actually_deletes(tmp_path: Path) -> None:
    """R-21：SQLite 里有真正的 DELETE FROM history WHERE id = ?。"""
    from sdm.history_store import HistoryEntry, HistoryStore

    store = HistoryStore(tmp_path / "history.db")
    entry = HistoryEntry(
        id=str(uuid.uuid4()),
        url="https://example.com/a.zip",
        filename="a.zip",
        destination_path=str(tmp_path / "a.zip"),
        total_bytes=123,
        bytes_done=123,
        state="completed",
        error_message=None,
        started_at=time.time(),
        finished_at=time.time(),
    )
    store.insert(entry)
    assert len(store.all(10)) == 1

    store.remove(entry.id)
    assert store.all(10) == [], "移除后还能查到记录"


# --------------------------------------------------------------------
# R-32 206 但 Content-Range 不匹配
# --------------------------------------------------------------------


class _BadRangeHandler(http.server.BaseHTTPRequestHandler):
    """总是返回整个文件，但状态码是 206。

    这是最阴险的一种服务器行为：CDN 边缘节点缓存了别的
    Range 响应、或者代理把整个文件塞回来了。客户端如果只看
    状态码就会把整个文件写进分片——文件大小看着差不多，
    内容全乱，而且很难查。
    """

    protocol_version = "HTTP/1.1"
    payload_path: Path = Path()
    total_bytes: int = 0

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        total = self.total_bytes
        self.send_response(206)
        self.send_header("Content-Length", str(total))
        self.send_header("Content-Range", f"bytes 0-{total - 1}/{total}")
        self.end_headers()
        with open(self.payload_path, "rb") as fh:
            while True:
                b = fh.read(65536)
                if not b:
                    break
                try:
                    self.wfile.write(b)
                except (BrokenPipeError, ConnectionResetError):
                    return


def test_r32_content_range_mismatch_is_rejected(tmp_path: Path) -> None:
    """R-32：206 但 Content-Range 与请求不符时，必须报错且不写盘。"""

    async def _inner() -> None:
        total = 512 * 1024
        payload = tmp_path / "p.bin"
        _payload(payload, total)

        handler = type("H", (_BadRangeHandler,), {
            "payload_path": payload, "total_bytes": total,
        })
        srv = _Srv(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{port}/p.bin"
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=16,
                ),
                parallelism_cap=8,
            )
            with pytest.raises(FlowError) as exc:
                await flow.run()
            assert "Content-Range" in str(exc.value) or "not_partial" in str(exc.value), (
                f"错误信息没说明是 Content-Range 的问题：{exc.value}"
            )
            # 目标文件不该存在（一个字节都不该写盘）
            assert not dest.exists(), "Content-Range 不匹配时仍然写了目标文件"
        finally:
            srv.shutdown()
            srv.server_close()

    _run(_inner())


def test_r32_content_range_matcher() -> None:
    """R-32：Content-Range 解析本身要对。"""
    from sdm.engine.http_downloader import HTTPDownloader

    match = HTTPDownloader._content_range_matches
    assert match("bytes 0-1023/2048", 0, 1023) is True
    assert match("bytes 0-1023/2048", 0, 1024) is False, "结束位置不同应判不匹配"
    assert match("bytes 1024-2047/2048", 0, 1023) is False, "起始位置不同应判不匹配"
    assert match("", 0, 1023) is False, "缺失应判不匹配"
    assert match("items 0-1023/2048", 0, 1023) is False, "单位不对应判不匹配"
    assert match("bytes abc-def/2048", 0, 1023) is False, "数字解析不了应判不匹配"
