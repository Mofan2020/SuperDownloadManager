"""时间窗口限速策略。

对应 Swift 版 Engine/SpeedPolicy.swift。

**Swift 版这块是死代码**（`SpeedPolicy` 从来没被调用过），
所以这里既实现了它，也把它真正接到了引擎上——不然就又造一个死代码。

模型：一组 `(from, to, bytesPerSec)` 时间窗口。取第一个覆盖
「当前时刻」的窗口；没有窗口匹配时用全局上限。

所以用户可以这样配：
  · 工作时间不限速（没有窗口匹配 → 用全局上限）
  · 夜间压到 5 MB/s
  · 18-22 点压到 1 MB/s（孩子睡觉时间）
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class SpeedRule:
    """一条时间窗口规则。from/to 是「一天中的第几分钟」。"""

    from_minute: int   # 0..1439
    to_minute: int     # 开区间；可以小于 from（跨午夜）
    bytes_per_sec: float

    def __init__(self, from_minute: int, to_minute: int, bytes_per_sec: float) -> None:
        self.from_minute = max(0, min(1439, from_minute))
        self.to_minute = max(0, min(1440, to_minute))
        self.bytes_per_sec = max(0.0, bytes_per_sec)

    def covers(self, minute: int) -> bool:
        """这个窗口是否覆盖给定的「一天中的第几分钟」。"""
        if self.from_minute < self.to_minute:
            return self.from_minute <= minute < self.to_minute
        # 跨午夜，例如 22:00–06:00
        return minute >= self.from_minute or minute < self.to_minute

    def to_dict(self) -> dict:
        return {
            "from_minute": self.from_minute,
            "to_minute": self.to_minute,
            "bytes_per_sec": self.bytes_per_sec,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SpeedRule":
        return cls(
            int(data.get("from_minute", 0)),
            int(data.get("to_minute", 0)),
            float(data.get("bytes_per_sec", 0)),
        )

    @property
    def label(self) -> str:
        """人类可读的时段描述。"""
        def hhmm(m: int) -> str:
            return f"{m // 60:02d}:{m % 60:02d}"
        return f"{hhmm(self.from_minute)}–{hhmm(self.to_minute)}"


class SpeedPolicy:
    """按时间窗口决定当前生效的速率。"""

    def __init__(self, rules: list[SpeedRule] | None = None, ceiling: float = 0.0) -> None:
        self._rules = list(rules or [])
        self._ceiling = max(0.0, ceiling)
        self._lock = threading.Lock()

    @property
    def rules(self) -> list[SpeedRule]:
        with self._lock:
            return list(self._rules)

    @property
    def ceiling(self) -> float:
        with self._lock:
            return self._ceiling

    def add(self, rule: SpeedRule) -> None:
        with self._lock:
            self._rules.append(rule)

    def remove(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._rules):
                self._rules.pop(index)

    def set_ceiling(self, bytes_per_sec: float) -> None:
        with self._lock:
            self._ceiling = max(0.0, bytes_per_sec)

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()

    def effective_rate(self, at: float | None = None) -> float:
        """当前生效的速率。0 表示不限速。"""
        at = time.time() if at is None else at
        minute = _minute_of_day(at)
        with self._lock:
            rules = list(self._rules)
            ceiling = self._ceiling
        for rule in rules:
            if rule.covers(minute):
                return rule.bytes_per_sec
        return ceiling

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "rules": [r.to_dict() for r in self._rules],
                "ceiling": self._ceiling,
            }

    @classmethod
    def from_dict(cls, data: dict) -> "SpeedPolicy":
        return cls(
            [SpeedRule.from_dict(r) for r in data.get("rules", []) if isinstance(r, dict)],
            float(data.get("ceiling", 0.0)),
        )


def _minute_of_day(timestamp: float) -> int:
    """时间戳 → 一天中的第几分钟。用本地时区。"""
    lt = time.localtime(timestamp)
    return lt.tm_hour * 60 + lt.tm_min


def minute_to_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def hhmm_to_minute(text: str) -> int:
    """'22:30' → 1350。解析失败返回 0。"""
    try:
        hh, _, mm = text.strip().partition(":")
        return max(0, min(1439, int(hh) * 60 + int(mm)))
    except (ValueError, AttributeError):
        return 0
