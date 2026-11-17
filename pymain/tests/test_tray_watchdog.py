"""托盘图标掉了要自己回来。

**用户报的现象是「过一会儿菜单栏图标自己没了」。** 掉了以后程序还在
跑，任务也在下，但常驻入口没了——用户找不到它，也就没有别的办法把它
叫回来（除非知道任务管理器）。

图标有两种掉法：

一、Explorer 重启了：任务栏窗口被重建，图标全被清掉。
二、Explorer 好好地跑着，图标被系统回收了（Windows 11 把新图标塞进
折叠区，从外面看就是「不见了」）。

第二种只盯任务栏句柄是发现不了的——句柄没变。所以每一拍都要问系统
「这个图标还在不在」。

**问不出来的时候不动手。** 问不出（None）跟不在（False）是两回事，
分不清就重挂的话，图标每 5 秒闪一次，用户看着是疯了。
"""

from __future__ import annotations

from typing import Any

import pytest

from sdm.gui import tray


class _FakeIcon:
    """记下 `visible` 被怎么设过。"""

    def __init__(self) -> None:
        self._hwnd = 12345
        self.visible_writes: list[bool] = []
        self._visible = True

    @property
    def visible(self) -> bool:
        return self._visible

    @visible.setter
    def visible(self, value: bool) -> None:
        self._visible = value
        self.visible_writes.append(value)


def _controller(icon: Any | None, tray_hwnd: int = 100) -> tray.TrayController:
    controller = tray.TrayController(bridge=None)
    controller._icon = icon
    controller._tray_hwnd = tray_hwnd
    return controller


def test_a_missing_icon_is_put_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """系统说不在，就把图标挂回去。"""
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: False)

    icon = _FakeIcon()
    controller = _controller(icon)
    controller._watch_tick()

    assert icon.visible_writes == [False, True]


def test_an_icon_that_is_there_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """系统说在，就不要动它。"""
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: True)

    icon = _FakeIcon()
    controller = _controller(icon)
    controller._watch_tick()

    assert icon.visible_writes == []


def test_an_icon_that_cannot_be_asked_about_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问不出来就不能当成不在。

    分不清「不在」和「问不了」就重挂，图标会每 5 秒闪一次。
    """
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: None)

    icon = _FakeIcon()
    controller = _controller(icon)
    controller._watch_tick()

    assert icon.visible_writes == []


def test_a_rebuilt_taskbar_puts_the_icon_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explorer 重启过：任务栏句柄变了，图标要挂回去。"""
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 200)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: True)

    icon = _FakeIcon()
    controller = _controller(icon, tray_hwnd=100)
    controller._watch_tick()

    assert icon.visible_writes == [False, True]
    assert controller._tray_hwnd == 200


def test_a_taskbar_that_is_not_there_yet_keeps_the_known_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务栏正在重启，句柄拿不到。

    句柄拿不到的时候不能说「任务栏重建过」——那会把手记住的句柄换成
    0，等任务栏回来、拿到同一个句柄时，又会被当成重建过一次，白挂
    一次图标。

    图标本身在不在是另一回事：问得出来就知道，掉了就挂回去。挂不上
    也只是白费一次，下一拍还会再试。
    """
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 0)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: False)

    icon = _FakeIcon()
    controller = _controller(icon, tray_hwnd=100)
    controller._watch_tick()

    # 句柄没有被清零。
    assert controller._tray_hwnd == 100
    # 图标确实掉了，所以还是挂了一次。
    assert icon.visible_writes == [False, True]


def test_a_taskbar_that_is_not_there_yet_leaves_a_live_icon_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务栏正在重启，但图标还注册着：不要动它。"""
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 0)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: True)

    icon = _FakeIcon()
    controller = _controller(icon, tray_hwnd=100)
    controller._watch_tick()

    assert icon.visible_writes == []
    assert controller._tray_hwnd == 100


def test_no_icon_means_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    """图标根本没装上的时候，看门狗不该去动它。"""
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: False)

    controller = _controller(None)
    controller._watch_tick()


def test_the_icon_is_put_back_with_false_then_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重挂的顺序是 False 再 True。

    pystray 的 `visible` setter 遇到值没变就直接返回，只置 True 等于
    什么都没做。
    """
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: False)

    icon = _FakeIcon()
    controller = _controller(icon)
    controller._watch_tick()

    assert icon.visible_writes[0] is False
    assert icon.visible_writes[1] is True


def test_a_failure_to_put_the_icon_back_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重挂失败不能把看门狗线程带崩。

    线程崩了图标就永远回不来了——比图标不见了更糟。
    """
    monkeypatch.setattr(tray, "_shell_tray_hwnd", lambda: 100)
    monkeypatch.setattr(tray, "_icon_present", lambda icon: False)

    class _BrokenIcon:
        _hwnd = 1

        @property
        def visible(self) -> bool:
            return True

        @visible.setter
        def visible(self, value: bool) -> None:
            raise OSError("the notification area is gone")

    controller = _controller(_BrokenIcon())
    controller._watch_tick()  # 不该抛
