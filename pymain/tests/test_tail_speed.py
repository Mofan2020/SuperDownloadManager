"""动态分段的长尾测试：下载末尾的速度不该塌掉。

**用户看到的现象**：下到 96% 的时候速度掉得很低，比刚开始低很多。
这说明「末尾只剩一两条连接在爬」，前面铺开的并发白白浪费了。

**测什么。** 从本地 Range 服务器的**并发时间轴**上看：下载到
最后 20% 时，平均还有几条连接在传数据。

**为什么不用「速度比值」。** 「末尾速度掉了多少」是拿时间比值
算的，CI 机器一忙就飘，阈值标不准就是一条会随机红的 flaky 测试。
而「尾段平均并发 ÷ 峰值并发」是**结构性的**——好的实现下载到最后
都保持满并发，坏的一到最后只剩一条连接在爬。这个比值跟机器快慢
基本无关。

**阈值是怎么标出来的。** 本机实测（16MB / 8 连接，重复 5 次）：

    修复前（分片数封顶=连接数 + worker 拆不动就下班）  0.38 ~ 0.50
    修复后                                            0.91 ~ 0.94

取阈值 **0.70**——离两边都有足够余量，不会因为 CI 机器偏慢或偏快
而翻车。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sdm.engine import work_dir
from sdm.engine.download_flow import DownloadFlow
from sdm.engine.range_planner import RangeStrategy

from .range_server import RangeFileHandler, RangeServer

#: 尾段起点（占总量的比例）。末尾这 20% 是长尾最明显的地方。
TAIL_START = 0.80

#: 尾段平均并发 ÷ 峰值并发，下限。
#:
#: 修复前实测 0.38~0.50，修复后 0.91~0.94。0.70 卡在中间，
#: 两侧余量都够。
TAIL_CONCURRENCY_RATIO_MIN = 0.70

#: 测试用的文件大小。32MB 是权衡出来的：
#:
#: - 太小（4MB）的话，下载总共不到 1 秒，尾段只有几个采样点，
#   噪声大到区分不出好坏。
#: - 太大（256MB）的话测试要跑 30 秒以上，CI 上纯属浪费。
#:
#: 32MB 时修复前后的区分度已经有 0.42 vs 0.94，很够。
TOTAL_BYTES = 32 * 1024 * 1024

#: 并发连接数。8 是常见的默认档位。
CONNECTIONS = 8

#: 策略里的最小分片大小。
#:
#: **必须是 1MB，不能是 64KB。** 最小可拆大小是「跟着剩余量缩放」
#: 的：按「剩余总量 / 目标分片数」算出平均每片该多大，再夹在硬地板
#: （`min_split_bytes`，64KB）和 `min_chunk_size` 之间。
#:
#: 如果 `min_chunk_size` 也设成 64KB，那缩放的结果**恒等于**
#: 64KB——自适应逻辑整个成了空操作，测它等于什么都没测。
#: 1MB 才有区分度：剩下 32MB 时自适应值被 `min_chunk_size` 夹在
#: 1MB，剩下 2MB 时降到 256KB。
MIN_CHUNK_SIZE = 1024 * 1024


def _tail_average_concurrency(total_bytes: int) -> tuple[float, int]:
    """返回 (尾段平均并发, 全程峰值并发)。

    **平均并发是时间加权的**：把并发数随时间的变化积分，除以尾段
    时长。用「采样点个数」取算术平均是错的——采样点在忙的时候密、
    闲的时候稀，算术平均会偏向忙的那段。

    尾段的起点用「已发送字节数达到 80%」来定位，而不是用固定时间
    ——后者在不同机器上会落在不同位置。
    """
    timeline = list(RangeFileHandler.concurrency_timeline)
    sent = list(RangeFileHandler.timeline)
    if not timeline or not sent:
        return 0.0, 0
    peak = max(active for _, active in timeline)

    threshold = total_bytes * TAIL_START
    tail_start = None
    for t, done in sent:
        if done >= threshold:
            tail_start = t
            break
    if tail_start is None:
        return 0.0, peak

    tail_end = sent[-1][0]
    points = [(t, active) for t, active in timeline if t >= tail_start]
    if not points or tail_end <= tail_start:
        return 0.0, peak

    # 时间加权积分。并发数在两个采样点之间按「后一个采样点的值」
    # 近似（阶梯函数）——采样间隔只有几毫秒，这个近似够准。
    area = 0.0
    span = 0.0
    current = points[0][1]
    last = points[0][0]
    for t, active in points[1:]:
        area += current * (t - last)
        span += t - last
        last = t
        current = active
    area += current * (tail_end - last)
    span += tail_end - last
    return (area / span if span > 0 else 0.0), peak


def _run_tail_download(tmp_path: Path) -> dict:
    """跑一次下载，返回长尾相关的指标。"""
    import asyncio

    RangeFileHandler.reset_timeline()
    srv = RangeServer(
        total_bytes=TOTAL_BYTES,
        chunk_delay=0.002,
        delay_per_block=True,
    )
    try:
        dest = tmp_path / "tail.bin"
        flow = DownloadFlow(
            url=srv.url,
            destination=dest,
            strategy=RangeStrategy(
                initial_chunk_size=64 * 1024,
                min_chunk_size=MIN_CHUNK_SIZE,
                default_chunks=CONNECTIONS,
                max_chunks=64,
                initial_chunks=2,
            ),
            parallelism_cap=CONNECTIONS,
            initial_connections=2,
        )
        started = time.monotonic()
        asyncio.run(flow.run())
        elapsed = time.monotonic() - started

        tail_avg, peak = _tail_average_concurrency(TOTAL_BYTES)
        # 分片布局看 sidecar——那是下载完成时写下的最终布局。
        sidecar = work_dir.sidecar_path(dest)
        chunks: list = []
        if sidecar.exists():
            chunks = json.loads(sidecar.read_text(encoding="utf-8")).get("chunks", [])
        return {
            "elapsed": elapsed,
            "tail_average_concurrency": tail_avg,
            "peak_concurrency": peak,
            "ratio": tail_avg / peak if peak else 0.0,
            "chunks": len(chunks),
            "workers_exited_early": flow.workers_exited_early,
            "worker_idle_waits": flow.worker_idle_waits,
            "min_live_workers": flow.min_live_workers,
        }
    finally:
        srv.stop()


def test_tail_keeps_connections_busy(tmp_path: Path) -> None:
    """尾段（后 20%）必须保持接近满并发。

    **这是本次修复的核心断言。**

    旧实现有两个问题叠在一起：

    1. 分片数封顶 = 目标分片数（= 连接数）。连接一下完，空闲
       worker 想去切最大的未完成分片，切不动 → **worker 直接退出**。
    2. 最小可拆大小固定不变，剩余量少到一定程度后一片都拆不动。

    修好之后：分片数上限改成 `max_chunks`（连接数×4 或 32），
    最小可拆大小随剩余量自适应，worker 拆不动时不退出而是等新分片。

    效果：末尾一直有活干，尾段并发不掉到峰值的 70% 以下。
    """
    metrics = _run_tail_download(tmp_path)
    ratio = metrics["ratio"]
    assert ratio >= TAIL_CONCURRENCY_RATIO_MIN, (
        f"尾段平均并发只有峰值的 {ratio:.0%}"
        f"（{metrics['tail_average_concurrency']:.1f} / {metrics['peak_concurrency']}）"
        f"——长尾没治，下载末尾的速度会塌掉"
    )


def test_chunk_layout_exceeds_target(tmp_path: Path) -> None:
    """分片数要能突破「目标分片数」。

    **目标分片数 = 连接数**，不是分片数的硬上限。多出来的分片就是
    给空闲连接准备的活干——没有它们，末尾就没法再切出新的活，
    也就没有长尾可言。
    """
    metrics = _run_tail_download(tmp_path)
    assert metrics["chunks"] > CONNECTIONS, (
        f"只切出 {metrics['chunks']} 个分片（目标 {CONNECTIONS}）——"
        f"分片数还是被目标分片数卡住了"
    )


def test_no_worker_exits_while_work_remains(tmp_path: Path) -> None:
    """**没有 worker 在活没干完的时候提前下班。**

    这是长尾的**直接成因**：worker 一退出，它那条连接就永久没了。
    下载末尾的活越来越少人干，活跃连接数一路往下掉，速度跟着掉到
    1/N——就是「下到 96% 就不动了」。

    这条断言盯的是行为本身，不是「末尾速度够不够快」这种间接
    指标——后者受机器负载影响，标不准阈值就是一条会随机红的
    flaky 测试。
    """
    metrics = _run_tail_download(tmp_path)
    assert metrics["workers_exited_early"] == 0, (
        f"{metrics['workers_exited_early']} 个 worker 在活没干完的时候跑了"
    )


def test_workers_wait_for_new_chunks_instead_of_idling_out(tmp_path: Path) -> None:
    """拆不动的时候 worker 要**等**，不是下班。

    尾段确实会出现「暂时拆不出新分片」的时刻（剩下的字节不够再
    拆一半）。这时候 worker 必须等着——别的 worker 切出新分片时
    自己会醒，末尾的活照样分给所有连接。

    等的次数必须大于 0：一条都不等，说明这条分支根本没走到，
    下面的「没人提前下班」就成了空断言。
    """
    metrics = _run_tail_download(tmp_path)
    assert metrics["worker_idle_waits"] > 0, (
        "worker 一次都没等过新分片——说明「等」这条分支没走到"
    )


def test_tail_concurrency_is_structural_not_a_timing_ratio(tmp_path: Path) -> None:
    """尾段并发必须是**结构性**的（几条连接在传），不是速度比值。

    这条是给前一条兜底的：万一有人把并发时间轴的采集改坏了，
    上面那条会因为除以 0 或者拿不到采样而误判。这条直接盯住
    「尾段至少有多条连接在飞」这个最原始的事实。
    """
    metrics = _run_tail_download(tmp_path)
    assert metrics["tail_average_concurrency"] >= 2.0, (
        f"尾段平均只有 {metrics['tail_average_concurrency']:.1f} 条连接在传"
    )
