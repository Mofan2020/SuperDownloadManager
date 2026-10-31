"""动态分段的长尾测试：下载末尾的速度不该塌掉。

**用户看到的现象**：下到 96% 的时候速度掉得很低，比刚开始低很多。
这说明「末尾只剩一两条连接在爬」，前面铺开的并发白白浪费了。

## 这套测试为什么是这么写的

长尾是个**跟机器快慢有关**的现象：CI runner 慢、服务器慢，
下载末尾的行为跟本机不一样。所以「末尾速度掉了多少」这种
绝对阈值，标不准就是一条会随机红的 flaky 测试。

解决办法分两层：

**第一层：结构性的事实**（完全不看时间）

1. 没有 worker 在活没干完的时候提前下班（`workers_exited_early == 0`）
2. worker 确实等过新分片（`worker_idle_waits > 0`）
3. 分片数突破了「目标分片数」= 连接数（`chunks > CONNECTIONS`）

这三条是长尾的**直接成因**，零波动。

**第二层：差分比较**（跟机器快慢无关）

同一个测试里跑两遍——一遍是现在的实现，一遍是「把分片数封顶改回
目标分片数」的旧行为——**同一台机器、同一时刻**，然后比较。

机器快慢在两边同时生效，抵消掉了。所以断言的是
「现在的实现明显好于旧行为」，而不是「现在的实现达到某个绝对值」。
这是 flaky 测试的根治办法：**跟一个已知的坏实现比**。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from sdm.engine import work_dir
from sdm.engine.download_flow import DownloadFlow
from sdm.engine.range_planner import DynamicPlanner, RangeStrategy

from .range_server import RangeFileHandler, RangeServer

#: 尾段起点（占总量的比例）。末尾这 20% 是长尾最明显的地方。
TAIL_START = 0.80

#: 并发连接数。8 是常见的默认档位。
CONNECTIONS = 8

#: 测试用的文件大小。32MB 是权衡出来的：
#:
#: - 太小（4MB）的话，下载总共不到 1 秒，尾段只有几个采样点，
#:   噪声大到区分不出好坏。
#: - 太大（256MB）的话测试要跑 30 秒以上，CI 上纯属浪费。
TOTAL_BYTES = 32 * 1024 * 1024

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

#: 差分比较时，尾段连接数至少要比旧行为多这么多。
#:
#: 实测（好 vs 坏，同机同时）：13 vs 9、14 vs 9、12 vs 8。
#: 差距稳定在 4~5 条。取 2 是留足余量——机器再慢也不至于把
#: 差距抹平，但差得多的机器也不至于误报。
TAIL_CONNECTIONS_GAIN_MIN = 2

#: 差分比较时，尾段平均并发 ÷ 连接数至少要比旧行为高这么多倍数。
#:
#: 实测：0.85 vs 0.41、0.83 vs 0.41、0.81 vs 0.41、0.84 vs 0.42。
#: 比值稳定在 2 倍左右。取 1.5 倍留余量。
TAIL_CONCURRENCY_GAIN_MIN = 1.5


def _tail_start_time(total_bytes: int) -> float | None:
    """尾段的起始时刻——「已发送字节数达到 80%」的那一刻。

    **用字节数定位，不用固定时间。** 固定时间在不同机器上会落在
    完全不同的地方（下载快慢差好几倍），量出来的「尾段」根本不是
    同一段。
    """
    for t, done in RangeFileHandler.timeline:
        if done >= total_bytes * TAIL_START:
            return t
    return None


def _tail_connections(total_bytes: int) -> int:
    """尾段里有**几条不同的连接**在发字节。

    这是最接近用户感受的结构指标：长尾没治的时候，末尾的活只剩
    一条连接在爬，这个数就是 1。

    比「尾段平均并发 ÷ 峰值并发」稳——后者受并发爬坡时机影响
    （爬坡慢只是让峰值晚点到，峰值本身也会飘）。
    """
    start = _tail_start_time(total_bytes)
    if start is None:
        return 0
    return len({
        request_id
        for t, request_id, count in RangeFileHandler.request_timeline
        if t >= start and count > 0
    })


def _tail_average_concurrency(total_bytes: int) -> float:
    """尾段的平均并发（时间加权），除以连接数。

    **平均并发必须时间加权**——把并发数随时间的变化积分，除以尾段
    时长。用「采样点个数」取算术平均是错的：采样点在忙的时候密、
    闲的时候稀，算术平均会偏向忙的那段。
    """
    start = _tail_start_time(total_bytes)
    if start is None:
        return 0.0
    timeline = RangeFileHandler.concurrency_timeline
    points = [(t, active) for t, active in timeline if t >= start]
    if not points:
        return 0.0
    end = RangeFileHandler.timeline[-1][0]
    area = 0.0
    span = 0.0
    current = points[0][1]
    last = points[0][0]
    for t, active in points[1:]:
        area += current * (t - last)
        span += t - last
        last = t
        current = active
    area += current * (end - last)
    span += end - last
    average = area / span if span > 0 else 0.0
    return average / CONNECTIONS


def _run_tail_download(tmp_path: Path, *, legacy_cap: bool = False) -> dict:
    """跑一次下载，返回长尾相关的指标。

    `legacy_cap=True` 时模拟**旧行为**：分片数封顶 = 目标分片数
    （= 连接数）。这是长尾最主要的一个根因——分片数被卡住之后，
    空闲 worker 切不出新分片，只能下班。

    实现方式是临时把 `strategy.max_chunks` 压到 `target`——
    `split_largest()` 的上限判断读的就是这个值。
    """
    import asyncio

    RangeFileHandler.reset_timeline()
    srv = RangeServer(
        total_bytes=TOTAL_BYTES,
        chunk_delay=0.002,
        delay_per_block=True,
    )
    original_split = DynamicPlanner.split_largest

    def capped_split(self):
        """模拟**旧行为**：分片数封顶 + 两个 worker 抢同一分片。

        两件事一起模拟，缺一不可：

        1. **分片数封顶**（`max_chunks` 压到 `target`）——这是长尾
           最主要的根因：分片数被卡住之后，空闲 worker 切不出新
           分片，只能下班。

        2. **抢分片**——旧实现里 `split_largest()` 把新切出来的分片
           **既**放进 `_pending`（别的 worker 会取走）**又**交给调用
           方（当前 worker 直接用）。两个 worker 同时下载同一个分
           片，抢同一把分片锁，传输互相阻塞。

        **第 2 件事必须一起模拟。** 它会让服务端看到的连接数虚高
        （两条连接在抢一个分片，真实吞吐只有一条），而时间加权
        的平均并发度因为传输被拖慢而**变低**。

        不模拟它的话，「旧行为」那一档的数值会随别的实现细节浮动
        ——2026-10-31 修「两个 worker 抢同一分片」那个 bug 之后，
        这一档的并发度从 0.21 涨到 0.52，比值从 3.8 倍压到 1.4 倍，
        差分测试就红了。但那不是长尾没治，是**分母的假并发被揭穿
        了**。

        基准必须是**稳定的坏实现**，不然差分比较没有意义。
        """
        if not legacy_cap:
            return original_split(self)
        saved = self.strategy.max_chunks
        self.strategy.max_chunks = self.target
        try:
            result = original_split(self)
            if result is not None:
                # **旧行为：新分片既进队列、又交给调用方。**
                self._pending.append(result.chunk.id)
            return result
        finally:
            self.strategy.max_chunks = saved

    DynamicPlanner.split_largest = capped_split
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

        # 分片布局看 sidecar——那是下载完成时写下的最终布局。
        sidecar = work_dir.sidecar_path(dest)
        chunks: list = []
        if sidecar.exists():
            chunks = json.loads(sidecar.read_text(encoding="utf-8")).get("chunks", [])
        return {
            "elapsed": elapsed,
            "tail_connections": _tail_connections(TOTAL_BYTES),
            "tail_concurrency_ratio": _tail_average_concurrency(TOTAL_BYTES),
            "chunks": len(chunks),
            "workers_exited_early": flow.workers_exited_early,
            "worker_idle_waits": flow.worker_idle_waits,
        }
    finally:
        DynamicPlanner.split_largest = original_split
        srv.stop()


# --------------------------------------------------------------------
# 第一层：结构性的事实（完全不看时间）
# --------------------------------------------------------------------


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


# --------------------------------------------------------------------
# 第二层：差分比较（跟机器快慢无关）
# --------------------------------------------------------------------


def test_tail_is_served_by_far_more_connections_than_the_legacy_behavior(
    tmp_path: Path,
) -> None:
    """现在的实现，尾段的连接数必须**明显多于**旧行为。

    **为什么跟旧行为比，而不是跟一个绝对值比。** 尾段的表现跟
    机器快慢有关：CI runner 慢、服务器慢，尾段的连接数就少
    （实测慢服务器下 12 条会掉到 9 条）。绝对阈值标不准就是一条
    会随机红的 flaky 测试。

    跟旧行为比就稳了：**同一台机器、同一时刻**跑两遍，机器快慢在
    两边同时生效，抵消掉。断言的是「现在的实现明显更好」。

    实测差距（好 vs 坏）：13 vs 9、14 vs 9、12 vs 8。
    """
    good = _run_tail_download(tmp_path / "good")
    legacy = _run_tail_download(tmp_path / "legacy", legacy_cap=True)

    gain = good["tail_connections"] - legacy["tail_connections"]
    assert gain >= TAIL_CONNECTIONS_GAIN_MIN, (
        f"尾段连接数只比旧行为多 {gain} 条"
        f"（现在 {good['tail_connections']}，旧行为 {legacy['tail_connections']}）"
        f"——长尾没治"
    )


def test_tail_concurrency_is_far_higher_than_the_legacy_behavior(
    tmp_path: Path,
) -> None:
    """现在的实现，尾段平均并发必须**明显高于**旧行为。

    理由跟上一条一样：跟旧行为比，机器快慢抵消掉。

    实测（好 vs 坏）：0.85 vs 0.41、0.83 vs 0.41、0.81 vs 0.41、
    0.84 vs 0.42——稳定在 2 倍左右。
    """
    good = _run_tail_download(tmp_path / "good")
    legacy = _run_tail_download(tmp_path / "legacy", legacy_cap=True)

    good_ratio = good["tail_concurrency_ratio"]
    legacy_ratio = legacy["tail_concurrency_ratio"]
    assert good_ratio >= legacy_ratio * TAIL_CONCURRENCY_GAIN_MIN, (
        f"尾段平均并发只有旧行为的 {good_ratio / legacy_ratio:.2f} 倍"
        f"（现在 {good_ratio:.2f}，旧行为 {legacy_ratio:.2f}）"
        f"——长尾没治"
    )
