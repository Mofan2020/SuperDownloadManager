"""Windows 通知区域（托盘）的底层询问。

**为什么需要这一层。** pystray 把 ``Shell_NotifyIcon`` 的返回值丢掉了
（``pystray/_win32.py`` 的 ``_message``：调用了但不返回、不检查），
所以「图标没挂上去」和「图标挂上去了」从程序里看是一模一样的。

Windows 11 又把新装的图标默认塞进折叠区（overflow chevron），图标
注册着、只是看不见。用户那边的说法是「过一会儿托盘自己没了」。

要分清这三件事（「从来没加上」「加过后来被销毁」「加上了但在折
叠区里藏着」），只能反过来问系统：这个图标现在还在不在。

**问法。** ``Shell_NotifyIconGetRect`` 回答「这个图标注册了吗」。

**uID 必须传 0。** pystray 建 ``NOTIFYICONDATAW`` 时传的是
``hID=id(self)``，而结构体里的字段叫 ``uID``——ctypes 把不认识的
关键字当成普通属性塞进去，真正的 ``uID`` 保持默认值 0。所以 pystray
挂的每个图标都是 ``uID == 0``，拿 ``id(icon)`` 去问会得到「不存在」，
而图标明明就在眼前。

**句柄不能省。** 这个调用认的是「窗口句柄 + 编号」的组合，只给
编号问的是全局编号 0 的那个图标，不是自己的。

非 Windows 上这一切都返回 None——「问不了」和「没有」是两回事，
不能混。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import ClassVar

__all__ = [
    "notification_area_ready",
    "notify_icon_present",
    "shell_tray_hwnd",
]

#: 任务栏窗口的类名。Explorer 重启之后这个窗口会被重建，句柄会变。
_SHELL_TRAY_WND = "Shell_TrayWnd"

#: 通知区域窗口的类名。
_TRAY_NOTIFY_WND = "TrayNotifyWnd"


def _user32():
    """user32.dll，句柄类型都声明好。

    **句柄参数必须声明 argtypes。** 从 ctypes 里读出来的句柄是普通
    Python int（64 位能到 1e18），不声明就按 C 的 int 处理，直接
    OverflowError。
    """
    dll = ctypes.WinDLL("user32", use_last_error=True)
    dll.FindWindowW.restype = wintypes.HWND
    dll.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    dll.FindWindowExW.restype = wintypes.HWND
    dll.FindWindowExW.argtypes = [
        wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR
    ]
    return dll


class _GUID(ctypes.Structure):
    """GUID。``Shell_NotifyIconGetRect`` 要它，但图标不是用 GUID 挂的。"""

    # ClassVar 是给 ruff 看的：_fields_ 是个可变列表，不标注的话它会被
    # 当成「所有实例共享的类属性」。ctypes 只认名字不认注解，运行时没影响。
    _fields_: ClassVar = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _NOTIFYICONIDENTIFIER(ctypes.Structure):
    """「哪一个图标」的说明。"""

    # ClassVar 是给 ruff 看的：_fields_ 是个可变列表，不标注的话它会被
    # 当成「所有实例共享的类属性」。ctypes 只认名字不认注解，运行时没影响。
    _fields_: ClassVar = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", _GUID),
    ]


class _ICONRECT(ctypes.Structure):
    """图标在屏幕上的位置。问的时候只是个占位。"""

    # ClassVar 是给 ruff 看的：_fields_ 是个可变列表，不标注的话它会被
    # 当成「所有实例共享的类属性」。ctypes 只认名字不认注解，运行时没影响。
    _fields_: ClassVar = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def is_supported() -> bool:
    """这个平台有没有通知区域。"""
    return sys.platform == "win32"


def shell_tray_hwnd() -> int:
    """任务栏窗口的句柄。Explorer 重启之后这个值会变。

    非 Windows 或者调用失败都返回 0。
    """
    if not is_supported():
        return 0
    try:
        user32 = _user32()
        return int(user32.FindWindowW(_SHELL_TRAY_WND, None) or 0)
    except Exception:
        return 0


def notification_area_ready() -> bool:
    """通知区域现在存在吗。

    登录时任务栏比程序先起来，所以这个判断要等一会儿才为真。
    """
    if not is_supported():
        return False
    try:
        user32 = _user32()
        taskbar = user32.FindWindowW(_SHELL_TRAY_WND, None)
        if not taskbar:
            return False
        return bool(user32.FindWindowExW(taskbar, None, _TRAY_NOTIFY_WND, None))
    except Exception:
        return False


def notify_icon_present(hwnd: int, uid: int = 0) -> bool | None:
    """那个图标现在还注册在通知区域里吗。

    ``True`` 是「在」，``False`` 是「不在」，``None`` 是「问不出来」
    ——后两者必须分开：``None`` 不能当成「不在」，否则换个问法就
    变成瞎重挂。

    传 ``uid=0``：pystray 挂的图标 ``uID`` 都是 0（见模块说明）。
    """
    if not is_supported():
        return None
    if not hwnd:
        return None

    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        get_rect = shell.Shell_NotifyIconGetRect
        get_rect.restype = ctypes.c_long
        get_rect.argtypes = [
            ctypes.POINTER(_NOTIFYICONIDENTIFIER),
            ctypes.POINTER(_ICONRECT),
        ]
    except (OSError, AttributeError, ValueError):
        return None

    ident = _NOTIFYICONIDENTIFIER(
        cbSize=ctypes.sizeof(_NOTIFYICONIDENTIFIER),
        hWnd=hwnd,
        uID=uid,
        guidItem=_GUID(),
    )
    rect = _ICONRECT()
    try:
        return get_rect(ctypes.byref(ident), ctypes.byref(rect)) == 0
    except (OSError, AttributeError, ValueError):
        return None
