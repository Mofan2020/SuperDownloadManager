"""剪贴板监听的跨平台读取测试。

这块出过两个「代码看着对，整条链路是死的」的 bug，症状都是
**用户界面上完全没有线索**（剪贴板监控就是个后台功能，不工作
的时候没有报错、没有弹窗）：

**(一) macOS 读剪贴板从来没成功过。** 代码写的是
`osascript -e "pbpaste"`，但 `pbpaste` 是 **shell 命令**，不是
AppleScript 命令——AppleScript 解释器把它当变量名，报
「变量"pbpaste"没有定义」(-2753)、退出码非 0。于是每轮都拿到
`None`，剪贴板监控从上线到现在一次都没工作过。

**(二) Windows 上句柄被截断。** `GetClipboardData` 返回的是
`HANDLE`（64 位），不声明 `restype` 时 ctypes 默认按 `c_int`
返回——64 位 Windows 上句柄被截成 32 位，低位最高位是 1 的
时候指针直接错位，读出来是空的。

这两个 bug 都在**开发机（macOS）上跑不出来**，所以测试必须能
在 macOS 上守住它们：macOS 那边查命令和真实读取，Windows 那边
用一个仿真 Win32 API 的假库跑完整条读取路径。

Windows 真机上的行为由 CI 的 windows runner 兜底。
"""

from __future__ import annotations

import ctypes
import shutil
from ctypes import wintypes
from pathlib import Path

import pytest

from sdm import clipboard

SRC = Path(__file__).resolve().parents[1] / "sdm" / "clipboard.py"

#: 假的 64 位剪贴板句柄。**低 32 位的最高位是 1**——这正是
#: 「按 c_int 返回就会被截断」的那一类值。
FAKE_HANDLE = 0x000001F3A2B3C4D5


# -- macOS：命令与真实读取 ----------------------------------------


def test_macos_does_not_use_the_broken_osascript_form() -> None:
    """**别再用 `osascript -e "pbpaste"`。**

    `pbpaste` 是 shell 命令，AppleScript 里没有这个命令名。
    正确的 AppleScript 写法是 `the clipboard`。
    """
    source = SRC.read_text(encoding="utf-8")
    assert '"-e", "pbpaste"' not in source, "又写回 osascript -e \"pbpaste\" 了"
    commands = clipboard._macos_read_commands()
    assert commands, "macOS 一个读剪贴板的命令都没有"
    for cmd in commands:
        if cmd[0].endswith("osascript"):
            assert "the clipboard" in cmd[-1], f"osascript 的 AppleScript 写法不对: {cmd}"


def test_macos_prefers_pbpaste_over_osascript() -> None:
    """优先用 `pbpaste`。

    它直接输出纯文本（文件类剪贴板时输出空、退出码 0，不会误报），
    而且不用每 0.5 秒起一个 AppleScript 解释器。
    """
    commands = clipboard._macos_read_commands()
    names = [c[0] for c in commands]
    assert names[-1] == "osascript", "osascript 应该是最后的兜底"
    if shutil.which("pbpaste"):
        assert "pbpaste" in names[0], f"pbpaste 应该在最前面，实际 {names}"
    # osascript 兜底那一路，AppleScript 写法必须是对的。
    fallback = commands[-1]
    assert fallback == ["osascript", "-e", "the clipboard"]


@pytest.mark.skipif(
    not shutil.which("pbpaste") and not shutil.which("osascript"),
    reason="这台机器没有 pbpaste / osascript",
)
def test_macos_read_actually_returns_text() -> None:
    """真读一次剪贴板。

    **这条是 macOS 那个 bug 的牙齿**：改回 `osascript -e "pbpaste"`
    的话，这里会拿到 `(None, -1)`，测试直接红。
    """
    text, change = clipboard._read_macos()
    assert text is not None, "读不到剪贴板（_read_macos 返回了 None）"
    assert isinstance(text, str)
    assert change == hash(text)


# -- Windows：Win32 API 的声明与读取 -------------------------------


class _FakeFunc:
    """假的 Win32 函数。

    **会按 `restype` 模拟 ctypes 的返回值转换**——这是关键：
    `restype` 是默认的 `c_int` 时，64 位的句柄会被截成 32 位
    （而且高位为 1 时变成负数）；`restype` 是 `c_void_p` 时才
    原样返回。

    不模拟这一层的话，「restype 没声明」这个 bug 在测试里就
    看不出来。
    """

    def __init__(self, fn, *, truncate_int_result: bool = True):
        self._fn = fn
        self.restype = None
        self.argtypes = None
        self._truncate = truncate_int_result
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        result = self._fn(*args)
        if self._truncate and (self.restype is None or self.restype is ctypes.c_int):
            # ctypes 默认按 c_int 返回 → 32 位截断。
            value = result & 0xFFFFFFFF
            if value >= 0x80000000:
                value -= 0x100000000
            return value
        return result


class _FakeLib:
    def __init__(self, funcs: dict):
        for name, func in funcs.items():
            setattr(self, name, func)


class _FakeWindll:
    def __init__(self, libs: dict):
        for name, lib in libs.items():
            setattr(self, name, lib)


def _fake_win32(text: str, *, open_ok: bool = True, has_text: bool = True):
    """造一套假的 user32 / kernel32。

    `GlobalLock` 返回一个真的指向宽字符串的指针——只有这样
    `ctypes.wchar_p(pointer).value` 才读得出东西。
    """
    buffer = ctypes.create_unicode_buffer(text)
    address = ctypes.addressof(buffer)

    def open_clipboard(_hwnd):
        return 1 if open_ok else 0

    def get_clipboard_data(_fmt):
        return FAKE_HANDLE if has_text else 0

    def global_lock(handle):
        # 句柄对不上就是拿到了错指针——真实 Windows 上这里会失败。
        if handle != FAKE_HANDLE:
            return 0
        return address

    def global_unlock(_handle):
        return 1

    def close_clipboard():
        return 1

    user32 = _FakeLib(
        {
            "OpenClipboard": _FakeFunc(open_clipboard),
            "GetClipboardData": _FakeFunc(get_clipboard_data),
            "CloseClipboard": _FakeFunc(close_clipboard),
        }
    )
    kernel32 = _FakeLib(
        {
            "GlobalLock": _FakeFunc(global_lock),
            "GlobalUnlock": _FakeFunc(global_unlock),
        }
    )
    # **缓冲区必须一直活着。** 它是局部变量，一出函数就被回收，
    # 地址随之失效——`c_wchar_p` 读到的就是一堆垃圾。
    user32._buffer = buffer
    kernel32._buffer = buffer
    return user32, kernel32


def test_windows_declares_handle_return_type() -> None:
    """**`GetClipboardData` 的 restype 必须是 HANDLE。**

    不声明的话 ctypes 默认按 `c_int` 返回 → 64 位句柄被截断 →
    指针错位 → 读出来是空的 → 剪贴板监控等于没有。
    """
    user32, kernel32 = _fake_win32("x")
    clipboard._declare_clipboard_api(user32, kernel32)

    assert user32.GetClipboardData.restype is wintypes.HANDLE, (
        "GetClipboardData 的返回类型必须是 HANDLE（c_void_p），"
        f"实际是 {user32.GetClipboardData.restype}"
    )
    assert user32.GetClipboardData.restype is not ctypes.c_int
    assert user32.OpenClipboard.argtypes == [wintypes.HWND]
    assert kernel32.GlobalLock.restype is wintypes.HANDLE
    assert kernel32.GlobalUnlock.argtypes == [wintypes.HGLOBAL]


def test_windows_reads_text_with_a_64bit_handle() -> None:
    """64 位句柄也要读得出文本。

    这是 Windows 那个 bug 的牙齿：`restype` 退回默认的 `c_int` 时，
    句柄被截成负数 → `GlobalLock` 对不上 → 返回空 → 测试红。
    """
    user32, kernel32 = _fake_win32("https://example.com/a.zip")
    text, change = clipboard._read_windows_with(user32, kernel32)
    assert text == "https://example.com/a.zip"
    assert change == hash("https://example.com/a.zip")


def test_windows_reads_empty_text_when_clipboard_has_no_text() -> None:
    """剪贴板里没有 Unicode 文本（复制的是文件之类）→ 没有候选。

    这不是错误，不能抛异常，也不能把上一轮的变更计数弄乱。
    """
    user32, kernel32 = _fake_win32("", has_text=False)
    text, change = clipboard._read_windows_with(user32, kernel32)
    assert text is None
    assert change == -1


def test_windows_survives_a_locked_clipboard() -> None:
    """别的程序正占着剪贴板 → 本轮跳过，不能抛异常。

    复制的那一刻浏览器就占着剪贴板，这是常事。
    """
    user32, kernel32 = _fake_win32("x", open_ok=False)
    text, change = clipboard._read_windows_with(user32, kernel32)
    assert text is None
    assert change == -1


def test_windows_closes_the_clipboard_even_on_failure() -> None:
    """`CloseClipboard` 一定要被调到。

    不关的话剪贴板会被这个进程一直占着——用户复制什么都复制不了。
    """
    user32, kernel32 = _fake_win32("x", has_text=False)
    clipboard._read_windows_with(user32, kernel32)
    assert user32.CloseClipboard.calls, "CloseClipboard 没被调用"


# -- 开关：线程常驻 + 三个入口统一 -------------------------------


def test_monitor_thread_starts_regardless_of_the_setting() -> None:
    """**监听器无条件起线程。**

    之前是「启动时设置已经开着才起」——用户从设置页或 Windows
    托盘把开关打开，线程压根没跑。
    """
    from sdm.gui import main as gui_main

    class _FakeBridge:
        def set_clipboard_candidate(self, url: str) -> None:
            return None

    monitor = gui_main._start_clipboard_monitor(_FakeBridge())
    try:
        assert monitor._thread is not None, "监听线程没起来"
        assert monitor._thread.is_alive(), "监听线程已经死了"
    finally:
        monitor.stop()


def test_monitor_starts_checking_when_the_setting_is_turned_on_later(monkeypatch) -> None:
    """开关后来打开 → 监听器开始检查。

    线程常驻 + 每轮读设置，开了下一轮就生效。
    """
    enabled = {"value": False}
    checked: list[int] = []

    monkeypatch.setattr(
        clipboard.ClipboardMonitor,
        "_capture_enabled",
        staticmethod(lambda: enabled["value"]),
    )
    monkeypatch.setattr(
        clipboard.ClipboardMonitor,
        "_check",
        lambda self: checked.append(1),
    )

    mon = clipboard.ClipboardMonitor(
        on_candidate=lambda u: None, poll_interval=0.01, idle_interval=0.01
    )
    mon.start()
    import time

    time.sleep(0.1)
    assert checked == [], "开关还关着，不该检查"
    enabled["value"] = True
    time.sleep(0.15)
    mon.stop()

    assert checked, "开关打开后监听器没有开始检查"


def test_bridge_toggle_writes_the_setting_and_pushes_the_event(monkeypatch) -> None:
    """菜单栏 / 托盘走 bridge 的统一出口。

    写设置 + 推 `settings_updated`——不推的话设置页上的开关还是
    老样子，用户以为没生效。
    """
    from sdm.gui.bridge import GuiBridge

    bridge = GuiBridge()
    pushed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        bridge, "_emit_event", lambda kind, payload=None: pushed.append((kind, payload))
    )

    result = bridge.set_clipboard_monitor(True)
    assert result["ok"] is True
    assert result["enabled"] is True
    assert bridge.settings_store.settings.capture_clipboard is True
    assert any(kind == "settings_updated" for kind, _ in pushed), "没推 settings_updated"

    bridge.set_clipboard_monitor(False)
    assert bridge.settings_store.settings.capture_clipboard is False


def test_tray_toggle_goes_through_the_bridge() -> None:
    """Windows 托盘的勾选项也得走 bridge。

    之前托盘自己写设置，不推事件——用户从托盘勾完，回头看设置页
    还是「关」。
    """
    from sdm.gui import tray

    calls: list[bool] = []

    class _FakeBridge:
        def set_clipboard_monitor(self, enabled: bool) -> dict:
            calls.append(enabled)
            return {"ok": True, "enabled": enabled}

    controller = tray.TrayController(bridge=_FakeBridge())
    controller._clipboard_checked = False
    controller._toggle_clipboard()
    assert calls == [True], "托盘没走 bridge 的统一出口"
    assert controller._clipboard_checked is True


# -- 失败要出声 ---------------------------------------------------


def test_read_failure_is_logged_at_warning_level(monkeypatch) -> None:
    """读取失败必须是 **warning**，不能是 debug。

    用户默认日志级别是 warning——debug 那条他们根本看不到，
    「剪贴板监控用不了」就变成了一个完全无从查起的问题。
    """
    from sdm.logging_setup import get_logger

    warnings: list[str] = []
    monkeypatch.setattr(
        get_logger(),
        "warning",
        lambda category, message: warnings.append(message),
    )
    clipboard._warn_read_failure("测试用的失败")
    assert any("测试用的失败" in w for w in warnings), "失败没有 warning 出来"


def test_read_failure_warning_is_throttled(monkeypatch) -> None:
    """同一个失败不要刷屏。

    监听器每 0.5 秒轮询一次，不节流的话一次失败刷几千行。
    """
    from sdm.logging_setup import get_logger

    warnings: list[str] = []
    monkeypatch.setattr(
        get_logger(),
        "warning",
        lambda category, message: warnings.append(message),
    )
    monkeypatch.setattr(clipboard, "_WARN_INTERVAL", 30.0)
    clipboard._warn_state.clear()

    for _ in range(20):
        clipboard._warn_read_failure("同一个失败")
    assert len(warnings) == 1, f"同一个失败报了 {len(warnings)} 次"
