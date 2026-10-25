"""分片规划器。

对应 Swift 版 Engine/RangeManager.swift。

分片就是一串左闭右开的字节区间。分裂 = 去掉一个片，换成共享中点的
两片；合并 = 去掉相邻两片，换成它们的并集。速度采样用滑动窗口，
避免一个从下载开始就存在的慢片一直拖累整体。

Swift 版用 actor 串行化，这里用一个普通类加锁——规划操作都是纯计算，
锁的临界区没有 IO。

**两类规划器，职责不同：**

- `RangePlanner` —— 一次规划、之后不变。Swift 版只有这一个，而且
  `rearrange()` 从来没有调用方，所以「动态分裂合并」实际从未运行过。
- `DynamicPlanner` —— **下载过程中持续变化**的布局。规格 §1.2 要求
  的就是它：「不是开始时一次性切死分片。空闲连接到来时，找到当前
  最大的未完成分片，将其对半拆分，新连接从拆分点继续下载。」
"""

from __future__ import annotations

import asyncio
import enum
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class Chunk:
    """一个下载分片。start 闭、end 开。"""

    id: int
    start: int
    end: int
    bytes_done: int = 0

    @property
    def size(self) -> int:
        return self.end - self.start

    @property
    def progress(self) -> float:
        if self.size <= 0:
            return 1.0
        return min(1.0, max(0.0, self.bytes_done / self.size))

    @property
    def complete(self) -> bool:
        return self.bytes_done >= self.size

    @property
    def write_position(self) -> int:
        """这个分片当前写到的绝对偏移。

        拆分在途分片时要拿它算拆点——拆点必须落在「已经写过的部分」
        之后，否则会把已落盘的字节判成新分片的内容。
        """
        return self.start + self.bytes_done

    @property
    def remaining(self) -> int:
        """还剩多少字节没下。"""
        return max(0, self.end - self.write_position)

    def range_header(self) -> str:
        if self.end - self.start == 1:
            return f"bytes={self.start}-{self.start}"
        return f"bytes={self.start}-{self.end - 1}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "bytes_done": self.bytes_done,
            "size": self.size,
            "progress": round(self.progress, 6),
        }


@dataclass
class RangeStrategy:
    """动态重划分的调参。默认值沿用 Swift v1.0 的「激进」策略。"""

    initial_chunk_size: int = 2 * 1024 * 1024      # 2 MB
    min_chunk_size: int = 1 * 1024 * 1024          # 1 MB
    max_chunk_size: int = 16 * 1024 * 1024         # 16 MB
    max_chunks: int = 128                         # 硬上限
    default_chunks: int = 32                      # 目标分片数
    #: **初始**分片数（规格 §1.2.3「初始连接数保守」）。
    #: 动态规划从这里起步，靠拆分长到 `default_chunks`。
    initial_chunks: int = 2
    sample_window_sec: float = 5.0
    split_threshold: float = 1.5                  # max/min 速度比 > 此值则分裂
    merge_threshold: float = 0.3                  # min/max 速度比 < 此值则合并
    split_streak_sec: float = 5.0
    first_rearrange_after: float = 10.0           # v1.0 激进版：10 秒而非 30 秒


@dataclass
class SpeedSample:
    chunk_id: int
    bytes_per_sec: float


class RearrangeDelta(enum.Enum):
    SPLIT = "split"
    MERGE = "merge"

    def __init__(self, kind: str, chunk_id: int = -1, other_id: int = -1) -> None:
        self.kind = kind
        self.chunk_id = chunk_id
        self.other_id = other_id


@dataclass
class SplitResult:
    """一次拆分的结果。"""

    chunk: Chunk        # 新分片（后半段）
    source: Chunk       # 被拆的分片（前半段，区间已缩短）
    split_at: int       # 拆点的绝对偏移
    was_inflight: bool  # 被拆的分片当时是否正在传输


class DynamicPlanner:
    """下载过程中持续变化的分片布局（规格 §1.2）。

    **它替代的不是「分片」，是「一次规划」。** 布局的起点很粗
    （默认 2 片），每来一个空闲连接就把当前**最大的未完成分片**
    对半拆开，于是分片数随着连接数一起长。

    为什么敢拆**在途**的分片：那一条 HTTP 请求的 Range 头已经发出
    去了，服务器还会继续发字节。拆开之后旧分片的区间缩短，写入
    时按新上限截断（见 `FileAssembler.append_limited`），超出的字节
    丢掉——最多丢一个数据块。换来的是真正多了一条连接在下载。
    """

    def __init__(
        self,
        strategy: RangeStrategy | None = None,
        content_length: int = 0,
        initial_chunks: int | None = None,
        target_chunks: int | None = None,
    ) -> None:
        self.strategy = strategy or RangeStrategy()
        self.content_length = content_length
        self.target = max(1, min(
            target_chunks if target_chunks is not None else self.strategy.default_chunks,
            self.strategy.max_chunks,
        ))
        initial = initial_chunks if initial_chunks is not None else self.strategy.initial_chunks
        initial = max(1, min(initial, self.target))
        self._chunks: dict[int, Chunk] = {}
        self._pending: deque[int] = deque()
        self._next_id = 0
        self._inflight: set[int] = set()
        self._lock = threading.RLock()
        self._seed(initial)

    # -- 建立 --------------------------------------------------------

    def _seed(self, count: int) -> None:
        """初始布局：把整个文件均分成 `count` 片。"""
        length = max(0, self.content_length)
        if length <= 0:
            return
        count = max(1, min(count, length))
        base = length // count
        start = 0
        for i in range(count):
            end = length if i == count - 1 else start + base
            self._chunks[self._next_id] = Chunk(id=self._next_id, start=start, end=end)
            self._pending.append(self._next_id)
            start = end
            self._next_id += 1

    def restore(self, chunks: list[Chunk]) -> None:
        """用会话文件里的布局恢复（续传）。

        恢复出来的分片**全部进待办队列**——已完成的部分靠
        `bytes_done` 跳过，没完成的重新排队。
        """
        with self._lock:
            self._chunks.clear()
            self._pending.clear()
            self._inflight.clear()
            self._next_id = 0
            for chunk in chunks:
                self._chunks[chunk.id] = chunk
                if not chunk.complete:
                    self._pending.append(chunk.id)
                self._next_id = max(self._next_id, chunk.id + 1)

    # -- 取分片 ------------------------------------------------------

    def take_pending(self) -> Chunk | None:
        """取一个待下载的分片。没有就返回 None。"""
        with self._lock:
            while self._pending:
                chunk_id = self._pending.popleft()
                chunk = self._chunks.get(chunk_id)
                if chunk is None or chunk.complete or chunk_id in self._inflight:
                    continue
                return chunk
            return None

    def split_largest(self) -> SplitResult | None:
        """把当前**最大的未完成分片**对半拆开。

        规格 §1.2：「空闲连接到来时，找到当前最大的未完成分片，
        将其对半拆分，新连接从拆分点继续下载。」

        拆点：
        - 分片**在途** → 取「已写位置到末尾」的中点。旧连接继续写
          到拆点为止，新连接从拆点接着往末尾下。已写的字节不会白下。
        - 分片**空闲** → 取整体中点。

        拆不动的情况：分片数已达目标、剩下的不够再拆一半（小于
        两倍最小分片大小）、或者没有未完成的分片。
        """
        with self._lock:
            if len(self._chunks) >= self.target:
                return None
            source = self._pick_split_target()
            if source is None:
                return None

            inflight = source.id in self._inflight
            if inflight:
                low = source.write_position
            else:
                low = source.start
            remaining = source.end - low
            if remaining < 2 * self.strategy.min_chunk_size:
                return None

            mid = (low + source.end) // 2
            if mid <= source.start or mid >= source.end:
                return None

            old_end = source.end
            source.end = mid
            new_chunk = Chunk(id=self._next_id, start=mid, end=old_end, bytes_done=0)
            self._next_id += 1
            self._chunks[new_chunk.id] = new_chunk
            self._pending.append(new_chunk.id)
            return SplitResult(
                chunk=new_chunk,
                source=source,
                split_at=mid,
                was_inflight=inflight,
            )

    def _pick_split_target(self) -> Chunk | None:
        """挑「最大的未完成分片」——按剩余字节数比。"""
        best: Chunk | None = None
        best_remaining = 0
        for chunk in self._chunks.values():
            if chunk.complete:
                continue
            remaining = chunk.remaining
            if remaining > best_remaining:
                best = chunk
                best_remaining = remaining
        return best

    # -- 状态 --------------------------------------------------------

    def mark_inflight(self, chunk_id: int) -> None:
        with self._lock:
            self._inflight.add(chunk_id)

    def mark_finished(self, chunk_id: int) -> None:
        with self._lock:
            self._inflight.discard(chunk_id)

    def update_progress(self, chunk_id: int, delta: int) -> None:
        """累计分片已下载字节。

        拆分在途分片时要靠它算拆点，所以必须**及时**更新——
        差得太多，拆点就可能落在已经写过的位置上。
        """
        with self._lock:
            chunk = self._chunks.get(chunk_id)
            if chunk is None:
                return
            chunk.bytes_done = min(chunk.size, chunk.bytes_done + delta)

    def chunks(self) -> list[Chunk]:
        with self._lock:
            return list(self._chunks.values())

    def chunk(self, chunk_id: int) -> Chunk | None:
        with self._lock:
            return self._chunks.get(chunk_id)

    @property
    def chunk_count(self) -> int:
        with self._lock:
            return len(self._chunks)

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def bytes_done(self) -> int:
        with self._lock:
            return sum(c.bytes_done for c in self._chunks.values())

    def all_complete(self) -> bool:
        with self._lock:
            return all(c.complete for c in self._chunks.values())


class ConnectionGovernor:
    """并发连接数的调节器（规格 §1.2.3）。

    **初始连接数保守**（默认 2），随下载推进逐步放开；出现错误就
    收回来——服务器在报错的时候继续加连接只会让它更糟。

    实现上不是信号量（信号量不能缩容），而是一个「当前允许几个
    同时下载」的计数器 + 一个唤醒事件。
    """

    def __init__(self, initial: int = 2, maximum: int = 32) -> None:
        self.maximum = max(1, maximum)
        self._limit = max(1, min(initial, self.maximum))
        self._running = 0
        self._changed: asyncio.Event | None = None

    def bind_loop(self, loop) -> None:
        """绑定事件循环并建好唤醒事件。

        `asyncio.Event` 在 3.10 之后不再绑定创建时的循环，所以要
        在**运行中的那个循环里**建，不能在 `__init__` 里建。
        """
        self._changed = asyncio.Event()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def running(self) -> int:
        return self._running

    def set_limit(self, value: int) -> None:
        new = max(1, min(self.maximum, value))
        if new == self._limit:
            return
        self._limit = new
        self._wake()

    def _wake(self) -> None:
        if self._changed is None:
            return
        try:
            self._changed.set()
        except Exception:
            pass

    async def acquire(self) -> None:
        """等一个下载名额。"""
        while True:
            if self._running < self._limit:
                self._running += 1
                return
            if self._changed is not None:
                self._changed.clear()
                try:
                    await self._changed.wait()
                except Exception:
                    await _sleep(0.05)
            else:
                await _sleep(0.02)
    def release(self) -> None:
        self._running = max(0, self._running - 1)
        self._wake()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RangePlanner:
    """持有分片 ID 并根据速度采样给出分裂/合并建议。

    规划本身是纯逻辑，实际的移动由引擎在锁内执行——保持规划器可单测。

    **注意：这个类只服务「按速度重划分」的旧策略，下载流程已经不用
    它了**（`DynamicPlanner` 接了规格 §1.2 的位置）。留着是因为
    Swift 版的语义对照还在用它，测试也要锁住那些行为。
    """

    def __init__(self, strategy: RangeStrategy | None = None) -> None:
        self.strategy = strategy or RangeStrategy()
        self._chunks: list[Chunk] = []
        self._next_id = 0
        self._started_at = time.monotonic()
        self._last_rearrange = 0.0
        self._lock = threading.RLock()

    # -- 计划 --------------------------------------------------------

    def start_plan(self, content_length: int, chunk_count: int | None = None) -> None:
        """规划分片。

        `chunk_count` 给了就用它，否则按「策略期望片大小」算：
        片数 = min(默认分片数, 文件大小 / 期望片大小)。
        传 0 结果是 1 片（整个文件当一片）。
        """
        with self._lock:
            if self._chunks:
                raise RuntimeError("plan already started")
            if chunk_count is not None:
                n = max(1, min(self.strategy.max_chunks, chunk_count))
            else:
                target = min(
                    self.strategy.default_chunks,
                    int(content_length / max(1, self.strategy.initial_chunk_size)),
                )
                n = max(1, target)
            base = content_length // n
            start = 0
            for i in range(n):
                end = content_length if i == n - 1 else start + base
                self._chunks.append(Chunk(id=self._next_id, start=start, end=end))
                start = end
                self._next_id += 1

    def all_chunks(self) -> list[Chunk]:
        with self._lock:
            return list(self._chunks)

    def replace_chunks(self, new_chunks: list[Chunk]) -> None:
        """整体替换计划，但保持分片数量不变。

        数量必须一致——正在跑的工作单元引用的是同一批 id，
        重新编号会让在途的下载失去归属。改了布局就得重启下载。
        """
        with self._lock:
            if len(new_chunks) != len(self._chunks):
                raise ValueError("replace_chunks: chunk count must match")
            self._chunks = list(new_chunks)

    def consume(self, chunk_id: int, bytes_count: int) -> None:
        with self._lock:
            for chunk in self._chunks:
                if chunk.id == chunk_id:
                    chunk.bytes_done = min(chunk.end, chunk.bytes_done + bytes_count)
                    return

    def complete(self, chunk_id: int) -> None:
        with self._lock:
            for chunk in self._chunks:
                if chunk.id == chunk_id:
                    chunk.bytes_done = chunk.end
                    return

    # -- 重划分 ------------------------------------------------------

    def rearrange(self, samples: list[SpeedSample], now: float | None = None) -> list[RearrangeDelta]:
        """根据速度采样给出分裂/合并建议。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            if now - self._started_at < self.strategy.first_rearrange_after:
                return []
            if now - self._last_rearrange < 1.0:
                return []

            by_id = {s.chunk_id: s.bytes_per_sec for s in samples}
            max_speed = max(by_id.values()) if by_id else 0.0
            if max_speed <= 0:
                return []

            deltas: list[RearrangeDelta] = []
            used: set[int] = set()

            # 先处理慢片——分裂会把工作单元从池子里短暂拿走，
            # 所以慢片和快片都需要照顾时，要先把工作单元让给快片。
            ordered = sorted(
                self._chunks,
                key=lambda c: by_id.get(c.id, float("inf")),
            )

            for chunk in ordered:
                if chunk.id in used:
                    continue
                speed = by_id.get(chunk.id)
                if speed is None:
                    continue
                ratio = speed / max_speed

                # 慢片：尝试和相邻慢片合并
                if ratio < self.strategy.merge_threshold and chunk.size < self.strategy.max_chunk_size:
                    idx = self._chunks.index(chunk)
                    neighbours: list[Chunk] = []
                    if idx > 0:
                        neighbours.append(self._chunks[idx - 1])
                    if idx + 1 < len(self._chunks):
                        neighbours.append(self._chunks[idx + 1])
                    partner = next(
                        (
                            n
                            for n in neighbours
                            if n.id not in used
                            and by_id.get(n.id, 0) < max_speed * (self.strategy.merge_threshold + 0.2)
                            and (chunk.size + n.size) <= self.strategy.max_chunk_size
                        ),
                        None,
                    )
                    if partner is not None:
                        used.add(chunk.id)
                        used.add(partner.id)
                        deltas.append(RearrangeDelta("merge", chunk.id, partner.id))
                        continue

                # 快片相对慢片优势明显 → 分裂慢片，把慢片拆小重新分配
                if (
                    len(self._chunks) < self.strategy.max_chunks
                    and chunk.size >= 4 * 1024 * 1024
                    and ratio < (1.0 / self.strategy.split_threshold)
                ):
                    used.add(chunk.id)
                    deltas.append(RearrangeDelta("split", chunk.id))

            if deltas:
                self._last_rearrange = now
            return deltas

    def apply(self, delta: RearrangeDelta) -> None:
        """执行一次重划分。"""
        with self._lock:
            if delta.kind == "split":
                idx = next((i for i, c in enumerate(self._chunks) if c.id == delta.chunk_id), None)
                if idx is None:
                    return
                chunk = self._chunks.pop(idx)
                mid = (chunk.start + chunk.end) // 2
                left = Chunk(id=self._next_id, start=chunk.start, end=mid, bytes_done=chunk.bytes_done)
                right = Chunk(id=self._next_id + 1, start=mid, end=chunk.end, bytes_done=chunk.bytes_done)
                self._chunks.insert(idx, left)
                self._chunks.insert(idx + 1, right)
                self._next_id += 2
            elif delta.kind == "merge":
                ai = next((i for i, c in enumerate(self._chunks) if c.id == delta.chunk_id), None)
                bi = next((i for i, c in enumerate(self._chunks) if c.id == delta.other_id), None)
                if ai is None or bi is None:
                    return
                lo, hi = (ai, bi) if ai < bi else (bi, ai)
                c1, c2 = self._chunks[lo], self._chunks[hi]
                merged = Chunk(
                    id=self._next_id,
                    start=min(c1.start, c2.start),
                    end=max(c1.end, c2.end),
                    bytes_done=c1.bytes_done + c2.bytes_done,
                )
                self._chunks.pop(hi)
                self._chunks.pop(lo)
                self._chunks.insert(lo, merged)
                self._next_id += 1
