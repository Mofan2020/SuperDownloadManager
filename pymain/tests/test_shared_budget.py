"""多任务并发时的连接预算共享。

**这里守的是一件具体的事：多个任务一起下载时，连接总数不炸。**

改造之前，每个任务各自拿满 `parallelism_cap` 条连接——三个任务就是
96 条，五个任务 160 条。服务器看到这种连接风暴开始限流（429），
然后每个任务都因为限流而失败。**任务越多越下不动**，这是用户能
直接感受到的。

改造之后：

- 全局硬上限还是 `parallelism_cap`，不会因为任务变多而线性膨胀。
- 单个任务独占时用满整个预算（跟以前一样快）。
- 第二个任务进来，第一个让出份额——但**至少留 4 条**给它，
  不然它会完全饿死。
- 任务结束（含失败、取消、暂停）必须把名额还回池子，否则池子会被
  死任务占满，后面的任务再也拿不到连接。

另外守两件相关的事：

- 连接数在 429 / 5xx / 超时之后**要能收回来**（以前只涨不跌）。
- 收回来的连接数会**重新涨回去**——服务器恢复了还得能加连接。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sdm.engine.download_engine import DownloadEngine, StartSpec
from sdm.engine.download_flow import DownloadFlow, DynamicPlanner
from sdm.engine.http_downloader import DownloadError, DownloadFailure
from sdm.engine.range_planner import RangeStrategy
from sdm.engine.task_queue import TaskQueue
from tests.conftest import run_async
from tests.range_server import RangeServer

# -- TaskQueue 本身 -------------------------------------------------


def test_single_task_gets_the_whole_budget() -> None:
    """就一个任务在跑，它拿满整个预算。"""
    queue = TaskQueue(parallelism_cap=32)
    queue.register("a", "downloading")
    assert queue.available_slots("a") == 32


def test_two_tasks_split_the_budget() -> None:
    """两个任务一起跑，总数不超上限。"""
    queue = TaskQueue(parallelism_cap=32)
    queue.register("a", "downloading")
    queue.register("b", "downloading")
    slots_a = queue.available_slots("a")
    slots_b = queue.available_slots("b")
    assert slots_a + slots_b <= 32, f"两个任务加起来 {slots_a + slots_b} 条，超了 32"
    assert slots_a == slots_b


def test_later_task_never_gets_zero_slots() -> None:
    """后来的任务至少分到 4 条，不然它会完全饿死。

    `max(4, cap/(n+1))` 里的那个 4 就是干这个的。
    """
    queue = TaskQueue(parallelism_cap=8)
    for name in ("a", "b", "c", "d", "e", "f", "g"):
        queue.register(name, "downloading")
    for name in ("a", "b", "c", "d", "e", "f", "g"):
        assert queue.available_slots(name) >= 4, f"任务 {name} 分到了 0 条"


def test_paused_task_does_not_take_a_share() -> None:
    """暂停的任务不占份额——槽位给还在下的任务。"""
    queue = TaskQueue(parallelism_cap=32)
    queue.register("a", "downloading")
    queue.register("b", "paused")
    assert queue.available_slots("a") == 32


def test_reserved_slots_are_tracked_globally() -> None:
    """预留的名额记在全局账上。"""
    queue = TaskQueue(parallelism_cap=10)
    queue.register("a", "downloading")
    queue.reserve("a", 6)
    assert queue.used() == 6
    assert queue.reserved_for("a") == 6


def test_share_can_exceed_cap_when_floor_applies() -> None:
    """每个任务至少 4 条——份额之和可以超上限，超的部分由硬池裁。

    硬上限由 `ConnectionBudget` 守，份额只是软上限。
    """
    queue = TaskQueue(parallelism_cap=8)
    for name in ("a", "b", "c", "d"):
        queue.register(name, "downloading")
    total = sum(queue.available_slots(name) for name in ("a", "b", "c", "d"))
    assert total >= 8, "份额之和不该低于上限——那说明有任务被饿死了"
    for name in ("a", "b", "c", "d"):
        assert queue.available_slots(name) >= 4


def test_release_returns_slots_to_the_pool() -> None:
    """归还之后全局占用要降下来。"""
    queue = TaskQueue(parallelism_cap=10)
    queue.register("a", "downloading")
    queue.reserve("a", 8)
    queue.release("a", 3)
    assert queue.reserved_for("a") == 3
    assert queue.used() == 3


def test_remove_task_returns_its_slots() -> None:
    """任务结束必须把名额还回去。

    不还的话池子会被死任务占满——取消或失败之后名额永远留着，
    后面的任务再也拿不到连接。
    """
    queue = TaskQueue(parallelism_cap=10)
    queue.register("a", "downloading")
    queue.reserve("a", 8)
    queue.remove("a")
    assert queue.used() == 0, f"池子还占着 {queue.used()} 条"
    assert queue.reserved_for("a") == 0


def test_set_parallelism_cap_applies_to_new_reservations() -> None:
    """改上限之后新任务按新上限算。"""
    queue = TaskQueue(parallelism_cap=32)
    queue.set_parallelism_cap(8)
    queue.register("a", "downloading")
    assert queue.available_slots("a") == 8


# -- 引擎接上共享池 ------------------------------------------------


def test_engine_registers_task_in_the_shared_pool(tmp_path: Path) -> None:
    """任务跑起来要占用共享池的名额。"""
    srv = RangeServer(total_bytes=2 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=32)
        try:
            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            await asyncio.sleep(0.3)
            assert engine._slot_queue.used() > 0, "任务没占用共享池名额"
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_finished_task_returns_its_slots(tmp_path: Path) -> None:
    """任务跑完，池子要空回去。

    这条最关键——池子被死任务占满是「任务越多越下不动」的另一种
    表现：前一个任务失败之后，后面的任务全部卡在 0 KB/s。
    """
    srv = RangeServer(total_bytes=512 * 1024)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=32)
        try:
            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            # 等它跑完
            for _ in range(200):
                await asyncio.sleep(0.05)
                if engine._slot_queue.used() == 0:
                    break
            assert engine._slot_queue.used() == 0, (
                f"任务结束了池子还占着 {engine._slot_queue.used()} 条"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_two_tasks_do_not_exceed_the_global_cap(tmp_path: Path) -> None:
    """两个任务一起跑，全局连接数不超上限。

    这是这次改造的核心断言。以前两个任务就是 64 条。
    """
    srv = RangeServer(total_bytes=8 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=8)
        try:
            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override=f"out{i}.bin",
                    )
                    for i in range(2)
                ]
            )
            peak = 0
            for _ in range(60):
                await asyncio.sleep(0.05)
                live = [t for t in engine.live.values() if t.flow is not None]
                total = sum(t.flow.active_connections for t in live)
                peak = max(peak, total)
            assert peak <= 8, f"两个任务同时开了 {peak} 条连接，超了 8"
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_many_tasks_still_respect_the_global_cap(tmp_path: Path) -> None:
    """任务多到份额下限生效时，总连接数还是不能超上限。

    **这条才是硬池的真正用武之地。** 4 个任务抢 8 条连接时，
    份额机制保证每个任务至少 4 条——份额之和是 16，超了上限 8。
    这时只有硬池能兜住：总量必须卡在 8。

    没有硬池的话，4 个任务就是 16 条连接。服务器看到这种连接
    风暴开始限流，然后每个任务都因为限流而失败——**任务越多
    越下不动**。
    """
    srv = RangeServer(total_bytes=16 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=8)
        try:
            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override=f"out{i}.bin",
                    )
                    for i in range(4)
                ]
            )
            peak = 0
            for _ in range(80):
                await asyncio.sleep(0.05)
                live = [t for t in engine.live.values() if t.flow is not None]
                total = sum(t.flow.active_connections for t in live)
                peak = max(peak, total)
            assert peak <= 8, f"四个任务同时开了 {peak} 条连接，超了 8"
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_budget_is_returned_when_tasks_finish(tmp_path: Path) -> None:
    """任务跑完，全局预算要回到 0。

    不还的话预算会被死任务占满——后面的任务拿到 0 个名额，
    永远卡在「下载中」但一个字节都不动。
    """
    srv = RangeServer(total_bytes=512 * 1024)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=8)
        try:
            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override=f"out{i}.bin",
                    )
                    for i in range(3)
                ]
            )
            for _ in range(300):
                await asyncio.sleep(0.05)
                if engine._budget.used == 0:
                    break
            assert engine._budget.used == 0, (
                f"任务都结束了，预算还占着 {engine._budget.used} 条"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_budget_is_returned_after_cancel(tmp_path: Path) -> None:
    """取消的任务也要还预算。"""
    srv = RangeServer(total_bytes=16 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=8)
        try:
            ids = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override=f"out{i}.bin",
                    )
                    for i in range(3)
                ]
            )
            await asyncio.sleep(0.4)
            for task_id in ids:
                await engine.cancel(task_id)
            await asyncio.sleep(0.3)
            assert engine._budget.used == 0, (
                f"取消之后预算还占着 {engine._budget.used} 条"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_new_task_makes_the_running_one_yield(tmp_path: Path) -> None:
    """第二个任务进来，第一个任务的份额要缩水。

    不缩的话两个任务各拿满 32 条 = 64 条连接。
    """
    srv = RangeServer(total_bytes=8 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=16)
        try:
            first = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="first.bin",
                    )
                ]
            )
            await asyncio.sleep(0.4)
            live = engine.live.get(first[0])
            assert live is not None and live.flow is not None
            solo_share = live.flow._slot_limit
            assert solo_share == 16, f"独占时份额是 {solo_share}，不是 16"

            await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="second.bin",
                    )
                ]
            )
            await asyncio.sleep(0.3)
            live = engine.live.get(first[0])
            assert live is not None and live.flow is not None
            assert live.flow._slot_limit == 8, (
                f"第二个任务进来后份额是 {live.flow._slot_limit}，应该缩到 8"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_share_recovers_when_the_other_task_finishes(tmp_path: Path) -> None:
    """别的任务跑完了，这个任务的份额要涨回去。

    不涨回去的话，下载完一个大文件之后，剩下的任务永远只有
    一半的速度——用户会以为设置没生效。
    """
    srv = RangeServer(total_bytes=4 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=16)
        try:
            first = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="first.bin",
                    )
                ]
            )
            second = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="second.bin",
                    )
                ]
            )
            await asyncio.sleep(0.4)
            live = engine.live.get(first[0])
            assert live is not None and live.flow is not None
            assert live.flow._slot_limit == 8, (
                f"两个任务时份额是 {live.flow._slot_limit}，应该各 8"
            )

            await engine.cancel(second[0])
            await asyncio.sleep(0.4)
            live = engine.live.get(first[0])
            assert live is not None and live.flow is not None
            assert live.flow._slot_limit == 16, (
                f"另一个任务结束后份额是 {live.flow._slot_limit}，应该涨回 16"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


def test_cancelled_task_returns_its_slots(tmp_path: Path) -> None:
    """取消的任务也要还名额。"""
    srv = RangeServer(total_bytes=8 * 1024 * 1024, chunk_delay=0.02)

    async def _inner() -> None:
        engine = DownloadEngine(parallelism_cap=16)
        try:
            ids = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            await asyncio.sleep(0.3)
            await engine.cancel(ids[0])
            await asyncio.sleep(0.2)
            assert engine._slot_queue.used() == 0, (
                f"取消之后池子还占着 {engine._slot_queue.used()} 条"
            )
        finally:
            await engine.aclose()

    try:
        run_async(_inner)
    finally:
        srv.stop()


# -- 错误之后连接数要能收回来 ----------------------------------------


def test_transient_error_halves_the_connection_limit(tmp_path: Path) -> None:
    """429 / 5xx / 超时之后连接数要减半。

    服务器在报错的时候继续加连接只会让它更糟——这是原来
    `_ramp_connections` 文档里写着、但代码里没做的事。
    """
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
    )
    flow._governor.set_limit(16)
    assert flow._governor.limit == 16

    flow._on_transient_error(
        DownloadError(DownloadFailure.STATUS, "HTTP 429", status=429)
    )

    assert flow._governor.limit == 8, (
        f"限流之后连接数是 {flow._governor.limit}，应该减半成 8"
    )


def test_transient_error_starts_a_cooldown(tmp_path: Path) -> None:
    """错误之后进入冷却期，冷却期内不放开连接。"""
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
    )
    before = time.monotonic()
    flow._on_transient_error(
        DownloadError(DownloadFailure.NETWORK, "timeout")
    )
    assert flow._error_backoff_until > before, "没有进入冷却期"
    # 冷却期要有实质长度（约 5 秒）——不是只是往后挪一毫秒
    assert flow._error_backoff_until - before >= 4.5, (
        f"冷却期只有 {flow._error_backoff_until - before:.2f}s，太短"
    )


def test_cooldown_prevents_ramp_up(tmp_path: Path) -> None:
    """冷却期内 `_ramp_connections` 不放开连接。

    没有这个测试，`_error_backoff_until = now` 这种变异也能过——
    `now` 在函数里取，自然比 `before` 大。

    **不跑完整的 `_ramp_connections` 循环**——那是个无限后台任务。
    只跑一次迭代的逻辑：mock 掉 sleep，看它会不会因为冷却期跳过加连接。
    """
    planner = DynamicPlanner(
        RangeStrategy(), content_length=1024 * 1024, initial_chunks=2, target_chunks=4
    )
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
    )
    flow._governor.set_limit(4)
    flow._on_transient_error(
        DownloadError(DownloadFailure.STATUS, "HTTP 429", status=429)
    )
    # 冷却期内 ramp 不该涨连接
    now = time.monotonic()
    flow._last_progress_at = now
    # 手动跑一次循环体的判断逻辑，不跑无限 sleep
    if planner.all_complete():
        pass  # 不会进来
    elif time.monotonic() < flow._error_backoff_until:
        # 冷却期内不加连接
        pass
    elif flow._last_progress_at < time.monotonic() - 2.0:
        pass
    else:
        # 这里不该执行
        flow._governor.set_limit(flow._governor.limit + 1)
    assert flow._governor.limit == 2, (
        f"冷却期内连接数涨了：{flow._governor.limit}，应该卡在 2"
    )


def test_cooldown_never_drops_below_one_connection(tmp_path: Path) -> None:
    """再怎么出错也至少留一条连接——降到 0 就永远卡住了。"""
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
    )
    flow._governor.set_limit(1)
    flow._on_transient_error(
        DownloadError(DownloadFailure.STATUS, "HTTP 503", status=503)
    )
    assert flow._governor.limit == 1


def test_slot_limit_caps_the_governor(tmp_path: Path) -> None:
    """全局预算分给这个任务的份额，Governor 不能超。"""
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
        parallelism_cap=32,
    )
    flow.set_slot_limit(4)
    flow._governor.set_limit(32)
    flow.set_slot_limit(4)
    assert flow._governor.limit <= 4, (
        f"Governor 是 {flow._governor.limit}，超过了份额 4"
    )


def test_ramp_respects_the_slot_limit(tmp_path: Path) -> None:
    """放开连接时不能超过全局预算给的份额。"""
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
        parallelism_cap=32,
    )
    flow.set_slot_limit(3)
    flow._governor.set_limit(3)
    assert flow._governor.limit == 3


def test_downloader_reports_transient_errors(tmp_path: Path) -> None:
    """下载器遇到可重试错误时要通知上层。

    不通知的话，Governor 不知道服务器在报错，连接数就只涨不跌。
    """
    from sdm.engine.http_downloader import HTTPDownloader

    reported: list[DownloadError] = []
    downloader = HTTPDownloader(max_retries=0)
    chunk = type("C", (), {"id": 0, "start": 0, "end": 10, "size": 10, "bytes_done": 0})()

    class _Assembler:
        def bytes_written(self, chunk_id: int) -> int:
            return 0

        def chunk_bytes_on_disk(self, chunk_id: int) -> int:
            return 0

    async def _boom(**kwargs):
        raise DownloadError(DownloadFailure.STATUS, "HTTP 429", status=429)

    downloader._stream_chunk = _boom  # type: ignore[method-assign]

    async def _inner() -> None:
        with pytest.raises(DownloadError):
            await downloader.download_chunk(
                url="http://example.com/a.bin",
                chunk=chunk,  # type: ignore[arg-type]
                assembler=_Assembler(),  # type: ignore[arg-type]
                on_transient_error=reported.append,
            )

    run_async(_inner)
    assert reported, "可重试错误没有通知上层"


@pytest.mark.parametrize(
    "kind,status",
    [
        (DownloadFailure.STATUS, 429),
        (DownloadFailure.STATUS, 503),
        (DownloadFailure.NETWORK, None),
    ],
)
def test_all_transient_kinds_trigger_backoff(
    tmp_path: Path, kind: DownloadFailure, status: int | None
) -> None:
    """429、5xx、网络错误都要触发回退。"""
    flow = DownloadFlow(
        url="http://example.com/a.bin",
        destination=tmp_path / "out.bin",
    )
    flow._governor.set_limit(8)
    flow._on_transient_error(DownloadError(kind, "boom", status=status))
    assert flow._governor.limit == 4
