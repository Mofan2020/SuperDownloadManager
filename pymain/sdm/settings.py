"""设置存储：JSON 文件，带默认值与容错。

对应 Swift 版散落在各处的 @AppStorage("sdm.xxx") 键。

键名沿用 Swift 版的命名，方便老用户迁移配置时不用改键。
读取时遇到不认识的键忽略、类型不对用默认值——配置文件坏了
不能让应用起不来。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .logging_setup import CAT_STORE, LogLevel, LogSettings, get_logger

log = get_logger()


@dataclass
class Settings:
    """全部用户设置。字段名对应 Swift 版的 UserDefaults 键。"""

    # 默认
    threads_default: int = 32
    bandwidth_bytes_per_sec: int = 0

    # 启动
    open_at_login: bool = False
    update_channel: str = "stable"

    # 通用
    locale: str = "system"
    classify_enabled: bool = True
    capture_clipboard: bool = True
    notify_on_complete: bool = True
    hide_dock_when_window_closed: bool = False
    toolbar_show_labels: bool = True
    #: 主题：system（跟随系统）/ light / dark。
    #: "system" 时前端**不设** data-theme 属性，让 prefers-color-scheme 说话——
    #: 写死成 light 再在深色系统下显示浅色，比什么都不做更糟。
    theme: str = "system"
    #: 任务列表里被隐藏的列（列 id 列表）。
    #: 默认空 = 全部显示。列可见性是纯 UI 偏好，不该进 Swift 版的键空间，
    #: 所以 key 用契约 §6 规定的 `sdm.<字段名>` 形式。
    hidden_columns: list[str] = field(default_factory=list)

    # 队列与并发
    #: 同时活跃的任务数上限（契约 §2.4）。超出的任务停在 queued。
    max_concurrent_tasks: int = 5
    #: 动态分段的起点连接数（契约 §2.5）。
    initial_connections: int = 2
    #: 分片数上限（契约 §2.5）。
    max_chunk_count: int = 32

    # 网络
    network_ignore_ssl: bool = False
    #: 全局 User-Agent。空 = 用程序内置的默认 UA。
    #:
    #: **站点规则的 UA 优先于它**——规则是用户为某个主机显式配的
    #: 「跟这个站点说话的方式」，全局设置只是兜底。
    network_user_agent: str = ""
    #: 全局代理。空 = 不走代理。
    proxy_url: str = ""
    #: 代理协议：http / https / socks4 / socks5。
    proxy_scheme: str = "http"
    #: 浏览器接管配对 token（契约 §4.2）。空 = 还没配过。
    browser_pair_token: str = ""

    # 日志
    logs_level: int = int(LogLevel.INFO)
    logs_persistence: bool = True
    logs_retention_days: int = 14
    logs_max_bytes_kb: int = 4096
    logs_max_lines: int = 5000

    # 窗口
    window_width: int = 1180
    window_height: int = 760
    window_x: int | None = None
    window_y: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def log_settings(self) -> LogSettings:
        return LogSettings(
            level=LogLevel(min(LogLevel.ERROR, max(LogLevel.DEBUG, self.logs_level))),
            retention_days=self.logs_retention_days,
            max_bytes_per_file=self.logs_max_bytes_kb * 1024,
            max_lines=self.logs_max_lines,
            persistence_enabled=self.logs_persistence,
        )


# Swift 版的 UserDefaults 键 → Python 字段
_KEY_MAP = {
    "sdm.threads.default": "threads_default",
    "sdm.bandwidth.bytesPerSec": "bandwidth_bytes_per_sec",
    "sdm.openAtLogin": "open_at_login",
    "sdm.updateChannel": "update_channel",
    "sdm.locale": "locale",
    "sdm.classify.enabled": "classify_enabled",
    "sdm.capture.clipboard": "capture_clipboard",
    "sdm.notify.onComplete": "notify_on_complete",
    "sdm.hideDockWhenWindowClosed": "hide_dock_when_window_closed",
    "sdm.toolbar.showLabels": "toolbar_show_labels",
    # 契约 §6 新增键。Swift 版没有对应的键，按契约的规定用
    # `sdm.<字段名>` 形式——别自作主张起个 camelCase 的名字，
    # 将来 Swift 版真加了这个设置，两边的键名会对不上。
    "sdm.theme": "theme",
    "sdm.hidden_columns": "hidden_columns",
    "sdm.max_concurrent_tasks": "max_concurrent_tasks",
    "sdm.initial_connections": "initial_connections",
    "sdm.max_chunk_count": "max_chunk_count",
    "sdm.proxy_url": "proxy_url",
    "sdm.proxy_scheme": "proxy_scheme",
    "sdm.browser_pair_token": "browser_pair_token",
    "sdm.network.ignoreSSL": "network_ignore_ssl",
    "sdm.network.userAgent": "network_user_agent",
    "sdm.logs.level": "logs_level",
    "sdm.logs.persistence": "logs_persistence",
    "sdm.logs.retentionDays": "logs_retention_days",
    "sdm.logs.maxBytesKB": "logs_max_bytes_kb",
    "sdm.logs.maxLines": "logs_max_lines",
}

_FIELD_MAP = {v: k for k, v in _KEY_MAP.items()}


class SettingsStore:
    """设置的单例存储。"""

    _instance: SettingsStore | None = None
    _instance_lock = threading.Lock()

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or paths.settings_path()
        self._lock = threading.Lock()
        self._settings = Settings()
        self.load()

    @classmethod
    def shared(cls) -> SettingsStore:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # -- 读写 --------------------------------------------------------

    def load(self) -> Settings:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return self._settings
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(CAT_STORE, f"配置文件解析失败，使用默认值: {self._path}")
            return self._settings
        if not isinstance(data, dict):
            return self._settings

        with self._lock:
            for field_name, value in data.items():
                if not hasattr(self._settings, field_name):
                    continue  # 不认识的键直接忽略
                current = getattr(self._settings, field_name)
                # 类型不对就用默认值，不让坏配置把应用带崩
                if isinstance(current, bool):
                    if isinstance(value, bool):
                        setattr(self._settings, field_name, value)
                elif isinstance(current, int):
                    if isinstance(value, int) and not isinstance(value, bool):
                        setattr(self._settings, field_name, value)
                elif isinstance(current, float):
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        setattr(self._settings, field_name, float(value))
                elif isinstance(current, str):
                    if isinstance(value, str):
                        setattr(self._settings, field_name, value)
                elif isinstance(current, list):
                    # 列表型设置（列可见性）也要能读回来。
                    #
                    # **不加这一支的话 `hidden_columns` 永远存不进去**——
                    # 下面所有 isinstance 分支都不匹配 list，值会被
                    # 静默丢掉，用户改了列可见性，下次启动又变回去。
                    # 逐项校验类型：列表里混进非字符串会让前端比较时炸掉。
                    if isinstance(value, list) and all(isinstance(x, str) for x in value):
                        setattr(self._settings, field_name, list(value))
                elif current is None and (value is None or isinstance(value, int | float | str)):
                    setattr(self._settings, field_name, value)
        return self._settings

    def save(self) -> None:
        with self._lock:
            payload = self._settings.to_dict()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as err:
            log.error(CAT_STORE, f"配置保存失败: {err}")

    @property
    def settings(self) -> Settings:
        return self._settings

    def get(self, name: str) -> Any:
        return getattr(self._settings, name)

    def set(self, name: str, value: Any) -> None:
        """改一个设置并落盘。未知字段忽略。"""
        if not hasattr(self._settings, name):
            return
        with self._lock:
            setattr(self._settings, name, value)
        self.save()

    def update(self, changes: dict) -> None:
        """批量改。未知键忽略。"""
        with self._lock:
            for name, value in changes.items():
                if not hasattr(self._settings, name):
                    continue
                current = getattr(self._settings, name)
                # 列表型键只接受「字符串列表」。前端传别的结构（比如把
                # hidden_columns 传成逗号分隔的字符串）时宁可不改——
                # 写坏的结构会在渲染时炸掉，比不生效更难查。
                if isinstance(current, list) and not (
                    isinstance(value, list) and all(isinstance(x, str) for x in value)
                ):
                    continue
                setattr(self._settings, name, value)
        self.save()

    def legacy_key(self, field_name: str) -> str:
        """Python 字段名 → Swift 版的 UserDefaults 键。"""
        return _FIELD_MAP.get(field_name, field_name)
