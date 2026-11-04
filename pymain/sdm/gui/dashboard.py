"""仪表盘统计——纯计算，不碰 GUI。

给 `sdm.gui.bridge.GuiBridge.get_dashboard_stats()` 用：它把引擎
快照和历史条目揉成一张统计表，前端只管画。

**为什么单独一个模块**：统计口径（今日怎么算、累计怎么算、分类
怎么归）是很容易改的东西，混在桥接层里改一次就要重新起 GUI 才能
看见效果。抽成纯函数之后，`tests/test_dashboard.py` 直接调它，
不用 GUI、不用 webview。

**去重口径**：历史条目的 id 就是任务 id（`HistoryEntry.id` 直接
取自任务摘要），同一个下载会同时出现在「引擎快照」和「历史」里。
统计前先按 id 合并——不然每个下载会被数两遍。

口径说明（都写在函数 docstring 里，改的时候一起改）：
  * 活跃 = state 为 downloading 的任务（含 connecting 那种「已经
    开跑但还没进字节」的）。
  * 今日 = 本地时区今天 0 点之后完成（finished_at）的下载。
  * 累计体积 = 已完成的任务按 total_bytes 算，其余按 bytes_done 算
    （排队中还没下过一个字节的任务不进累计）。
  * 分类 = 任务自带 category 的用自带的；历史条目没有 category，
    按文件名后缀推断，猜不出来归「其他」。
"""

from __future__ import annotations

import time
from datetime import datetime

from ..engine.categories import DownloadCategory, category_for_filename

#: 算「活跃」的状态。queued 不算——它还没开始下。
_ACTIVE_STATES = frozenset({"downloading"})

#: 算「累计体积」时，只有完成的任务才按总字节算。
_COMPLETED_STATE = "completed"


def _bytes_of(entry: dict) -> int:
    """一个条目占了多少字节。

    总字节优先（total_bytes），没有总字节就用已下载字节。
    """
    total = entry.get("total_bytes")
    if total:
        try:
            return max(0, int(total))
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(entry.get("bytes_done") or 0))
    except (TypeError, ValueError):
        return 0


def _downloaded_bytes(entry: dict) -> int:
    """这个条目**实际下载了**多少字节。

    已完成的按总字节算；没完成的只能按已下载字节算——一个排队中
    的 2 GB 任务不能算进「累计下载体积」，它一个字节都还没下。
    """
    if entry.get("state") == _COMPLETED_STATE:
        return _bytes_of(entry)
    try:
        return max(0, int(entry.get("bytes_done") or 0))
    except (TypeError, ValueError):
        return 0


def _category_of(entry: dict) -> str:
    """条目属于哪个分类。

    任务自带 category 就用自带的；历史条目没有这个字段，按文件名
    后缀猜（`sdm.engine.categories` 的扩展名表），猜不出来归「其他」。
    """
    category = entry.get("category")
    if isinstance(category, str) and category:
        return category
    guess = category_for_filename(str(entry.get("filename") or ""))
    return guess.value if guess else DownloadCategory.OTHER.value


def _today_start(now: float | None = None) -> float:
    """本地时区今天 0 点的 unix 时间戳。"""
    moment = datetime.fromtimestamp(now if now is not None else time.time()).astimezone()
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _merge_entries(tasks: list[dict] | None, history: list[dict] | None) -> dict[str, dict]:
    """把任务快照和历史按 id 合并成一份。

    同 id 的条目：任务的数据更新（有 category、有 speed），历史的
    数据更全（finished_at 一定填了）。所以先放历史，再用任务覆盖，
    但任务里缺失的键保留历史的值。
    """
    merged: dict[str, dict] = {}

    for index, entry in enumerate(history or []):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id") or f"history-{index}")
        merged[key] = dict(entry)

    for index, entry in enumerate(tasks or []):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("id") or f"task-{index}")
        combined = dict(merged.get(key, {}))
        combined.update(entry)
        merged[key] = combined

    return merged


def build_dashboard(tasks: list[dict], history: list[dict]) -> dict:
    """算仪表盘要用的统计。

    参数
    ----
    tasks:
        引擎快照（`task.to_dict()` 的结果）。每项至少有 id / state /
        bytes_done / total_bytes / speed_bytes_per_sec / category。
    history:
        历史条目（`HistoryEntry.to_dict()` 的结果）。**没有 category
        字段**，分类按文件名后缀推断。

    返回
    ----
    ```python
    {
        "ok": True,
        "active": {"count": 3, "speed": 1048576.0},
        "today": {"downloads": 2, "bytes": 12345678},
        "totals": {
            "downloads": 42, "bytes": 987654321, "failed": 1,
            "by_category": [{"id": "video", "count": 5, "bytes": 123}],
        },
        "recent": [
            {"id": "...", "filename": "a.mp4", "category": "video",
             "bytes": 123, "state": "completed", "finished_at": 1730000000.0},
        ],
    }
    ```
    """
    task_list = [entry for entry in (tasks or []) if isinstance(entry, dict)]
    history_list = [entry for entry in (history or []) if isinstance(entry, dict)]
    merged = _merge_entries(task_list, history_list)

    # -- 活跃任务：速度只算真的在下的 --------------------------------
    active_count = 0
    speed = 0.0
    for entry in task_list:
        if entry.get("state") not in _ACTIVE_STATES:
            continue
        active_count += 1
        try:
            speed += float(entry.get("speed_bytes_per_sec") or 0.0)
        except (TypeError, ValueError):
            pass

    # -- 今日完成 ---------------------------------------------------
    today_start = _today_start()
    today_downloads = 0
    today_bytes = 0
    for entry in merged.values():
        if entry.get("state") != _COMPLETED_STATE:
            continue
        finished_at = entry.get("finished_at")
        if not finished_at or float(finished_at) < today_start:
            continue
        today_downloads += 1
        today_bytes += _bytes_of(entry)

    # -- 累计 -------------------------------------------------------
    total_downloads = len(merged)
    total_bytes = sum(_downloaded_bytes(entry) for entry in merged.values())
    failed = sum(1 for entry in merged.values() if entry.get("state") == "failed")

    # -- 按分类统计 -------------------------------------------------
    buckets: dict[str, dict] = {}
    for entry in merged.values():
        category = _category_of(entry)
        bucket = buckets.setdefault(category, {"id": category, "count": 0, "bytes": 0})
        bucket["count"] += 1
        bucket["bytes"] += _downloaded_bytes(entry)
    by_category = sorted(
        buckets.values(),
        key=lambda item: (-item["bytes"], -item["count"], item["id"]),
    )

    # -- 最近完成（最多 8 条） ---------------------------------------
    recent_entries = [
        entry
        for entry in merged.values()
        if entry.get("state") == _COMPLETED_STATE and entry.get("finished_at")
    ]
    recent_entries.sort(key=lambda entry: float(entry.get("finished_at") or 0), reverse=True)
    recent = [
        {
            "id": str(entry.get("id") or ""),
            "filename": str(entry.get("filename") or ""),
            "category": _category_of(entry),
            "bytes": _bytes_of(entry),
            "state": _COMPLETED_STATE,
            "finished_at": float(entry.get("finished_at") or 0),
        }
        for entry in recent_entries[:8]
    ]

    return {
        "ok": True,
        "active": {"count": active_count, "speed": speed},
        "today": {"downloads": today_downloads, "bytes": today_bytes},
        "totals": {
            "downloads": total_downloads,
            "bytes": total_bytes,
            "failed": failed,
            "by_category": by_category,
        },
        "recent": recent,
    }
