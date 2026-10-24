"""GUI 桥接层：Python 后端暴露给前端 JS 调用的方法。

对应 Swift 版 App/MainWindow/DownloadListStore.swift 的作用，
但在 Python 里前端是 HTML/JS，所以桥接层是 pywebview 的 api 对象。

**前端只通过这个类接触后端。** 所有方法返回 JSON 可序列化的
Python 对象；所有「主动推送」用 `_call_js()` 调前端全局函数。

命名规则：前端调 `window.pywebview.api.<方法名>()`。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import paths
from ..engine.categories import DownloadCategory, category_from_url
from ..engine.download_engine import DownloadEngine, StartSpec, TaskState
from ..history_store import HistoryStore
from ..i18n import I18n, available_languages
from ..logging_setup import CAT_GUI, get_logger
from ..notifications import notify
from ..schedules import ScheduleStore
from ..settings import SettingsStore
from ..site_rules import SiteRule, SiteRuleStore
from ..system_integration import open_file as sys_open_file
from ..system_integration import open_url as sys_open_url
from ..system_integration import reveal_in_finder as sys_reveal
from ..system_integration import set_open_at_login

log = get_logger()

# pywebview 只在 GUI 环境可用，延迟导入让非 GUI 环境也能 import 本模块。
try:  # pragma: no cover — 仅 GUI 环境
    import webview
except ImportError:  # pragma: no cover
    webview = None  # type: ignore[assignment]


class GuiBridge:
    """前端可调用的所有方法。"""

    def __init__(self, engine: DownloadEngine | None = None) -> None:
        self.engine = engine or DownloadEngine()
        self.settings_store = SettingsStore.shared()
        self.site_rules = SiteRuleStore.shared()
        self.schedules = ScheduleStore()
        self.history = HistoryStore()
        self._window: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._notified: dict[str, set[str]] = {"completed": set(), "failed": set()}
        self._clipboard_candidate: dict | None = None
        self._pending_dir: list[str] = []
        self._lock = threading.Lock()

    # -- 生命周期 ----------------------------------------------------

    def set_window(self, window: Any) -> None:
        self._window = window
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _call_js(self, func: str, payload: Any = None) -> None:
        """调前端的全局函数。"""
        if self._window is None:
            return
        try:
            args = json.dumps(payload, ensure_ascii=False)
            self._window.evaluate_js(f"{func}({args})")
        except Exception as err:
            log.debug(CAT_GUI, f"调用前端 {func} 失败: {err}")

    def _run(self, coro) -> None:
        """把协程丢到引擎的事件循环里跑。"""
        if self._loop is None:
            asyncio.run(coro)
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    # -- 启动时的初始化 ----------------------------------------------

    def bootstrap(self) -> dict:
        """前端加载完成后调用：返回初始状态。"""
        self.history.open()
        settings = self.settings_store.settings

        # 把设置推给引擎
        self._run(self.engine.set_speed_limit(float(settings.bandwidth_bytes_per_sec)))
        self.engine.set_max_threads(settings.threads_default)
        self.engine.set_ignore_ssl(settings.network_ignore_ssl)
        get_logger().update_settings(settings.log_settings())

        # 计划任务调度
        self.schedules.start(self._on_schedule_fire)

        return {
            "app": self.get_app_info(),
            "translations": I18n(settings.locale).catalog(),
            "language": settings.locale,
            "tasks": [t.to_dict() for t in self.engine.snapshot()],
            "settings": self.get_settings(),
            "languages": available_languages(),
            "categories": [c.value for c in DownloadCategory],
        }

    def _on_schedule_fire(self, item) -> None:
        """计划任务到点了。"""
        self._run(self.add_url(item.url))
        self._call_js("onScheduleFired", {"url": item.url})

    # -- 应用信息 ----------------------------------------------------

    def get_app_info(self) -> dict:
        from ..version import APP_DISPLAY_NAME, __version__

        return {
            "name": APP_DISPLAY_NAME,
            "version": __version__,
            "platform": "macOS" if paths.is_macos() else ("Windows" if paths.is_windows() else "Linux"),
            "download_dir": str(paths.download_dir()),
        }

    # -- 任务列表 ----------------------------------------------------

    def get_tasks(self) -> list[dict]:
        """任务列表。

        不带分片明细——分片有 32~128 项，每 700ms 传一次的话
        光序列化就是无谓开销。分片只在详情面板展开时单独取。
        """
        return [task.to_dict() for task in self.engine.snapshot()]

    def get_task_chunks(self, task_id: str) -> list[dict]:
        """单个任务的分片明细。详情面板用。"""
        return self.engine.get_chunks(task_id)

    def get_task(self, task_id: str) -> dict | None:
        task = self.engine.get(task_id)
        return task.to_dict(with_chunks=True) if task else None

    def add_url(
        self,
        url: str,
        filename: str | None = None,
        category: str | None = None,
        save_dir: str | None = None,
        max_threads: int | None = None,
    ) -> dict:
        """添加一个下载。"""
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "请输入有效的 http(s) 链接"}

        if save_dir:
            directory = Path(save_dir)
        else:
            base = paths.download_dir()
            cat = category_from_url(url)
            if category:
                from ..engine.categories import category_for_extension

                matched = category_for_extension(category)
                cat = matched or cat
            directory = base / cat.folder_name
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            return {"ok": False, "error": f"无法创建目录: {err}"}

        spec = StartSpec(
            url=url,
            destination_directory=directory,
            filename_override=filename or None,
            max_threads_override=max_threads if max_threads and max_threads > 0 else None,
        )
        ids = self._run(self.engine.start([spec]))
        return {"ok": True, "task_id": ids[0] if ids else ""}

    def pause_task(self, task_id: str) -> dict:
        self._run(self.engine.pause(task_id))
        return {"ok": True}

    def resume_task(self, task_id: str) -> dict:
        self._run(self.engine.resume(task_id))
        return {"ok": True}

    def cancel_task(self, task_id: str) -> dict:
        self._run(self.engine.cancel(task_id))
        return {"ok": True}

    def remove_task(self, task_id: str) -> dict:
        self._run(self.engine.remove(task_id))
        return {"ok": True}

    def clear_completed(self) -> dict:
        self._run(self.engine.clear_completed())
        return {"ok": True}

    def pause_all(self) -> dict:
        for task in self.engine.snapshot():
            if task.state in (TaskState.DOWNLOADING.value, TaskState.QUEUED.value):
                self._run(self.engine.pause(task.id))
        return {"ok": True}

    def resume_all(self) -> dict:
        for task in self.engine.snapshot():
            if task.state == TaskState.PAUSED.value:
                self._run(self.engine.resume(task.id))
        return {"ok": True}

    def retry_task(self, task_id: str) -> dict:
        """重试失败的任务。"""
        self._run(self.engine.resume(task_id))
        return {"ok": True}

    def set_task_speed_limit(self, task_id: str, bytes_per_sec: float) -> dict:
        self._run(self.engine.set_task_speed_limit(task_id, float(bytes_per_sec)))
        return {"ok": True}

    def relink_task(self, task_id: str, new_url: str) -> dict:
        """给等待新链接的任务换链接。"""
        result = self._run(self.engine.relink(task_id, new_url))
        return result

    # -- 文件操作 ----------------------------------------------------

    def open_file(self, path: str) -> dict:
        return {"ok": sys_open_file(path)}

    def reveal_in_finder(self, path: str) -> dict:
        return {"ok": sys_reveal(path)}

    def open_source_url(self, url: str) -> dict:
        return {"ok": sys_open_url(url)}

    def copy_to_clipboard(self, text: str) -> dict:
        """把文本写到系统剪贴板。"""
        try:
            if paths.is_macos():
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
            elif paths.is_windows():
                subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True, timeout=5)
            else:
                subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True, timeout=5)
            return {"ok": True}
        except Exception as err:
            return {"ok": False, "error": str(err)}

    def compute_checksum(self, path: str) -> dict:
        """算 SHA-256。前端显示结果。"""
        target = Path(path)
        if not target.exists():
            return {"ok": False, "error": "文件不存在"}
        try:
            digest = hashlib.sha256()
            with open(target, "rb") as fh:
                while True:
                    block = fh.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
            return {"ok": True, "sha256": digest.hexdigest()}
        except OSError as err:
            return {"ok": False, "error": str(err)}

    def delete_file(self, path: str) -> dict:
        """删除已完成的文件（移到废纸篓）。"""
        target = Path(path)
        if not target.exists():
            return {"ok": True}
        try:
            if paths.is_macos():
                subprocess.run(
                    ["osascript", "-e", f'tell app "Finder" to delete POSIX file "{target}"'],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            else:
                target.unlink()
            return {"ok": True}
        except Exception as err:
            return {"ok": False, "error": str(err)}

    # -- 设置 --------------------------------------------------------

    def get_settings(self) -> dict:
        return self.settings_store.settings.to_dict()

    def update_settings(self, changes: dict) -> dict:
        """改设置并立即生效。"""
        self.settings_store.update(changes)
        settings = self.settings_store.settings
        self._run(self.engine.set_speed_limit(float(settings.bandwidth_bytes_per_sec)))
        self.engine.set_max_threads(settings.threads_default)
        self.engine.set_ignore_ssl(settings.network_ignore_ssl)
        get_logger().update_settings(settings.log_settings())
        return {"ok": True, "settings": settings.to_dict()}

    def set_language(self, language: str) -> dict:
        self.settings_store.set("locale", language)
        return {"ok": True, "translations": I18n(language).catalog()}

    def set_open_at_login(self, enabled: bool) -> dict:
        result = set_open_at_login(enabled)
        self.settings_store.set("open_at_login", enabled)
        return {"ok": result}

    # -- 站点规则 ----------------------------------------------------

    def get_site_rules(self) -> list[dict]:
        return [rule.to_dict() for rule in self.site_rules.load()]

    def add_site_rule(
        self,
        host: str,
        referer: str = "",
        user_agent: str = "",
        cookies: str = "",
        basic_auth_user: str = "",
        basic_auth_password: str = "",
    ) -> dict:
        host = (host or "").strip().lower()
        if not host:
            return {"ok": False, "error": "主机名不能为空"}
        rule = SiteRule(
            id=str(uuid.uuid4()),
            host=host,
            referer=referer or None,
            user_agent=user_agent or None,
            cookies=cookies or None,
            basic_auth_user=basic_auth_user or None,
        )
        self.site_rules.add(rule, basic_auth_password or None)
        return {"ok": True, "rules": self.get_site_rules()}

    def remove_site_rule(self, rule_id: str) -> dict:
        self.site_rules.remove(rule_id)
        return {"ok": True, "rules": self.get_site_rules()}

    def clear_site_rules(self) -> dict:
        self.site_rules.clear_all()
        return {"ok": True, "rules": []}

    # -- 计划任务 ----------------------------------------------------

    def get_schedules(self) -> list[dict]:
        return [item.to_dict() for item in self.schedules.all()]

    def add_schedule(self, url: str, fire_at: float, note: str = "") -> dict:
        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "请输入有效的 http(s) 链接"}
        item = self.schedules.add(url, float(fire_at), note or "")
        return {"ok": True, "item": item.to_dict()}

    def remove_schedule(self, item_id: str) -> dict:
        self.schedules.remove(item_id)
        return {"ok": True}

    # -- 历史 --------------------------------------------------------

    def get_history(self, limit: int = 500) -> list[dict]:
        return [entry.to_dict() for entry in self.history.all(limit)]

    def clear_history(self) -> dict:
        self.history.clear_all()
        return {"ok": True}

    def remove_history_entry(self, entry_id: str) -> dict:
        self.history.remove(entry_id)
        return {"ok": True}

    # -- 日志 --------------------------------------------------------

    def get_logs(self, limit: int = 500) -> list[dict]:
        entries = get_logger().read_all()
        return [
            {
                "timestamp": entry.timestamp.isoformat(),
                "level": entry.level.name,
                "level_tag": entry.level.short_tag,
                "category": entry.category,
                "message": entry.message,
            }
            for entry in entries[:limit]
        ]

    def get_log_file_path(self) -> dict:
        return {"ok": True, "path": str(get_logger().file_path)}

    # -- 通知 --------------------------------------------------------

    def notify(self, title: str, body: str) -> dict:
        return {"ok": notify(title, "", body)}

    # -- 剪贴板候选 --------------------------------------------------

    def get_clipboard_candidate(self) -> dict | None:  # noqa: D
        """前端轮询这个，拿剪贴板里发现的下载链接。"""
        with self._lock:
            candidate = self._clipboard_candidate
            self._clipboard_candidate = None
        return candidate

    def set_clipboard_candidate(self, url: str) -> None:
        with self._lock:
            self._clipboard_candidate = {
                "url": url,
                "category": category_from_url(url).value,
                "default_threads": self.settings_store.settings.threads_default,
            }
        self._call_js("onClipboardCandidate", self._clipboard_candidate)

    def dismiss_clipboard_candidate(self) -> dict:
        with self._lock:
            self._clipboard_candidate = None
        return {"ok": True}

    # -- 目录选择 ----------------------------------------------------

    def choose_directory(self) -> dict:
        """打开系统目录选择框。"""
        if self._window is None:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            dialog_type = getattr(webview, "FOLDER_DIALOG", 2)
            result = self._window.create_file_dialog(dialog_type)
        except Exception as err:
            return {"ok": False, "error": str(err)}
        if not result:
            return {"ok": False, "cancelled": True}
        path = result[0] if isinstance(result, (list, tuple)) else str(result)
        return {"ok": True, "path": path}
