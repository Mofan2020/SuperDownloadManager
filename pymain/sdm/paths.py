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
    """配置与数据库目录。

    **环境变量 `SDM_CONFIG_DIR` 优先。** 用途：
    - 测试隔离——pytest 全程指向临时目录，不会碰用户真实数据
    - 便携模式——把配置和数据库放在 U 盘或别的盘里
    """
    override = os.environ.get("SDM_CONFIG_DIR")
    if override:
        return Path(override)
    if is_windows():
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / APP_DIR_NAME
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / APP_DIR_NAME


def log_dir() -> Path:
    """日志目录。

    **环境变量 `SDM_LOG_DIR` 优先**，理由同 `config_dir()`。
    """
    override = os.environ.get("SDM_LOG_DIR")
    if override:
        return Path(override)
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
    """守护进程 IPC socket 路径。

    **环境变量 `SDM_SOCKET` 优先**——和 Swift 版 CLI/SDMDM.swift:212
    的 `ProcessInfo.processInfo.environment["SDM_SOCKET"] ??
    defaultSocketPath` 语义一致。测试、并行跑多个 daemon、
    想把 socket 放到别处时都靠它。daemon 和 CLI 都走这个函数，
    算出来的路径必然一致。

    unix socket 的路径有长度上限：macOS 104 字节，Linux 108。超了
    socket 根本建不出来，daemon 启动时报 "AF_UNIX path too long"。

    正常的 macOS 路径是
    `/Users/<用户名>/Library/Application Support/SuperDownloadManager/sdmd.sock`，
    七十几字节没事。但用户名长、或者 HOME 指向深层目录时就超了。

    超了就退回 `/tmp/sdm-<uid>.sock`——短，且带 uid 不会跟别的用户
    撞。socket 本身权限是 0600，只有本人能连。

    `SDM_SOCKET` 是用户显式指定的，原样返回（不做长度回退——
    用户自己指定的路径该不该用由用户决定，报错比偷偷换路径好）。
    Windows 上 IPC 走 TCP 回环端口，路径不用，但覆盖仍然生效，
    免得两边算出不一样的值。
    """
    override = os.environ.get("SDM_SOCKET")
    if override:
        return override
    if is_windows():
        return "npipe://sdm-sdmd"
    path = str(config_dir() / "sdmd.sock")
    if len(path.encode("utf-8")) >= 100:
        return f"/tmp/sdm-{os.getuid()}.sock"
    return path


def settings_path() -> Path:
    return config_dir() / "settings.json"


def site_rules_path() -> Path:
    return config_dir() / "site_rules.json"


def schedules_path() -> Path:
    return config_dir() / "schedules.json"


def download_dir() -> Path:
    """下载目录。

    **环境变量 `SDM_DOWNLOAD_DIR` 优先**，理由同 `config_dir()`。
    """
    override = os.environ.get("SDM_DOWNLOAD_DIR")
    if override:
        return Path(override)
    return Path.home() / "Downloads"


def cli_install_dir() -> Path:
    """CLI 软链目标目录。"""
    if is_windows():
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Programs" / "SDM"
    return Path.home() / ".local" / "bin"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    log_dir().mkdir(parents=True, exist_ok=True)
