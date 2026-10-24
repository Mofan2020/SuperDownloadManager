"""跨平台路径解析。

所有持久化位置集中在这里，避免 macOS / Windows 各写一套。
Windows 用 %LOCALAPPDATA%\\SuperDownloadManager，
macOS 用 ~/Library/Application Support/SuperDownloadManager，
Linux 用 ~/.local/share/SuperDownloadManager。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "SuperDownloadManager"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_macos() -> bool:
    return sys.platform == "darwin"


def config_dir() -> Path:
    """配置与数据库目录。"""
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / APP_DIR_NAME
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / APP_DIR_NAME


def log_dir() -> Path:
    """日志目录。"""
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / APP_DIR_NAME / "Logs"
    if is_macos():
        return Path.home() / "Library" / "Logs" / APP_DIR_NAME
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / APP_DIR_NAME


def history_db_path() -> Path:
    return config_dir() / "history.sqlite"


def socket_path() -> str:
    """守护进程 IPC socket 路径。"""
    if is_windows():
        return "npipe://sdm-sdmd"
    return str(config_dir() / "sdmd.sock")


def settings_path() -> Path:
    return config_dir() / "settings.json"


def site_rules_path() -> Path:
    return config_dir() / "site_rules.json"


def schedules_path() -> Path:
    return config_dir() / "schedules.json"


def download_dir() -> Path:
    return Path.home() / "Downloads"


def cli_install_dir() -> Path:
    """CLI 软链目标目录。"""
    if is_windows():
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs" / "SDM"
    return Path.home() / ".local" / "bin"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
