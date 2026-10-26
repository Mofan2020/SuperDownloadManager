"""进度通道的单元测试。

**这些测试必须确定性通过。** 之前「进度最终报不满」只在端到端
下载里偶发（跑 8 次挂 1 次），定位成本极高。根因在通道的合并
窗口里，所以这里用**假时钟**把窗口边界钉死，直接复现。

那条偶发 bug 的机制：`ProgressChannel.report()` 在合并窗口到期时
送的是 `_pending`（上一个窗口攒的旧值），把**这一次**的上报值直接
抹掉。下载末尾那次上报正好撞上窗口到期时，最后一段进度就永远
少报一截——2 MB 的文件最终报成 2031616，正好少一个 64 KB 块。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine import progress_channel as progress_channel_mod
from sdm.engine.progress_channel import ChunkProgress, ProgressChannel
from tests.conftest import run_async


class _FakeClock:
    """假时钟。只提供 `monotonic`，够进度通道用了。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    """把进度通道用的时钟换成假的。

    只换 `progress_channel` 模块里的 `time` 引用——直接 patch
    `time.monotonic` 会影响到整个进程（pytest 自己也用）。
    """
    fake = _FakeClock()
    monkeypatch.setattr(progress_channel_mod, "time", fake)
    return fake


def _drain(channel: ProgressChannel) -> list[int]:
    """把通道里已经排队的值取出来（同步读，不起事件循环）。"""
    out: list[int] = []
    while True:
        try:
            out.append(channel._queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


# --------------------------------------------------------------------
# 合并窗口
# --------------------------------------------------------------------


def test_window_expiry_sends_the_newest_value_not_the_stale_one(clock):
    """窗口到期时必须送**这次**的值。

    旧代码送的是 `_pending`（上一个窗口的旧值），并把这次的
    `total_bytes` 一起丢掉。表现就是进度少报一截。
    """
    ch = ProgressChannel(coalesce_seconds=0.05)

    ch.report(100)          # 第一次：立刻发
    clock.advance(0.01)
    ch.report(200)          # 窗口内 → 攒着
    clock.advance(0.06)     # 窗口过期
    ch.report(300)          # 必须发出 300

    assert _drain(ch) == [100, 300]


def test_flush_sends_the_coalesced_value(clock):
    """窗口内攒的值，flush 时要发出去。"""
    ch = ProgressChannel(coalesce_seconds=0.05)

    ch.report(100)
    clock.advance(0.01)
    ch.report(250)
    ch.flush()

    assert _drain(ch) == [100, 250]


def test_last_report_is_never_lost(clock):
    """最后那次上报不能丢——哪怕它正好撞上窗口到期。

    这条锁的就是端到端那条偶发失败：末尾的数据块落在窗口到期
    那一侧时，进度就永远停在 98.97%。

    时间点是刻意挑的：两次 10ms 的步进稳稳落在 50ms 窗口内，
    最后一次 100ms 的步进稳稳跨过窗口边界——不跟浮点精度较劲。
    """
    ch = ProgressChannel(coalesce_seconds=0.05)
    block = 65536

    ch.report(block)              # 第一次：立刻发
    clock.advance(0.01)
    ch.report(block * 2)          # 窗口内 → 攒着
    clock.advance(0.01)
    ch.report(block * 3)          # 窗口内 → 攒着（pending = block*3）
    # 最后一块：跨过窗口边界。
    clock.advance(0.10)
    ch.report(block * 4)
    ch.flush()

    values = _drain(ch)
    assert values[-1] == block * 4, f"最后报成了 {values[-1]}"
    assert max(values) == block * 4


def test_values_are_monotonic(clock):
    """进度流必须单调上升。"""
    ch = ProgressChannel(coalesce_seconds=0.05)

    total = 0
    for _ in range(200):
        clock.advance(0.007)     # 故意在窗口内外之间来回跳
        total += 65536
        ch.report(total)
    ch.flush()

    values = _drain(ch)
    assert values == sorted(values)


# --------------------------------------------------------------------
# 汇总器
# --------------------------------------------------------------------


def test_chunk_progress_sums_up_the_deltas(clock):
    """汇总器报的是所有分片增量的和。"""
    ch = ProgressChannel(coalesce_seconds=0.0)
    agg = ChunkProgress(ch)

    agg.report(100)
    agg.report(200)
    agg.report(300)

    assert _drain(ch) == [100, 300, 600]


def test_chunk_progress_never_goes_backwards(clock):
    """负增量直接忽略——进度条不能倒退。"""
    ch = ProgressChannel(coalesce_seconds=0.0)
    agg = ChunkProgress(ch)

    agg.report(500)
    agg.report(-100)
    agg.report(200)

    # 负增量被整个丢掉，连一次上报都不发。
    assert _drain(ch) == [500, 700]


def test_chunk_progress_starts_from_the_resume_base(clock):
    """续传时汇总基数是盘上已有的字节。

    少了这个基数，续传后第一次上报会比盘上已有的还小——进度条
    倒退一次（引擎的 `summary.bytes_done` 直接被覆盖），紧接着
    算出来的速度也是个巨大的假值。
    """
    ch = ProgressChannel(coalesce_seconds=0.0)
    agg = ChunkProgress(ch, base=1_000_000)

    agg.report(65536)

    assert _drain(ch) == [1_065_536]


def test_resume_progress_does_not_go_backwards(clock):
    """续传的完整链路：先报盘上已有的，再报增量——不许倒退。"""
    ch = ProgressChannel(coalesce_seconds=0.0)
    pre_existing = 1_000_000

    ch.report(pre_existing)
    agg = ChunkProgress(ch, base=pre_existing)
    agg.report(65536)
    agg.report(65536)

    values = _drain(ch)
    assert values == [1_000_000, 1_065_536, 1_131_072]
    assert values == sorted(values)


# --------------------------------------------------------------------
# 流式消费
# --------------------------------------------------------------------


def test_stream_ends_on_finish(clock):
    """`finish()` 之后流必须结束，消费方不能一直等。"""
    ch = ProgressChannel(coalesce_seconds=0.0)
    seen: list[int] = []

    async def _inner():
        async def consume():
            async for value in ch.stream():
                seen.append(value)

        task = asyncio.create_task(consume())
        ch.report(4096)
        ch.finish()
        await asyncio.wait_for(task, timeout=2)

    run_async(_inner)
    assert seen == [4096]
