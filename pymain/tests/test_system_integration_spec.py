"""系统集成层的行为契约（契约 §5）。

测的是**不需要 GUI、不需要显示器**的部分：菜单规格、状态行文案、
剪贴板开关落到哪个设置键、Windows 注册表路径怎么拼、托盘菜单跟
菜单栏是不是逐项一致。

真碰 AppKit 的部分（建 status item、画 Dock 图标）跑不了无头，
那部分靠代码审查 + 真机验证。
"""

from __future__ import annotations

import sys

import pytest

from sdm import system_integration
from sdm.gui import macos_status, tray

#: 契约 §5.1 规定的六个菜单项（顺序不能变）。
REQUIRED_ACTIONS = [
    "statusLine",
    "openMainWindow",
    "pauseAll",
    "resumeAll",
    "clipboardMonitor",
    "quitApp",
]

#: 除状态行外的五个菜单项，标题是固定的。
REQUIRED_TITLES = [
    "显示主窗口",
    "暂停全部",
    "继续全部",
    "剪贴板监控",
    "退出",
]


def _items(spec_list):
    """去掉分隔线，只留真正的菜单项。"""
    return [spec for spec in spec_list if not spec.separator]


# -- 菜单规格 -------------------------------------------------------


def test_menu_spec_has_the_six_required_items_in_order() -> None:
    """契约 §5.1 的菜单项一个不少、顺序不变。

    多一个少一个都不行——菜单栏是用户唯一能看到的功能清单。
    """
    actions = [spec.action for spec in _items(macos_status.MENU_SPEC)]
    assert actions == REQUIRED_ACTIONS


def test_menu_spec_titles_match_the_contract() -> None:
    """标题照契约写（状态行的标题是运行时刷的，单独测）。"""
    titles = [spec.title for spec in _items(macos_status.MENU_SPEC)]
    assert titles[0] == "空闲", "状态行的初始文案应该是「空闲」"
    assert titles[1:] == REQUIRED_TITLES


def test_status_line_item_is_not_selectable() -> None:
    """状态行只是显示，不能点。

    能点的状态行会被当成一个命令——用户一点，状态就没了。
    """
    status = next(s for s in _items(macos_status.MENU_SPEC) if s.action == "statusLine")
    assert status.enabled is False


def test_clipboard_item_is_checkable() -> None:
    """「剪贴板监控」是勾选项。"""
    item = next(
        s for s in _items(macos_status.MENU_SPEC) if s.action == "clipboardMonitor"
    )
    assert item.checkable is True


def test_pause_and_resume_items_exist() -> None:
    """暂停全部 / 继续全部是契约点名要的。"""
    actions = {spec.action for spec in _items(macos_status.MENU_SPEC)}
    assert "pauseAll" in actions
    assert "resumeAll" in actions


# -- 状态行文案 -----------------------------------------------------


def test_status_line_is_idle_when_nothing_is_downloading() -> None:
    assert macos_status.format_status_line(0.0, 0, 0.0) == "空闲"


def test_status_line_shows_count_and_speed() -> None:
    """契约 §5.1 给的例子：`下载中 3 个 · 2.4 MB/s`。"""
    speed = 2.4 * 1024 * 1024
    assert macos_status.format_status_line(speed, 3) == "下载中 3 个 · 2.4 MB/s"


def test_status_line_appends_the_speed_limit() -> None:
    """设了全局限速就追加 `· 限速 512 KB/s`。"""
    speed = 2.4 * 1024 * 1024
    limit = 512 * 1024
    assert (
        macos_status.format_status_line(speed, 3, limit)
        == "下载中 3 个 · 2.4 MB/s · 限速 512 KB/s"
    )


def test_status_line_without_limit_has_no_limit_part() -> None:
    """没设限速就不能多出「限速」那一段。"""
    text = macos_status.format_status_line(1024.0, 1, 0.0)
    assert "限速" not in text
    assert text == "下载中 1 个 · 1 KB/s"


def test_status_line_ignores_negative_numbers() -> None:
    """负速度不该把文案搞成负数。"""
    assert macos_status.format_status_line(-100.0, 1) == "下载中 1 个 · 0 B/s"
    assert macos_status.format_status_line(1024.0, -3) == "空闲"


def test_status_refresh_interval_is_about_one_and_a_half_seconds() -> None:
    """契约 §5.1：状态行 ~1.5 秒刷新，不是每帧。"""
    assert 1.0 <= macos_status.STATUS_REFRESH_SECONDS <= 2.0


def test_dock_refresh_interval_is_about_one_second() -> None:
    """Dock 进度 ~1 秒刷新。"""
    assert 0.5 <= macos_status.DOCK_REFRESH_SECONDS <= 1.5


# -- 剪贴板开关 -----------------------------------------------------


class _FakeStore:
    """只记写入的假设置存储。"""

    def __init__(self) -> None:
        self.writes: list[tuple[str, object]] = []
        self.values: dict[str, object] = {}

    def get(self, name: str):
        return self.values.get(name)

    def set(self, name: str, value) -> None:
        self.writes.append((name, value))
        self.values[name] = value


def test_clipboard_toggle_maps_to_the_capture_clipboard_setting() -> None:
    """「剪贴板监控」勾选项写的就是 `settings.capture_clipboard`。

    写别的键的话，菜单栏勾上了、设置页还是关的——两处对不上。
    """
    store = _FakeStore()
    assert macos_status.set_clipboard_monitor_enabled(True, store=store) is True
    assert store.writes == [("capture_clipboard", True)]

    assert macos_status.set_clipboard_monitor_enabled(False, store=store) is False
    assert store.writes[-1] == ("capture_clipboard", False)


def test_clipboard_setting_key_exists_in_the_settings_model() -> None:
    """这个键得真的存在于 Settings 里，否则 set() 会静默忽略。"""
    from sdm.settings import Settings

    assert macos_status.CLIPBOARD_SETTING_KEY == "capture_clipboard"
    assert hasattr(Settings(), "capture_clipboard")


def test_clipboard_enabled_reads_the_setting() -> None:
    store = _FakeStore()
    store.values["capture_clipboard"] = True
    assert macos_status.clipboard_monitor_enabled(store=store) is True
    store.values["capture_clipboard"] = False
    assert macos_status.clipboard_monitor_enabled(store=store) is False


# -- Dock 进度 ------------------------------------------------------


def test_overall_progress_uses_only_active_tasks() -> None:
    """已经下完的任务不该算进分母。

    算进去的话进度条会一直停在 99%，新任务一来就掉回去。
    """
    tasks = [
        {"state": "downloading", "total_bytes": 100, "bytes_done": 50},
        {"state": "queued", "total_bytes": 100, "bytes_done": 0},
        {"state": "completed", "total_bytes": 900, "bytes_done": 900},
    ]
    assert macos_status.compute_overall_progress(tasks) == 0.25


def test_overall_progress_is_zero_without_active_tasks() -> None:
    tasks = [{"state": "completed", "total_bytes": 100, "bytes_done": 100}]
    assert macos_status.compute_overall_progress(tasks) == 0.0
    assert macos_status.compute_overall_progress([]) == 0.0


def test_overall_progress_skips_tasks_with_unknown_size() -> None:
    """`total_bytes` 是 None 的任务算进去会把分母算错。"""
    tasks = [
        {"state": "downloading", "total_bytes": None, "bytes_done": 10},
        {"state": "downloading", "total_bytes": 100, "bytes_done": 100},
    ]
    assert macos_status.compute_overall_progress(tasks) == 1.0


def test_dock_progress_is_a_safe_noop_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 macOS 上必须是空操作，不能炸。

    **这条测试不能碰真的 Dock 图标**——所以把平台改掉，走「非 macOS」
    那条分支（它根本不 import AppKit）。
    """
    monkeypatch.setattr(macos_status.sys, "platform", "linux")
    assert macos_status.set_dock_progress(0.5) is False
    assert macos_status.clear_dock_progress() is False


# -- Windows 托盘 ---------------------------------------------------


def test_tray_menu_spec_matches_the_macos_menu_item_for_item() -> None:
    """托盘菜单跟菜单栏**逐项一致**（契约 §5.2）。

    两边共用同一份规格，所以这不只是「内容一样」，连对象都是同一个
    ——不存在「改了一边忘了另一边」。
    """
    assert tray.TRAY_MENU_SPEC is macos_status.MENU_SPEC

    macos_items = [(s.action, s.title) for s in _items(macos_status.MENU_SPEC)]
    tray_items = [(s.action, s.title) for s in _items(tray.TRAY_MENU_SPEC)]
    assert tray_items == macos_items


def test_tray_menu_has_the_same_six_items() -> None:
    actions = [spec.action for spec in _items(tray.TRAY_MENU_SPEC)]
    assert actions == REQUIRED_ACTIONS


def test_tray_module_imports_without_pystray() -> None:
    """没装 pystray 也不能 import 失败。

    **托盘是常驻入口，不该挡住应用启动。** 没装 pystray 时
    `available()` 返回 False，托盘不出现，应用照常跑。

    **这里不能靠「当前环境恰好没装 pystray」来验。** pystray 是
    Windows 的正式依赖（requirements.txt 里 `sys_platform == "win32"`），
    Windows CI 上它一定是装着的——照着环境写断言，在 Windows 上
    必红，在 macOS 上必绿。

    所以这里**真的把 import 挡掉**，重新 import 一次模块：
    `pystray` 变成 None、`available()` 变成 False，才算验过。
    """
    import builtins
    import importlib

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "pystray" or name.startswith("pystray."):
            raise ImportError("模拟：没装 pystray")
        return real_import(name, *args, **kwargs)

    saved = {k: v for k, v in sys.modules.items() if k == "pystray" or k.startswith("pystray.")}
    for key in saved:
        sys.modules.pop(key, None)

    builtins.__import__ = _blocked_import
    try:
        reloaded = importlib.reload(tray)
        assert reloaded.pystray is None, "pystray 挡住了但模块里还留着它"
        assert reloaded.available() is False, "没装 pystray 时 available() 必须返回 False"
    finally:
        builtins.__import__ = real_import
        sys.modules.update(saved)
        # 把模块恢复成「环境里真实的样子」，别让这次 reload
        # 污染后面用 pystray 的用例。
        importlib.reload(tray)


def test_tray_available_matches_environment() -> None:
    """`available()` 要跟环境对得上。

    Windows + 装了 pystray → True；其它情况 → False。
    """
    expected = sys.platform == "win32" and tray.pystray is not None
    assert tray.available() is expected


def test_tray_is_unavailable_off_windows() -> None:
    """macOS 上有菜单栏，不需要托盘。"""
    if sys.platform != "win32":
        assert tray.available() is False


def test_tray_actions_accept_pystrays_two_argument_signature() -> None:
    """pystray 点菜单项是 `action(icon, item)` 这样调的。

    **写成无参的话点一下就 TypeError**——菜单看着在、点了没反应，
    而且 pystray 不会把异常报到日志里。处理函数统一用 `(*_args)`
    收下那两个参数。
    """
    import inspect

    controller = tray.TrayController(bridge=object())
    handlers = [
        controller._action_for("openMainWindow"),
        controller._action_for("pauseAll"),
        controller._action_for("resumeAll"),
        controller._action_for("quitApp"),
        controller._toggle_clipboard,
    ]
    for handler in handlers:
        params = inspect.signature(handler).parameters.values()
        takes_var_args = any(
            param.kind is inspect.Parameter.VAR_POSITIONAL for param in params
        )
        takes_two = len(list(params)) >= 2
        assert takes_var_args or takes_two, f"{handler} 接不住 pystray 的 (icon, item)"


# -- Windows URL Scheme 注册表 --------------------------------------


def test_url_scheme_registry_plan_paths_and_values() -> None:
    """注册表怎么写是钉死的。

    值名为空串 = 键的默认值（注册表里的 `(Default)`）——写成
    `"(Default)"` 会在注册表里真的造出一个叫 `(Default)` 的普通值，
    系统认不出来。
    """
    plan = system_integration.url_scheme_registry_plan(
        "superdownloadmanager", r"C:\Apps\sdm.exe"
    )
    assert plan == [
        (
            r"Software\Classes\superdownloadmanager",
            "Default",
            "Super Download Manager",
        ),
        (
            r"Software\Classes\superdownloadmanager",
            "",
            "URL:SuperDownloadManager Protocol",
        ),
        (r"Software\Classes\superdownloadmanager", "URL Protocol", ""),
        (
            r"Software\Classes\superdownloadmanager\shell\open\command",
            "",
            '"C:\\Apps\\sdm.exe" "%1"',
        ),
    ]


def test_url_scheme_registry_plan_is_under_hkcu_not_hkcr() -> None:
    """必须写在 HKCU 底下——HKCR 要管理员权限，普通用户装不上。"""
    plan = system_integration.url_scheme_registry_plan(
        "superdownloadmanager", r"C:\Apps\sdm.exe"
    )
    for subkey, _value_name, _value in plan:
        assert not subkey.startswith("HKEY_")
        assert subkey.startswith(r"Software\Classes\superdownloadmanager")


def test_url_scheme_registry_plan_command_quotes_the_executable() -> None:
    """命令行要给路径加引号，`%1` 是链接本身。"""
    plan = system_integration.url_scheme_registry_plan(
        "superdownloadmanager", r"C:\Program Files\SDM\sdm.exe"
    )
    command = plan[-1][2]
    assert command.startswith('"') and command.endswith('"%1"')
    assert command == r'"C:\Program Files\SDM\sdm.exe" "%1"'


def test_url_scheme_registry_plan_is_idempotent_by_construction() -> None:
    """同样的输入必须得到同样的计划——注册前后的比较靠这个。"""
    first = system_integration.url_scheme_registry_plan(
        "superdownloadmanager", r"C:\Apps\sdm.exe"
    )
    second = system_integration.url_scheme_registry_plan(
        "superdownloadmanager", r"C:\Apps\sdm.exe"
    )
    assert first == second


def test_url_scheme_registration_is_a_noop_off_windows() -> None:
    """macOS 上不用注册表（走 Info.plist 的 CFBundleURLTypes）。"""
    if sys.platform == "win32":
        pytest.skip("Windows 上真的会写注册表")
    assert system_integration.register_url_scheme() is False
    assert system_integration.unregister_url_scheme() is False
    assert system_integration.is_url_scheme_registered() is False


def test_url_scheme_name_matches_the_gui_module() -> None:
    """注册表里的 scheme 名得跟 GUI 那边收的 scheme 一致。"""
    from sdm.gui import url_scheme

    assert system_integration.URL_SCHEME == url_scheme.URL_SCHEME
    assert system_integration.URL_SCHEME == "superdownloadmanager"
