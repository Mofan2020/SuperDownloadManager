"""系统集成：打开文件、显示在文件管理器、开机自启。

对应 Swift 版 App/LaunchAtLogin.swift 与 DownloadListStore 的
openFile / revealInFinder。
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path

from .logging_setup import CAT_GUI, get_logger

log = get_logger()


def open_file(path: str | Path) -> bool:
    """用系统默认程序打开文件。"""
    target = Path(path)
    if not target.exists():
        return False
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(target)], check=False, capture_output=True, timeout=10)
        elif system == "Windows":
            os_startfile = getattr(__import__("os"), "startfile", None)
            if os_startfile is None:
                return False
            os_startfile(str(target))  # type: ignore[misc]
        else:
            subprocess.run(["xdg-open", str(target)], check=False, capture_output=True, timeout=10)
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"打开文件失败: {err}")
        return False


def reveal_in_finder(path: str | Path) -> bool:
    """在文件管理器里选中文件。"""
    target = Path(path)
    if not target.exists():
        return False
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(
                ["open", "-R", str(target)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        elif system == "Windows":
            subprocess.run(
                ["explorer", f"/select,{target}"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        else:
            subprocess.run(
                ["xdg-open", str(target.parent)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"在访达中显示失败: {err}")
        return False


def open_url(url: str) -> bool:
    """在默认浏览器里打开 URL。"""
    try:
        if platform.system() == "Darwin":
            subprocess.run(["open", url], check=False, capture_output=True, timeout=10)
        elif platform.system() == "Windows":
            import webbrowser

            webbrowser.open(url)
        else:
            subprocess.run(["xdg-open", url], check=False, capture_output=True, timeout=10)
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"打开链接失败: {err}")
        return False


def open_log_in_console(path: str | Path) -> bool:
    """在 macOS 的 Console.app 里打开日志。

    对应 Swift 版 `SettingsView.swift` 里的「在 Console.app 中打开」
    按钮。Swift 那版用的是 `x-man-page://console.app/subsystem/<id>`
    这种 URL scheme，直接跳到按子系统过滤的视图；Python 这边用
    `open -a Console <path>` 打开日志文件本身——Console.app 会把
    打开的文件收进 Console 里，够用了。

    子系统 ID 从 `version.BUNDLE_ID` 取，不硬编码。Windows 和
    Linux 没有 Console.app，直接返回 False。

    文件不存在时返回 False——Console 打开一个不存在的路径只会
    弹一个报错框，没什么用。
    """
    target = Path(path)
    if not target.exists():
        return False
    if platform.system() != "Darwin":
        log.info(CAT_GUI, "当前系统不支持 Console.app")
        return False
    from .version import BUNDLE_ID

    try:
        subprocess.run(
            ["open", "-a", "Console", str(target)],
            check=False,
            capture_output=True,
            timeout=10,
        )
        log.debug(CAT_GUI, f"已在 Console.app 中打开日志（子系统 {BUNDLE_ID}）")
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"在 Console.app 中打开日志失败: {err}")
        return False


# -- 开机自启 -------------------------------------------------------


def set_open_at_login(enabled: bool) -> bool:
    """开关开机自启。

    macOS 用 launchd 的 LaunchAgent，Windows 用注册表 Run 键。
    Linux 不支持（没有通用的用户级自启机制）。
    """
    system = platform.system()
    if system == "Darwin":
        return _set_open_at_login_macos(enabled)
    if system == "Windows":
        return _set_open_at_login_windows(enabled)
    log.info(CAT_GUI, "当前系统不支持开机自启")
    return False


def _set_open_at_login_macos(enabled: bool) -> bool:
    from .version import BUNDLE_ID

    label = f"{BUNDLE_ID}.autostart"
    agents = Path.home() / "Library" / "LaunchAgents"
    plist = agents / f"{label}.plist"
    launchctl = ["launchctl", "load"]

    if not enabled:
        try:
            subprocess.run(
                ["launchctl", "unload", str(plist)],
                check=False,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            plist.unlink()
        except OSError:
            pass
        return True

    executable = get_app_executable()
    if executable is None:
        log.warning(CAT_GUI, "找不到程序路径，无法设置开机自启")
        return False

    agents.mkdir(parents=True, exist_ok=True)
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    try:
        plist.write_text(content, encoding="utf-8")
        subprocess.run(
            [*launchctl, str(plist)],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError) as err:
        log.warning(CAT_GUI, f"开机自启设置失败: {err}")
        return False


def _set_open_at_login_windows(enabled: bool) -> bool:
    try:
        import winreg

        from .version import APP_NAME

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                executable = get_app_executable()
                if executable is None:
                    return False
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{executable}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"开机自启设置失败: {err}")
        return False


def get_app_executable() -> str | None:
    """当前程序的启动路径。"""
    if getattr(sys, "frozen", False):
        return sys.executable
    # 从源码运行时指向仓库里的启动脚本
    return None


def is_open_at_login_enabled() -> bool:
    """当前是否已设置开机自启。"""
    system = platform.system()
    if system == "Darwin":
        from .version import BUNDLE_ID

        plist = Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.autostart.plist"
        return plist.exists()
    if system == "Windows":
        try:
            import winreg

            from .version import APP_NAME

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
                winreg.QueryValueEx(key, APP_NAME)
                return True
        except (FileNotFoundError, OSError):
            return False
    return False
