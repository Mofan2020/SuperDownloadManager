"""「连接中」阶段：探测 + 建连这段不能显示成 0 KB/s。

**背景（用户 2026-10-25 实测）**：一个任务跑了 3 分半，界面一直是
「0 KB/s」，没有报错，也没有进度，最后被用户取消。

定位下来是三件事叠在一起：

1. 引擎层探一次、流程层又探一次 —— 两个 RTT 的纯等待。
2. 探测用的是分片的 read 超时（60 秒的空闲看门狗），
   而且重试预算只统计退避的 sleep、不统计请求本身耗掉的时间，
   服务器不响应时能静默好几分钟。
3. 界面在收到第一个字节之前，速度格恒显示「0 KB/s」——
   用户分不清「还没开始」和「卡死了」。

这一批测试守着这三件事。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from sdm.engine.content_sniffer import ContentProbe, ProbeError
from sdm.engine.download_flow import DownloadFlow, FlowError, FlowFailure
from sdm.engine.http_downloader import HTTPDownloader
from sdm.engine.range_planner import RangeStrategy
from tests.conftest import run_async
from tests.range_server import RangeServer


def _probe(url: str = "http://example.com/a.bin", length: int = 1024) -> ContentProbe:
    return ContentProbe(
        url=url,
        content_length=length,
        accepts_ranges=True,
        etag=None,
        last_modified=None,
        content_type="application/octet-stream",
        suggested_filename="a.bin",
        is_final=True,
        status_code=206,
        range_supported=True,
    )


class _CountingDownloader(HTTPDownloader):
    """记探测次数的下载器。

    继承真的 `HTTPDownloader`（不是 Mock），这样除 `probe` 之外的
    方法都还是真的行为——测的是「流程层还会不会再探一次」。
    """

    def __init__(self) -> None:
        super().__init__()
        self.probe_calls = 0

    async def probe(self, url, extra_headers=None):  # type: ignore[override]
        self.probe_calls += 1
        return _probe(url)


# -- 1. 流程层不再重复探测 -------------------------------------------


def test_flow_reuses_probe_result_instead_of_probing_again(tmp_path: Path) -> None:
    """给了探测结果，流程层就**不能**再探一次。

    早先引擎探一次拿文件名、流程再探一次拿长度——两个 RTT。
    服务器建连慢的时候（实测 2.5 秒一个 RTT）这就是 5 秒的
    纯等待，界面全程 0 KB/s。
    """
    server = RangeServer(total_bytes=64 * 1024)
    try:

        async def _inner() -> None:
            downloader = _CountingDownloader()
            dest = tmp_path / "out.bin"
            flow = DownloadFlow(
                url=server.url,
                destination=dest,
                downloader=downloader,
                strategy=RangeStrategy(
                    initial_chunk_size=16 * 1024, default_chunks=2, max_chunks=2
                ),
                parallelism_cap=2,
                # 调用方已经探好了，直接给它。
                probe=ContentProbe(
                    url=server.url,
                    content_length=server.total_bytes,
                    accepts_ranges=True,
                    etag=None,
                    last_modified=None,
                    content_type="application/octet-stream",
                    suggested_filename="out.bin",
                    is_final=True,
                    status_code=206,
                    range_supported=True,
                ),
            )
            await flow.run()
            assert dest.read_bytes() == server.expected_bytes()
            assert downloader.probe_calls == 0, "流程层又探了一次"

        run_async(_inner)
    finally:
        server.stop()


def test_flow_uses_provided_probe_error_without_probing_again(tmp_path: Path) -> None:
    """调用方探过了但失败了——流程层直接用这个错误，不再探。"""

    async def _inner() -> None:
        downloader = _CountingDownloader()
        flow = DownloadFlow(
            url="http://example.com/a.bin",
            destination=tmp_path / "out.bin",
            downloader=downloader,
            probe_error=ProbeError("HTTP 404", status=404),
        )
        with pytest.raises(FlowError) as exc:
            await flow.run()
        assert exc.value.kind == FlowFailure.PROBE_FAILED.value
        assert exc.value.status == 404
        assert downloader.probe_calls == 0, "探测失败后又探了一次"

    run_async(_inner)


# -- 2. 探测的重试预算要算请求本身的时间 ------------------------------


def test_probe_budget_counts_request_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """请求耗掉的时间必须算进重试预算。

    原来预算只统计退避的 sleep：服务器不响应时，每次请求都要
    等满客户端的 read 超时（60 秒），预算永远花不出去，
    5 次重试 = 5 分钟静默。
    """
    attempts = {"n": 0}

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        attempts["n"] += 1
        # 每次请求都要 0.2 秒——模拟「服务器慢，但有响应」。
        await asyncio.sleep(0.2)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    downloader = HTTPDownloader(probe_retry_budget=0.5, max_retries=5)
    started = time.monotonic()
    with pytest.raises(ProbeError):
        asyncio.run(downloader.probe("http://example.com/a.bin"))
    elapsed = time.monotonic() - started

    # 0.5 秒预算：第 1 次请求就花掉 0.2 秒，再加 0.5 秒退避就超了，
    # 所以只该试 1 次。早先的算法会试满 6 次（退避 0.5+1+1.5+2+2 秒）。
    assert attempts["n"] == 1, f"探测次数没被预算卡住：{attempts['n']} 次"
    assert elapsed < 1.0, f"探测静默了 {elapsed:.2f} 秒"


def test_probe_uses_its_own_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测必须用自己的短超时，不能用分片的 60 秒看门狗。"""
    seen: list[object] = []

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        seen.append(timeout)
        return _probe(url)

    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    downloader = HTTPDownloader(probe_timeout=12.0)
    asyncio.run(downloader.probe("http://example.com/a.bin"))

    assert seen, "探测没有拿到超时参数"
    timeout = seen[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 12.0, f"探测的读超时是 {timeout.read}，不是 12 秒"
    # 客户端自己的 read 超时是 60 秒（分片的空闲看门狗），
    # 两者不能混。
    assert downloader.idle_timeout == 60.0


def test_probe_timeout_failure_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测超时要给用户一句人话，不是 `ReadTimeout` 这种类名。"""

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    downloader = HTTPDownloader(probe_retry_budget=0.1, max_retries=0)
    with pytest.raises(ProbeError) as exc:
        asyncio.run(downloader.probe("http://example.com/a.bin"))
    assert "超时" in exc.value.message, f"文案没提到超时：{exc.value.message!r}"
    assert "ReadTimeout" not in exc.value.message


# -- 3. 引擎把探测结果交给流程层 --------------------------------------


def test_engine_passes_probe_to_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """引擎探到的结果要直接交给流程层，流程层不许再探。"""
    from sdm.engine import download_engine as engine_mod

    created: list[dict] = []
    real_flow_cls = engine_mod.DownloadFlow

    class _RecordingFlow(real_flow_cls):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            created.append(kwargs)
            # 不真的下载——这里只关心参数有没有传下去。
            super().__init__(**kwargs)

        async def run(self):  # type: ignore[override]
            raise FlowError(FlowFailure.PROBE_FAILED.value, "测试：不真跑")

    monkeypatch.setattr(engine_mod, "DownloadFlow", _RecordingFlow)

    class _ProbeOnceDownloader(HTTPDownloader):
        def __init__(self) -> None:
            super().__init__()
            self.probe_calls = 0

        async def probe(self, url, extra_headers=None):  # type: ignore[override]
            self.probe_calls += 1
            return _probe(url)

    downloader = _ProbeOnceDownloader()
    engine = engine_mod.DownloadEngine()
    engine.downloader = downloader

    async def _inner() -> None:
        await engine.start([
            engine_mod.StartSpec(
                url="http://example.com/a.bin",
                destination_directory=tmp_path,
            )
        ])
        # 让任务跑起来（_run_task 是后台任务）。
        for _ in range(50):
            await asyncio.sleep(0.02)
            if created:
                break
        assert created, "流程层根本没被创建"
        assert created[0].get("probe") is not None, "探测结果没传给流程层"
        assert created[0].get("probe_error") is None

    run_async(_inner)
    assert downloader.probe_calls == 1, f"探测了 {downloader.probe_calls} 次"


def test_engine_marks_task_as_connecting_until_first_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """任务开跑后、收到第一个字节前，connecting 必须是 True。

    收到字节就翻成 False——这是界面「连接中 → 0 KB/s」的切换点。
    """
    from sdm.engine import download_engine as engine_mod

    class _FakeChannel:
        """只发两帧进度：第一帧 0 字节，第二帧有字节。"""

        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

        def bind_loop(self, loop) -> None:
            self._loop = loop

        def report(self, value: int) -> None:
            self._queue.put_nowait(value)

        async def stream(self):
            while True:
                value = await self._queue.get()
                yield value

    class _FakeFlow:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.planned_chunks = []
            self.server_supports_ranges = True
            self.on_chunk_progress = None

        async def run(self):
            channel = self.kwargs["progress_channel"]
            channel.report(0)
            await asyncio.sleep(0)
            channel.report(1024)
            await asyncio.sleep(0)
            raise FlowError(FlowFailure.PROBE_FAILED.value, "测试：不真跑")

    monkeypatch.setattr(engine_mod, "DownloadFlow", _FakeFlow)
    monkeypatch.setattr(engine_mod, "ProgressChannel", _FakeChannel)

    class _ProbeOnceDownloader(HTTPDownloader):
        async def probe(self, url, extra_headers=None):  # type: ignore[override]
            return _probe(url)

    engine = engine_mod.DownloadEngine()
    engine.downloader = _ProbeOnceDownloader()

    async def _inner() -> None:
        await engine.start([
            engine_mod.StartSpec(
                url="http://example.com/a.bin",
                destination_directory=tmp_path,
            )
        ])
        task_id = next(iter(engine.tasks))
        await asyncio.sleep(0.05)
        summary = engine.tasks[task_id]
        # 收到字节之后不该再是「连接中」。
        assert summary.connecting is False
        # 任务失败后也不该是「连接中」。
        assert summary.state == "failed"

    run_async(_inner)
