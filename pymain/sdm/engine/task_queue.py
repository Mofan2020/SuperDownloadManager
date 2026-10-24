"""任务队列：决定并发槽位怎么分。

对应 Swift 版 Engine/TaskQueue.swift。

策略是「专用带宽」：用户通常只想把一个文件快点下完，所以
单个活动任务独占并发预算；第二个任务只有在第一个跑满带宽
或被暂停时才分到槽位。

区别于 Swift 版的是这里保留至少 4 个槽位给后来的任务——
Swift 版分到 `max(4, cap/(n+1))`，逻辑一样。
"""

from __future__ import annotations

import threading


class TaskQueue:
    """并发槽位分配。"""

    def __init__(self, parallelism_cap: int = 32) -> None:
        self.parallelism_cap = parallelism_cap
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}  # task_id -> state

    def register(self, task_id: str, state: str) -> None:
        with self._lock:
            self._active[task_id] = state

    def update_state(self, task_id: str, state: str) -> None:
        with self._lock:
            self._active[task_id] = state

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._active.pop(task_id, None)

    def available_slots(self, task_id: str) -> int:
        """这个任务现在能分到几个并发槽。"""
        with self._lock:
            others = sum(
                1
                for tid, state in self._active.items()
                if tid != task_id and state == "downloading"
            )
        if others == 0:
            return self.parallelism_cap
        return max(4, self.parallelism_cap // (others + 1))
