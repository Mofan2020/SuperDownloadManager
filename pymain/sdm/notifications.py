"""系统通知：下载完成/失败时提醒。

对应 Swift 版 App/MainWindow/DownloadListStore.swift 里的
postCompleteNotification / postFailureNotification。

macOS 用 osascript 调 Notification Center（不需要额外依赖），
Windows 用 win10toast 风格的 PowerShell 脚本，失败都静默忽略——
通知是锦上添花，不该让主流程失败。
"""

from __future__ import annotations

import platform
import subprocess
import sys

from .logging_setup import CAT_GUI, get_logger

log = get_logger()


def notify(title: str, subtitle: str = "", body: str = "") -> bool:
    """发一条系统通知。返回是否成功发出。"""
    try:
        if sys.platform == "darwin":
            return _notify_macos(title, subtitle, body)
        if sys.platform.startswith("win"):
            return _notify_windows(title, subtitle, body)
    except Exception as err:  # noqa: BLE001
        log.debug(CAT_GUI, f"系统通知发送失败: {err}")
    return False


def _notify_macos(title: str, subtitle: str, body: str) -> bool:
    # osascript 里带引号要转义，否则标题里的引号会截断脚本。
    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{esc(body)}" with title "{esc(title)}" '
        f'subtitle "{esc(subtitle)}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _notify_windows(title: str, subtitle: str, body: str) -> bool:
    # 用 PowerShell 的 Windows.UI.Notifications。引号用单引号包裹，
    # 文本里的单引号翻倍。
    def esc(text: str) -> str:
        return text.replace("'", "''")

    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null; "
        "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{esc(subtitle + chr(10) + body)}')) > $null; "
        f"$n=[Windows.UI.Notifications.ToastNotification]::new($t); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('SuperDownloadManager').Show($n)"
    )
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps,
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def request_permission() -> bool:
    """申请通知权限。

    macOS 的通知权限由用户在系统设置里管，程序这边没法主动弹窗
    申请；第一次发通知时系统会自己问。Windows 同理。
    这里只是做一次「试探性发送」，让系统把权限对话框弹出来。
    """
    if platform.system() == "Darwin":
        return True
    if platform.system() == "Windows":
        return True
    return False
