"""进度通道：把分片的字节汇成文件级进度。

对应 Swift 版 Engine/DownloadFlow.swift 里的 ProgressChannel / ChunkProgress。

**这里修的是 Swift v1.0.5-v1.0.6 的「进度条乱窜」bug。**

那两版上报的是「单分片的累计量」，于是行里的进度条会在各分片
完成时来回跳：分片 0 完成显示 33%，分片 1 完成显示 66%，
分片 2 完成显示 100%——而不是单调上升。

正确做法：所有分片的上报值**求和**，得到文件级总量。
这个总量只会单调上升。

另外两个细节：
- 50ms 合并窗口内只发最新值（不是第一个值）。
- 结束前必须 flush，否则最后一段进度会丢。
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator


class ProgressChannel:
    """文件级进度流。"""

    def __init__(self, coalesce_seconds: float = 0.05) -> None:
        self._coalesce = coalesce_seconds
        self._lock = threading.Lock()
        self._last_reported_at = 0.0
        self._pending: int | None = None
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._finished = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记住调用方的事件循环，这样跨线程 report 也能投递。"""
        self._loop = loop

    def report(self, total_bytes: int) -> None:
        """上报文件级总字节数。

        **窗口到期时必须送「这次」的值，不能送 `_pending`。**

        `_pending` 是上一个窗口攒下来的旧值，一定比这次的
        `total_bytes` 小。送旧值等于把这次的增量直接丢掉——
        而且丢的是**最后一段**：下载末尾那次上报正好撞上窗口到期，
        它的值就被 `_pending = None` 抹了，之后 `flush()` 也没东西可发。

        实测后果：2 MB 的文件最终报成 2031616，正好少一个 64 KB
        块（进度条停在 98.97% 再也不动），而且是概率性的——
        看最后一个数据块落在窗口的哪一侧。
        """
        with self._lock:
            now = time.monotonic()
            if now - self._last_reported_at < self._coalesce:
                # 窗口内：记住最新值，等窗口结束再发。
                self._pending = total_bytes
                return
            self._last_reported_at = now
            self._pending = None
        self._deliver(total_bytes)

    def flush(self) -> None:
        """把待发值立即送出去，忽略合并窗口。"""
        with self._lock:
            if self._pending is None:
                return
            pending = self._pending
            self._pending = None
            self._last_reported_at = time.monotonic()
        self._deliver(pending)

    def finish(self) -> None:
        """结束流。"""
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._deliver(None)

    def _deliver(self, value: int | None) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, value)
                return
            except RuntimeError:
                pass
        self._queue.put_nowait(value)

    async def stream(self) -> AsyncIterator[int]:
        """消费进度流。流结束时循环退出。"""
        while True:
            value = await self._queue.get()
            if value is None:
                return
            yield value


class ChunkProgress:
    """分片进度汇总器：把所有分片的上报求和。"""

    def __init__(self, channel: ProgressChannel, base: int = 0) -> None:
        """`base` 是续传时盘上已经有的字节数。

        **必须把它算进来。** 汇总器从 0 起算的话，续传后第一次
        上报报的是「本次下载的字节数」，比盘上已有的少——进度条
        会**倒退**（引擎的 `summary.bytes_done` 直接被这个值覆盖），
        紧接着算出来的速度也是个巨大的假值。
        """
        self._channel = channel
        self._lock = threading.Lock()
        self._aggregate = max(0, base)

    def report(self, per_chunk_bytes: int) -> None:
        """上报某个分片本次写入了多少字节（相对该分片自己的区间）。

        负值直接忽略。进度值是给用户看的累计量，一旦倒退，
        进度条就会来回跳——这正是要避免的行为。
        """
        if per_chunk_bytes < 0:
            return
        with self._lock:
            self._aggregate += per_chunk_bytes
            total = self._aggregate
        self._channel.report(total)
