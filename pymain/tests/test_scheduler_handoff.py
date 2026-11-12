"""队列交给下一个任务的时机。

用户报的现象：几个任务跑完，剩下那个「排队中」还要愣好一会儿才动。

原因是 `_finish_task` 在任务自己的 `finally` 里调 `_schedule()`，而
`_active_count()` 靠 `task.done()` 判断「这个任务还在跑」。任务在
自己的 `finally` 里时它**当然还没结束**——于是名额算满，队列不动；
等协程真的返回，又没有人再调一次 `_schedule()`。

这条测试让 `_run_task` 用假实现（真实现要真的 HTTP 请求），但保持
`finally` 里收尾的形状，这样测的是调度器的交接逻辑本身。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_engine import DownloadEngine, StartSpec

from .conftest import run_async

#: 完成任务要花多久。够长，能确定「第一个还在跑」这个前提成立。
TASK_SECONDS = 0.05

#: 上一个跑完到下一个开跑的容忍时间。
#:
#: 不设成 0：`_schedule` 是同步调用，但新的任务要等事件循环转一圈才
#: 真正开始跑。0.5 秒对「同一个循环里的交接」已经很宽松，而 bug 的
#: 表现是**永远不会开始**，所以这个上界不会把真 bug 放过。
HANDOFF_SECONDS = 0.5


def _engine(tmp_path: Path, monkeypatch, concurrency: int = 1) -> tuple[DownloadEngine, list[str]]:
    """造一个引擎，把真正下载的那一步换成可控的假实现。"""
    engine = DownloadEngine(max_concurrent_tasks=concurrency)
    started: list[str] = []

    async def fake_run(task_id: str, live) -> None:
        started.append(task_id)
        summary = engine.tasks.get(task_id)
        try:
            await asyncio.sleep(TASK_SECONDS)
            if summary is not None:
                summary.state = "completed"
        finally:
            # 跟真的 `_run_task` 一样：收尾在 finally 里，此时这个
            # 协程还没返回。
            engine.live.pop(task_id, None)
            engine._finish_task(task_id)

    monkeypatch.setattr(engine, "_run_task", fake_run)
    return engine, started


def _spec(tmp_path: Path, index: int) -> StartSpec:
    return StartSpec(
        url=f"http://example.invalid/file{index}.bin",
        destination_directory=tmp_path,
        filename_override=f"t{index}.bin",
    )


def test_the_next_queued_task_starts_as_soon_as_one_finishes(tmp_path, monkeypatch):
    """两个任务、一个名额：第一个跑完，第二个要立刻接上。

    「愣好一会儿」就是这里——交接没有发生，一直等到别的事件
    （用户新加任务、改并发上限）把调度器叫醒。
    """
    engine, started = _engine(tmp_path, monkeypatch)

    async def _inner() -> None:
        ids = await engine.start([_spec(tmp_path, 0), _spec(tmp_path, 1)])
        # `create_task` 只是排进循环，还没真的跑起来。
        await asyncio.sleep(0)
        assert len(started) == 1, started

        deadline = asyncio.get_running_loop().time() + TASK_SECONDS + HANDOFF_SECONDS
        while asyncio.get_running_loop().time() < deadline and len(started) < 2:
            await asyncio.sleep(0.01)

        assert len(started) == 2, (
            f"第一个任务跑完了，第二个还在排队——已启动 {len(started)} 个，"
            f"队列 {engine.queue_order()}，id={ids}"
        )

    run_async(_inner)


def test_finishing_several_tasks_hands_over_every_slot(tmp_path, monkeypatch):
    """三个任务、两个名额：第一个跑完下一个接上，最后两个都能跑完。

    只盯一个名额会漏掉「同时结束」那种情况：几个任务在同一轮里
    收尾，每个都以为自己还占着名额。
    """
    engine, started = _engine(tmp_path, monkeypatch, concurrency=2)

    async def _inner() -> None:
        await engine.start([_spec(tmp_path, i) for i in range(3)])

        deadline = asyncio.get_running_loop().time() + (TASK_SECONDS * 6) + HANDOFF_SECONDS
        while asyncio.get_running_loop().time() < deadline and len(started) < 3:
            await asyncio.sleep(0.01)

        assert len(started) == 3, f"只启动了 {len(started)} 个：{started}"

    run_async(_inner)


def test_the_slot_is_not_handed_over_twice(tmp_path, monkeypatch):
    """一个名额时，不能因为收尾没算清楚就把两个任务同时放出去。

    并把上限守住——修「交接太慢」最省事的错法是把计数直接减到 0。
    """
    engine, _started = _engine(tmp_path, monkeypatch, concurrency=1)

    async def _inner() -> None:
        await engine.start([_spec(tmp_path, i) for i in range(3)])

        # 任何时刻真正在跑的都不能超过名额。轮询得够密，才看得见
        # 越界的那一瞬间。
        over = 0
        for _ in range(60):
            running = sum(
                1
                for live in engine.live.values()
                if live.task is not None and not live.task.done()
            )
            if running > 1:
                over = running
            await asyncio.sleep(0.01)

        assert over == 0, f"同时跑了 {over} 个任务，上限是 1"

    run_async(_inner)


# ---------------------------------------------------- 连接名额的爬坡
#
# 分片被 worker 领走不占名额，真正开始传才要。名额从
# `initial_connections`（默认 2）开始放，慢的时候用户看到的就是
# 「一堆分片排队中」——其中一个要等好一会儿才轮到。
#
# 爬坡本身是对的（一上来就开 32 条连接是连接风暴），问题在**步长**：
# 每 100 毫秒只放一条，32 条要 3 秒。用户设的就是 32 线程，等 3 秒
# 才用上，观感上就是「卡住了」。


def _ramp_fixture(limit: int, cap: int, start: int):
    """造一个只够 `_ramp_connections` 用的假 self。"""
    import types

    from sdm.engine.range_planner import ConnectionGovernor

    fake = types.SimpleNamespace(
        _cancelled=False,
        _error_backoff_until=0.0,
        _last_progress_at=time.monotonic(),
        _slot_limit=limit,
        parallelism_cap=cap,
        _governor=ConnectionGovernor(start, cap),
    )
    return fake


class _NotDonePlanner:
    """进度条还在动，但没下完。"""

    def all_complete(self) -> bool:
        return False


async def _run_ramp(fake, seconds: float) -> None:
    from sdm.engine.download_flow import DownloadFlow

    task = asyncio.create_task(
        DownloadFlow._ramp_connections(fake, _NotDonePlanner())  # type: ignore[arg-type]
    )
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_the_connection_ramp_reaches_the_setting_quickly():
    """用户设了 32 线程，半秒内就该用上大半。

    线性爬坡（每条 +1 / 100ms）在半秒里只能到 7 条，剩下的 25 个
    分片都在「排队中」。用户报的就是这个。
    """
    fake = _ramp_fixture(limit=32, cap=32, start=2)
    run_async(_run_ramp, fake, 0.5)
    assert fake._governor.limit >= 16, (
        f"半秒只开到 {fake._governor.limit} 条连接，32 个线程要等"
        f"{(32 - 2) * 0.1:.1f} 秒"
    )


def test_the_ramp_still_starts_gently():
    """别为了快就一上来开满——那正是爬坡要避免的连接风暴。

    **必须跑过第一拍再断言。** 原来只跑 0.05 秒，而爬坡的第一拍在
    0.1 秒——两个版本读到的都还是初始的 2 条，等于什么都没测。
    变异测试当场抓出来了（「一步开满」那条漏网）。
    """
    fake = _ramp_fixture(limit=32, cap=32, start=2)
    # 0.15 秒 = 第一拍已过、第二拍还没到；0.3 秒内最多两次翻倍，
    # 所以 ≤ 8 这一条对「翻倍」永远成立，对「一步开满」必然不成立。
    run_async(_run_ramp, fake, 0.15)
    assert fake._governor.limit <= 8, (
        f"0.15 秒就开到 {fake._governor.limit} 条连接，等于没爬坡"
    )


def test_the_ramp_never_passes_the_slot_share():
    """份额是上限。爬坡再快也不能越过它。"""
    fake = _ramp_fixture(limit=6, cap=32, start=2)
    run_async(_run_ramp, fake, 0.6)
    assert fake._governor.limit <= 6, fake._governor.limit


def test_the_ramp_does_not_climb_while_the_connection_is_stuck():
    """没有字节流动时不加连接——服务器有问题时加连接只会更糟。"""
    fake = _ramp_fixture(limit=32, cap=32, start=2)
    fake._last_progress_at = time.monotonic() - 10.0
    before = fake._governor.limit
    run_async(_run_ramp, fake, 0.5)
    assert fake._governor.limit == before, f"卡住时还把连接涨到了 {fake._governor.limit}"


def test_the_ramp_does_not_climb_during_the_error_cooldown():
    """429 / 5xx 之后的冷却期内不加连接。"""
    fake = _ramp_fixture(limit=32, cap=32, start=2)
    fake._error_backoff_until = time.monotonic() + 30.0
    before = fake._governor.limit
    run_async(_run_ramp, fake, 0.5)
    assert fake._governor.limit == before
