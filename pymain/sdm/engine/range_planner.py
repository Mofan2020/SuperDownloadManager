"""分片规划器。

对应 Swift 版 Engine/RangeManager.swift。

分片就是一串左闭右开的字节区间。分裂 = 去掉一个片，换成共享中点的
两片；合并 = 去掉相邻两片，换成它们的并集。速度采样用滑动窗口，
避免一个从下载开始就存在的慢片一直拖累整体。

Swift 版用 actor 串行化，这里用一个普通类加锁——规划操作都是纯计算，
锁的临界区没有 IO。
"""

from __future__ import annotations

import enum
import threading
import time
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
    default_chunks: int = 32                      # 每任务默认分片数
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


class RangePlanner:
    """持有分片 ID 并根据速度采样给出分裂/合并建议。

    规划本身是纯逻辑，实际的移动由引擎在锁内执行——保持规划器可单测。
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
