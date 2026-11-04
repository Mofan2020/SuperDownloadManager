"""仪表盘统计的单元测试。

`build_dashboard` 是纯函数——不依赖 GUI、不依赖 webview，
所以这里直接调它就能验口径。

盯的是几件容易算错的事：
  * 同一个下载在「任务」和「历史」里各出现一次时，只能算一次。
  * 「今日」按本地时区的今天 0 点算，不是 UTC。
  * 排队中一个字节都没下的任务，不进「累计下载体积」。
  * 历史条目没有 category，分类得按文件名后缀推断。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from sdm.gui.dashboard import build_dashboard


def _local_midnight(now: float) -> float:
    moment = datetime.fromtimestamp(now).astimezone()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _task(**kwargs) -> dict:
    base = {
        "id": "t1",
        "url": "https://example.com/a.mp4",
        "filename": "a.mp4",
        "category": "video",
        "state": "downloading",
        "bytes_done": 0,
        "total_bytes": 100,
        "speed_bytes_per_sec": 0.0,
        "finished_at": None,
    }
    base.update(kwargs)
    return base


def _history(**kwargs) -> dict:
    base = {
        "id": "h1",
        "url": "https://example.com/a.zip",
        "filename": "a.zip",
        "total_bytes": 200,
        "bytes_done": 200,
        "state": "completed",
        "finished_at": time.time(),
    }
    base.update(kwargs)
    return base


# -- 空输入 ----------------------------------------------------------


def test_empty_input_returns_zeroed_structure() -> None:
    result = build_dashboard([], [])
    assert result["ok"] is True
    assert result["active"] == {"count": 0, "speed": 0.0}
    assert result["today"] == {"downloads": 0, "bytes": 0}
    assert result["totals"]["downloads"] == 0
    assert result["totals"]["bytes"] == 0
    assert result["totals"]["failed"] == 0
    assert result["totals"]["by_category"] == []
    assert result["recent"] == []


# -- 活跃任务 --------------------------------------------------------


def test_active_counts_downloading_and_sums_speed() -> None:
    tasks = [
        _task(id="a", state="downloading", speed_bytes_per_sec=1024.0),
        _task(id="b", state="downloading", speed_bytes_per_sec=512.0),
        _task(id="c", state="queued", speed_bytes_per_sec=999.0),
        _task(id="d", state="paused", speed_bytes_per_sec=888.0),
    ]
    result = build_dashboard(tasks, [])
    assert result["active"]["count"] == 2
    assert result["active"]["speed"] == pytest.approx(1536.0)


def test_active_ignores_missing_speed_field() -> None:
    task = _task(id="a", state="downloading")
    task.pop("speed_bytes_per_sec")
    result = build_dashboard([task], [])
    assert result["active"]["count"] == 1
    assert result["active"]["speed"] == 0.0


# -- 今日 ------------------------------------------------------------


def test_today_counts_only_finished_after_local_midnight() -> None:
    now = time.time()
    midnight = _local_midnight(now)
    yesterday = midnight - 1

    tasks = [
        _task(id="a", state="completed", total_bytes=100, finished_at=now),
        _task(id="b", state="completed", total_bytes=50, finished_at=yesterday),
        _task(id="c", state="downloading", total_bytes=70, finished_at=None),
    ]
    result = build_dashboard(tasks, [])
    assert result["today"]["downloads"] == 1
    assert result["today"]["bytes"] == 100


def test_today_uses_local_timezone_not_utc() -> None:
    """本地时区今天 0 点之后的完成记录算「今日」。"""
    now = time.time()
    midnight = _local_midnight(now)
    # 本地 0 点之后 10 秒 —— UTC 下可能是昨天。
    result = build_dashboard(
        [_task(id="a", state="completed", total_bytes=10, finished_at=midnight + 10)],
        [],
    )
    assert result["today"]["downloads"] == 1


# -- 累计与去重 ------------------------------------------------------


def test_same_id_in_tasks_and_history_counted_once() -> None:
    """同一个下载同时在任务和历史里（引擎快照 + 历史表），只能算一次。"""
    now = time.time()
    tasks = [_task(id="same", state="completed", total_bytes=500, finished_at=now)]
    history = [_history(id="same", state="completed", total_bytes=500, finished_at=now)]
    result = build_dashboard(tasks, history)
    assert result["totals"]["downloads"] == 1
    assert result["totals"]["bytes"] == 500
    assert result["today"]["downloads"] == 1


def test_queued_task_bytes_not_counted_in_total_volume() -> None:
    """排队中一个字节都没下的任务，不算进累计下载体积。"""
    tasks = [_task(id="a", state="queued", bytes_done=0, total_bytes=2_000_000_000)]
    result = build_dashboard(tasks, [])
    assert result["totals"]["bytes"] == 0
    assert result["totals"]["downloads"] == 1


def test_partial_download_counts_only_downloaded_bytes() -> None:
    tasks = [_task(id="a", state="downloading", bytes_done=300, total_bytes=1000)]
    result = build_dashboard(tasks, [])
    assert result["totals"]["bytes"] == 300


def test_failed_count_includes_history_failures() -> None:
    tasks = [_task(id="a", state="failed", bytes_done=10, total_bytes=100, finished_at=time.time())]
    history = [_history(id="h2", state="failed", bytes_done=0, total_bytes=100)]
    result = build_dashboard(tasks, history)
    assert result["totals"]["failed"] == 2


# -- 分类统计 --------------------------------------------------------


def test_by_category_uses_task_category() -> None:
    tasks = [
        _task(id="a", category="video", state="completed", total_bytes=100, finished_at=time.time()),
        _task(id="b", category="video", state="completed", total_bytes=50, finished_at=time.time()),
        _task(id="c", category="music", state="completed", total_bytes=10, finished_at=time.time()),
    ]
    result = build_dashboard(tasks, [])
    buckets = {item["id"]: item for item in result["totals"]["by_category"]}
    assert buckets["video"]["count"] == 2
    assert buckets["video"]["bytes"] == 150
    assert buckets["music"]["count"] == 1


def test_by_category_sorted_by_bytes_desc() -> None:
    tasks = [
        _task(id="a", category="music", state="completed", total_bytes=10, finished_at=time.time()),
        _task(id="b", category="video", state="completed", total_bytes=999, finished_at=time.time()),
    ]
    result = build_dashboard(tasks, [])
    assert [item["id"] for item in result["totals"]["by_category"]] == ["video", "music"]


def test_history_category_inferred_from_filename() -> None:
    """历史条目没有 category 字段，靠文件名后缀推断。"""
    history = [
        _history(id="h1", filename="clip.mp4", state="completed", total_bytes=10, finished_at=time.time()),
        _history(id="h2", filename="song.flac", state="completed", total_bytes=10, finished_at=time.time()),
        _history(id="h3", filename="blob", state="completed", total_bytes=10, finished_at=time.time()),
    ]
    result = build_dashboard([], history)
    buckets = {item["id"]: item for item in result["totals"]["by_category"]}
    assert buckets["video"]["count"] == 1
    assert buckets["music"]["count"] == 1
    assert buckets["other"]["count"] == 1


# -- 最近完成 --------------------------------------------------------


def test_recent_lists_completed_desc_and_caps_at_eight() -> None:
    now = time.time()
    tasks = [
        _task(id=f"t{i}", state="completed", total_bytes=10, finished_at=now - i)
        for i in range(12)
    ]
    result = build_dashboard(tasks, [])
    assert len(result["recent"]) == 8
    finished = [item["finished_at"] for item in result["recent"]]
    assert finished == sorted(finished, reverse=True)


def test_recent_excludes_non_completed() -> None:
    now = time.time()
    tasks = [
        _task(id="a", state="failed", total_bytes=10, finished_at=now),
        _task(id="b", state="downloading", total_bytes=10, finished_at=None),
    ]
    result = build_dashboard(tasks, [])
    assert result["recent"] == []


def test_recent_entry_shape() -> None:
    now = time.time()
    tasks = [_task(id="x", filename="movie.mp4", category="video", state="completed",
                   total_bytes=1234, finished_at=now)]
    entry = build_dashboard(tasks, [])["recent"][0]
    assert entry["id"] == "x"
    assert entry["filename"] == "movie.mp4"
    assert entry["category"] == "video"
    assert entry["bytes"] == 1234
    assert entry["state"] == "completed"
    assert entry["finished_at"] == pytest.approx(now)


# -- 健壮性 ----------------------------------------------------------


def test_none_inputs_do_not_crash() -> None:
    result = build_dashboard(None, None)  # type: ignore[arg-type]
    assert result["totals"]["downloads"] == 0


def test_malformed_entries_are_ignored() -> None:
    result = build_dashboard(["not a dict", _task(id="a")], [None])  # type: ignore[list-item]
    assert result["totals"]["downloads"] == 1
