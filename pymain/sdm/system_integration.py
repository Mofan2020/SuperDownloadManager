"""系统集成：打开文件、显示在文件管理器、开机自启。

对应 Swift 版 App/LaunchAtLogin.swift 与 DownloadListStore 的
openFile / revealInFinder。
"""

from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

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


# -- URL Scheme 注册（Windows） --------------------------------------
#
# 注册 `superdownloadmanager://` 之后，双击网页里的这个链接就能唤起
# 应用（Windows 会把它当成一个 URL 协议交给注册表里写的命令行）。
#
# macOS 那边靠 Info.plist 的 `CFBundleURLTypes`（build.py 写的），
# 不需要注册表——所以这段只对 Windows 生效。


#: 我们注册的 URL scheme。跟 build.py 写进 Info.plist 的
#: `CFBundleURLSchemes`、以及 `gui/url_scheme.py` 里的 `URL_SCHEME`
#: 是同一个。
URL_SCHEME = "superdownloadmanager"

#: 注册表里显示的名字（`Default` 值）。
URL_SCHEME_DISPLAY_NAME = "Super Download Manager"

#: 协议描述（`(Default)` 值）。
URL_SCHEME_PROTOCOL_NAME = "URL:SuperDownloadManager Protocol"


def url_scheme_registry_plan(
    scheme: str = URL_SCHEME,
    executable: str = "",
) -> list[tuple[str, str, str]]:
    """要往注册表里写的东西：`[(子路径, 值名, 值), ...]`。

    **值名为空串 = 该键的默认值。** 注册表里的 `(Default)` 不是一个
    叫 `(Default)` 的值，而是**键的默认值**——winreg 里用空字符串当
    值名来写它。写成 `"(Default)"` 会在注册表里真的造出一个叫
    `(Default)` 的普通值，系统认不出来，双击链接也就没反应。

    **路径在 `Software\\Classes` 下，不是 `HKEY_CLASSES_ROOT`。**
    HKCR 是 HKCU\\Software\\Classes 和 HKLM\\Software\\Classes 的合并
    视图，往它底下写需要管理员权限；直接写 HKCU 那一半普通用户就能
    装，不用提权。

    拆成纯函数是为了能在 macOS 上测——注册表只有 Windows 有，
    但路径怎么拼是纯逻辑，不该只能上 Windows 才验得了。
    """
    base = rf"Software\Classes\{scheme}"
    return [
        # 资源管理器里显示的协议名
        (base, "Default", URL_SCHEME_DISPLAY_NAME),
        # 协议描述
        (base, "", URL_SCHEME_PROTOCOL_NAME),
        # **有这个值（哪怕是空串）才表示「这是个 URL 协议」**
        (base, "URL Protocol", ""),
        # 双击链接时执行的命令行。%1 是链接本身
        (base + r"\shell\open\command", "", f'"{executable}" "%1"'),
    ]


def register_url_scheme(executable: str | None = None) -> bool:
    """注册 `superdownloadmanager://`，让双击链接能唤起应用。

    **幂等：** 已经是这样了就什么都不做。注册表写入会通知系统
    （Explorer 会刷新），每次启动都重写一遍是白折腾。

    **不需要管理员权限**（写在 HKCU 下）。

    `executable` 可以显式指定程序路径；从源码跑的时候
    `get_app_executable()` 返回 None（没有 .exe 可注册），注册不了。

    返回是否注册成功。
    """
    if platform.system() != "Windows":
        log.info(CAT_GUI, "当前系统不需要注册 URL scheme")
        return False

    exe = executable or get_app_executable()
    if not exe:
        log.warning(CAT_GUI, "找不到程序路径，无法注册 URL scheme")
        return False

    try:
        import winreg
    except ImportError:
        log.warning(CAT_GUI, "winreg 不可用，无法注册 URL scheme")
        return False

    try:
        plan = url_scheme_registry_plan(URL_SCHEME, exe)
        if _url_scheme_matches(winreg, plan):
            return True
        for subkey, value_name, value in plan:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, value)
        log.info(CAT_GUI, f"URL scheme 已注册：{URL_SCHEME}://")
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"注册 URL scheme 失败: {err}")
        return False


def unregister_url_scheme() -> bool:
    """注销 `superdownloadmanager://`。

    把 `Software\\Classes\\<scheme>` 底下整棵树删掉。
    返回是否成功（本来就没注册也算成功）。
    """
    if platform.system() != "Windows":
        return False

    try:
        import winreg
    except ImportError:
        return False

    try:
        _delete_key_tree(winreg, rf"Software\Classes\{URL_SCHEME}")
        log.info(CAT_GUI, f"URL scheme 已注销：{URL_SCHEME}://")
        return True
    except Exception as err:
        log.warning(CAT_GUI, f"注销 URL scheme 失败: {err}")
        return False


def is_url_scheme_registered() -> bool:
    """URL scheme 有没有注册。"""
    if platform.system() != "Windows":
        return False

    try:
        import winreg
    except ImportError:
        return False

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{URL_SCHEME}",
            0,
            winreg.KEY_READ,
        ) as key:
            winreg.QueryValueEx(key, "URL Protocol")
            return True
    except (FileNotFoundError, OSError):
        return False


def _url_scheme_matches(winreg: Any, plan: list[tuple[str, str, str]]) -> bool:
    """注册表里现在的内容是不是已经跟 plan 完全一样了。"""
    for subkey, value_name, value in plan:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, subkey, 0, winreg.KEY_READ
            ) as key:
                current, _ = winreg.QueryValueEx(key, value_name)
        except (FileNotFoundError, OSError):
            return False
        if current != value:
            return False
    return True


def _delete_key_tree(winreg: Any, path: str) -> None:
    """删掉一个键和它下面所有的子键。

    **必须先删子键再删父键**——父键里有东西的话 DeleteKey 会失败
    （注册表不像文件系统那样会级联删除）。
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            path,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            child_count = winreg.QueryInfoKey(key)[0]
            for index in range(child_count):
                child = winreg.EnumKey(key, index)
                _delete_key_tree(winreg, path + "\\" + child)
    except FileNotFoundError:
        return
    except OSError:
        pass

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


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
