"""速度显示：EMA 平滑 + 终态归零。

对应 Swift 版 Engine/SpeedDisplay.swift。

**这里锁住 Swift v1.0.8 的 bug：进度满了速度还在显示。**

v1.0.7 用 α=0.4 的 EMA 平滑每帧速度。下载到 100% 时引擎把
speedBytesPerSec 归零，但 EMA 缓存还在以 0.6·prev 的速度往 0 衰减，
所以用户会看到「进度条满了，速度还在显示 500 KB/s」，要过一秒
才掉下去。

修法：任务处于终态（完成/失败/取消）或暂停时，显示速度恒为 0，
并且把 EMA 缓存清掉——不管引擎报了什么。
"""

from __future__ import annotations

from dataclasses import dataclass

# 终态：显示速度恒为 0
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
# 暂停态：同样显示 0
PAUSED_STATES = frozenset({"paused", "awaiting_new_link"})


@dataclass
class SpeedInput:
    state: str
    engine_speed_bytes_per_sec: float
    delta_bytes_since_last_tick: int
    tick_interval_seconds: float


@dataclass
class SpeedOutput:
    displayed_speed: float
    next_ema: float


def compute(
    input_data: SpeedInput,
    previous_ema: float,
    alpha: float = 0.4,
) -> SpeedOutput:
    """算出本帧该显示的速度，以及下一帧的 EMA 缓存值。"""
    if input_data.state in TERMINAL_STATES or input_data.state in PAUSED_STATES:
        return SpeedOutput(displayed_speed=0.0, next_ema=0.0)

    if input_data.engine_speed_bytes_per_sec > 0:
        raw = input_data.engine_speed_bytes_per_sec
    elif input_data.tick_interval_seconds > 0:
        raw = input_data.delta_bytes_since_last_tick / input_data.tick_interval_seconds
    else:
        raw = 0.0

    if previous_ema == 0:
        next_ema = raw
    else:
        next_ema = alpha * raw + (1 - alpha) * previous_ema
    return SpeedOutput(displayed_speed=next_ema, next_ema=next_ema)


def format_rate(bytes_per_sec: float) -> str:
    """把字节/秒格式化成人类看得懂的字符串。"""
    if bytes_per_sec >= 1_048_576:
        return f"{bytes_per_sec / 1_048_576:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def format_duration(seconds: float) -> str:
    """秒数格式化成人类看得懂的时长。"""
    if seconds < 0:
        return ""
    if seconds < 60:
        return f"{seconds:.0f}s"
    total_min = int(seconds // 60)
    secs = int(seconds % 60)
    if total_min < 60:
        return f"{total_min}m {secs}s"
    hours = total_min // 60
    minutes = total_min % 60
    return f"{hours}h {minutes}m"


def format_size(num_bytes: int | float | None) -> str:
    """字节数格式化成人类看得懂的体积。"""
    if num_bytes is None:
        return "—"
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
