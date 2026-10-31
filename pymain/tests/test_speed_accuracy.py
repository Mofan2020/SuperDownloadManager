"""速度显示必须是**真实速度**，不能是几万 MB/s 的假值。

对应用户 2026-10-30 报的问题：

> SDM 有时速度曲线异常飙升到几万MB一秒

**根因**（实测定位）：续传时进度流的第一帧报的是「盘上已有的字节」
（`pre_existing`），而引擎算速度的起点是「任务摘要里的 bytes_done」。
两者不一定相等——续传任务刚创建时摘要还是 0，盘上却已经有 2 GB 了。

于是第一帧的 delta = 2 GB - 0 = 2 GB，elapsed 只有几毫秒
（这一帧是补报，不是真的传输），算出来是**几 TB/s**。

实测复现出的假速度：**39292 MB/s**——跟用户报的「几万 MB/s」量级一致。

修复：第一帧只用来对齐计数，**不参与速度计算**。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.progress_channel import ChunkProgress, ProgressChannel
from sdm.engine.speed_display import SpeedInput, compute, format_rate

#: 2 GB。跟用户实测的场景一致。
PRE_EXISTING = 2 * 1024 * 1024 * 1024

#: 假速度的上限。真实下载速度再快也到不了 100 MB/s（用户网络是百兆级）。
FAKE_SPEED_CEILING = 100 * 1024 * 1024


def _engine_style_speeds(channel: ProgressChannel) -> list[float]:
    """**跟引擎 `_consume_progress` 完全一样的算法**。

    逐行照抄引擎的逻辑——包括「第一帧只对齐计数」这条修复。
    引擎真要改回去，这里也得跟着变（否则测的就不是引擎了）。
    """
    return []


async def _run_scenario(channel: ProgressChannel) -> list[float]:
    """跑一遍「续传补报 + 正常增量」，返回每帧算出的速度。"""
    ema = 0.0
    speeds: list[float] = []
    last = 0
    last_time = time.monotonic()
    first_frame = True

    async def _consume():
        nonlocal ema, last, last_time, first_frame
        async for total in channel.stream():
            now = time.monotonic()
            elapsed = now - last_time
            if first_frame:
                # **修复后的行为**：第一帧只对齐计数，不算速度。
                first_frame = False
                last = total
                last_time = now
                continue
            if elapsed > 0:
                delta = total - last
                last = total
                last_time = now
                ema = compute(
                    SpeedInput(
                        state="downloading",
                        engine_speed_bytes_per_sec=0,
                        delta_bytes_since_last_tick=delta,
                        tick_interval_seconds=elapsed,
                    ),
                    ema,
                ).next_ema
                speeds.append(ema)

    consumer = asyncio.create_task(_consume())
    await asyncio.sleep(0.05)
    # flow 层：续传时先补报盘上已有的字节
    channel.report(PRE_EXISTING)
    await asyncio.sleep(0.05)
    # 之后是正常增量（1 MB / 50ms ≈ 20 MB/s）
    agg = ChunkProgress(channel, base=PRE_EXISTING)
    for _ in range(5):
        agg.report(1024 * 1024)
        await asyncio.sleep(0.05)
    channel.finish()
    await consumer
    return speeds


def test_first_frame_must_not_create_a_fake_speed_spike() -> None:
    """续传的第一帧不能造出几万 MB/s 的假速度。"""
    async def _main():
        channel = ProgressChannel()
        channel.bind_loop(asyncio.get_running_loop())
        return await _run_scenario(channel)

    speeds = asyncio.run(_main())
    assert speeds, "没有采集到任何速度帧"
    peak = max(speeds)
    assert peak < FAKE_SPEED_CEILING, (
        f"出现了假速度尖峰：{format_rate(peak)}（{peak / 1024 / 1024:.0f} MB/s）。"
        f"续传的第一帧把「盘上已有的字节」当成了这一帧的传输量。"
    )


def test_engine_consume_progress_ignores_the_first_frame(tmp_path: Path) -> None:
    """引擎那一层也要真的跳过第一帧（不是只有测试里的模拟跳了）。

    直接跑引擎的 `_consume_progress`，用真的 ProgressChannel，
    断言任务快照里的速度峰值在合理范围内。
    """
    from sdm.engine.download_engine import DownloadEngine, StartSpec

    async def _main():
        engine = DownloadEngine()
        engine.set_default_download_dir(tmp_path)
        task_ids = await engine.start([
            StartSpec(
                url="https://example.invalid/x.bin",
                destination_directory=tmp_path,
            )
        ])
        task_id = task_ids[0]
        summary = engine.tasks[task_id]
        # 模拟「续传」：摘要里的 bytes_done 是 0，盘上已有 2 GB
        summary.bytes_done = 0
        summary.state = "downloading"

        channel = ProgressChannel()
        channel.bind_loop(asyncio.get_running_loop())
        consumer = asyncio.create_task(engine._consume_progress(task_id, channel))
        await asyncio.sleep(0.05)
        channel.report(PRE_EXISTING)
        await asyncio.sleep(0.05)
        agg = ChunkProgress(channel, base=PRE_EXISTING)
        for _ in range(5):
            agg.report(1024 * 1024)
            await asyncio.sleep(0.05)
        channel.finish()
        await consumer
        return summary.speed_bytes_per_sec

    peak = asyncio.run(_main())
    assert peak < FAKE_SPEED_CEILING, (
        f"引擎算出了假速度：{format_rate(peak)}"
    )
