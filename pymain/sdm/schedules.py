"""计划任务：定时启动下载。

对应 Swift 版 App/MainWindow/ScheduleView.swift。

Swift 版把计划任务存在 view 的 @State 里——窗口一关就没了，
而且换个标签页重建视图也会丢。这里存成 JSON 文件，重启还在。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import paths
from .logging_setup import CAT_STORE, get_logger

log = get_logger()


@dataclass
class ScheduledItem:
    id: str
    url: str
    fire_at: float
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledItem":
        return cls(
            id=data.get("id") or str(uuid.uuid4()),
            url=data.get("url", ""),
            fire_at=float(data.get("fire_at", 0)),
            note=data.get("note", "") or "",
        )

    def is_due(self, now: float | None = None) -> bool:
        return (now or time.time()) >= self.fire_at


class ScheduleStore:
    """计划任务的存储与调度。"""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.schedules_path()
        self._lock = threading.Lock()
        self._items: list[ScheduledItem] = []
        self._timer_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_fire: Callable[[ScheduledItem], None] | None = None
        self.load()

    # -- 读写 --------------------------------------------------------

    def load(self) -> list[ScheduledItem]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return self._items
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return self._items
        if isinstance(data, list):
            self._items = [ScheduledItem.from_dict(x) for x in data if isinstance(x, dict)]
        return self._items

    def save(self) -> None:
        with self._lock:
            payload = [item.to_dict() for item in self._items]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as err:
            log.error(CAT_STORE, f"计划任务保存失败: {err}")

    def all(self) -> list[ScheduledItem]:
        with self._lock:
            return list(self._items)

    def add(self, url: str, fire_at: float, note: str = "") -> ScheduledItem:
        item = ScheduledItem(id=str(uuid.uuid4()), url=url, fire_at=fire_at, note=note)
        with self._lock:
            self._items.append(item)
        self.save()
        return item

    def remove(self, item_id: str) -> None:
        with self._lock:
            self._items = [x for x in self._items if x.id != item_id]
        self.save()

    # -- 调度 --------------------------------------------------------

    def start(self, on_fire: Callable[[ScheduledItem], None]) -> None:
        """启动调度线程。"""
        if self._timer_thread is not None:
            return
        self._on_fire = on_fire
        self._stop.clear()
        self._timer_thread = threading.Thread(target=self._loop, name="sdm-schedule", daemon=True)
        self._timer_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=2.0)
            self._timer_thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            due = [item for item in self.all() if item.is_due(now)]
            for item in due:
                self.remove(item.id)
                if self._on_fire is not None:
                    try:
                        self._on_fire(item)
                    except Exception as err:  # noqa: BLE001
                        log.error(CAT_STORE, f"计划任务触发失败: {err}")
            self._stop.wait(1.0)
