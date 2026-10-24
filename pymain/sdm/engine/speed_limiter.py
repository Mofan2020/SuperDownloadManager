"""令牌桶限速器。

对应 Swift 版 Engine/SpeedLimiter.swift。桶按 rate 速率补充，
consume(n) 在令牌不足时按比例 sleep。

与 Swift 版的区别：Swift 版是 actor（串行邮箱），这里用一把锁。
锁的临界区只有算术，不含 IO，不会成为瓶颈。
"""

from __future__ import annotations

import asyncio
import threading
import time


class SpeedLimiter:
    """令牌桶。rate <= 0 表示不限速。"""

    def __init__(self, rate_bytes_per_sec: float = 0.0, granularity: float = 0.1) -> None:
        self._rate = max(0.0, rate_bytes_per_sec)
        self._granularity = granularity
        self._capacity = max(self._rate * granularity, 1.0)
        self._tokens = self._capacity if self._rate > 0 else float("inf")
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    def set_rate(self, rate_bytes_per_sec: float) -> None:
        """运行时改速率。桶重置成新速率下的满桶，立即生效。"""
        r = max(0.0, rate_bytes_per_sec)
        with self._lock:
            self._rate = r
            self._capacity = max(r * self._granularity, 1.0)
            self._tokens = self._capacity if r > 0 else float("inf")
            self._last_refill = time.monotonic()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._rate > 0

    async def consume(self, n: int) -> float:
        """请求发送 n 字节的许可，不足则挂起等待。返回实际睡眠秒数。"""
        if not self.is_enabled():
            return 0.0
        slept = 0.0
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return slept
                needed = n - self._tokens
                sleep_sec = max(self._granularity, needed / self._rate)
            await asyncio.sleep(sleep_sec)
            slept += sleep_sec

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        added = self._rate * elapsed
        self._tokens = min(self._capacity, self._tokens + added)
        self._last_refill = now
