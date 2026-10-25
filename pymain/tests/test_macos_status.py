"""macOS 菜单栏（NSStatusItem）的行为。

菜单结构**照契约 §5.1** 来：状态行 / 显示主窗口 / 暂停全部 / 继续全部 /
剪贴板监控 / 退出。

> **这里跟以前的版本不一样。** 早先菜单是照 Swift 版
> `AppDelegate.makeMenu()` 抄的（英文标题 + 加 URL / 访达 / 关于 / 设置）。
> 契约 §5.1 把菜单钉死成六项中文菜单项，Swift 版的那些项不在契约里，
> 所以菜单改了——**契约是并行开发的唯一真源**。

真正碰 AppKit 的部分（建 status item、SF Symbol 图标、Dock 进度）
靠代码审查 + 真机验证，这里测的是不需要 Cocoa 就能验的部分。
"""

from __future__ import annotations

import sys

import pytest

from sdm.gui import macos_status


def test_menu_spec_matches_the_contract():
    """菜单项跟契约 §5.1 一字不差。

    顺序也不能变——菜单栏是用户唯一能看到的功能清单。
    """
    titles = [s.title for s in macos_status.MENU_SPEC if not s.separator]
    assert titles == [
        "空闲",
        "显示主窗口",
        "暂停全部",
        "继续全部",
        "剪贴板监控",
        "退出",
    ]


def test_menu_spec_shortcuts():
    """⌘0 打开主窗口、⌘Q 退出。"""
    by_action = {s.action: s for s in macos_status.MENU_SPEC if not s.separator}
    assert by_action["openMainWindow"].key_equivalent == "0"
    assert by_action["quitApp"].key_equivalent == "q"
    for action in ("openMainWindow", "quitApp"):
        assert by_action[action].command is True, f"{action} 该带 Command 修饰键"


def test_menu_spec_separators():
    """分组：状态行 | 功能项 | 退出。"""
    kinds = ["sep" if s.separator else s.action for s in macos_status.MENU_SPEC]
    assert kinds == [
        "statusLine",
        "sep",
        "openMainWindow",
        "pauseAll",
        "resumeAll",
        "clipboardMonitor",
        "sep",
        "quitApp",
    ]


def test_status_line_is_not_selectable():
    """状态行只显示，不能点。"""
    by_action = {s.action: s for s in macos_status.MENU_SPEC if not s.separator}
    assert by_action["statusLine"].enabled is False


def test_clipboard_item_is_checkable():
    """「剪贴板监控」是勾选项。"""
    by_action = {s.action: s for s in macos_status.MENU_SPEC if not s.separator}
    assert by_action["clipboardMonitor"].checkable is True


def test_every_action_has_a_handler_slot():
    """菜单里的每个动作都要有对应的处理方法名。

    少一个就是点了没反应——菜单项跟处理器是一一对应的。
    """
    expected = {
        "statusLine",
        "openMainWindow",
        "pauseAll",
        "resumeAll",
        "clipboardMonitor",
        "quitApp",
    }
    actual = {s.action for s in macos_status.MENU_SPEC if not s.separator}
    assert actual == expected


def test_handler_dispatch():
    """注册了处理器就调得到，没注册的安静跳过。"""
    controller = macos_status.StatusBarController(bridge=None, window=None)
    seen: list[str] = []
    controller.on("pauseAll", lambda: seen.append("pauseAll"))
    controller.on("quitApp", lambda: seen.append("quitApp"))

    macos_status._dispatch(controller, "pauseAll")
    macos_status._dispatch(controller, "quitApp")
    macos_status._dispatch(controller, "notRegistered")

    assert seen == ["pauseAll", "quitApp"]


def test_handler_exception_does_not_propagate():
    """菜单动作炸了不能把 app 带下去。"""
    controller = macos_status.StatusBarController(bridge=None, window=None)

    def boom() -> None:
        raise RuntimeError("炸了")

    controller.on("pauseAll", boom)
    macos_status._dispatch(controller, "pauseAll")  # 不抛异常就算过


def test_update_status_records_the_status_text():
    """状态行的文案更新后要留在 controller 上（菜单还没建时也要能算）。"""
    controller = macos_status.StatusBarController(bridge=None, window=None)
    text = controller.update_status(2.4 * 1024 * 1024, 3, 0)
    assert text == "下载中 3 个 · 2.4 MB/s"
    assert controller.status_text == text


@pytest.mark.skipif(sys.platform != "darwin", reason="只 macOS 有菜单栏")
def test_available_on_macos():
    assert macos_status.available() is True


@pytest.mark.skipif(sys.platform == "darwin", reason="非 macOS 不该有菜单栏")
def test_not_available_elsewhere():
    assert macos_status.available() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="要 pyobjc")
def test_target_responds_to_every_action():
    """target 必须真的响应菜单里用的每个 selector。

    PyObjC 把 Python 的 `addURL_` 转成 ObjC 的 `addURL:`。名字写错
    的话菜单点了没反应，而且不报错——只能靠 respondsToSelector 查。
    """
    # importorskip 而不是硬 import——没有 PyObjC 的环境（Linux、
    # Windows、CI 的「屏蔽 PyObjC」那步）要跳过，而不是报错。
    foundation = pytest.importorskip("Foundation")
    nsselector = foundation.NSSelectorFromString

    controller = macos_status.StatusBarController(bridge=None, window=None)
    target = macos_status._make_target(controller)
    for spec in macos_status.MENU_SPEC:
        if spec.separator:
            continue
        selector = nsselector(f"{spec.action}:")
        assert target.respondsToSelector_(selector), f"{spec.action} 没被 target 响应"


def test_dock_progress_skips_redraw_when_unchanged(monkeypatch):
    """进度没变就不重绘（规格 §6.4：空闲时 CPU 接近 0）。

    Dock 进度是 1 秒刷一次。进度不变的时候每次都重新画一张图再
    `display()` 一次，等于每秒钟白唤醒一次 AppKit 的绘制管线。
    """
    if sys.platform != "darwin":
        pytest.skip("只在 macOS 上跑")

    drawn: list[float] = []

    class _FakeTile:
        def setContentView_(self, view):
            drawn.append(view)

        def display(self):
            pass

    class _FakeApp:
        def dockTile(self):
            return _FakeTile()

    class _FakeNSApplication:
        @staticmethod
        def sharedApplication():
            return _FakeApp()

    import types

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSApplication = _FakeNSApplication
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    # 画图那步要真 AppKit，这里用假的替掉——本测试只关心
    # 「要不要画」这件事。
    monkeypatch.setattr(
        macos_status, "_render_progress_image", lambda fraction, size=None: fraction
    )
    monkeypatch.setattr(macos_status, "_last_dock_fraction", None, raising=False)

    assert macos_status.set_dock_progress(0.5) is True
    assert drawn == [0.5], "第一次应该画"

    # 同样的进度值：不画
    assert macos_status.set_dock_progress(0.5) is True
    assert len(drawn) == 1, "进度没变却重绘了"

    # 变了：画
    assert macos_status.set_dock_progress(0.6) is True
    assert len(drawn) == 2

    # **回到 0 必须真的把 Dock 图标恢复默认。**
    #
    # 这里锁的是一个顺序坑：先记 0.0 再调清理的话，清理函数里的
    # 「已经清过了就别再清」会把真正的清理跳掉，Dock 图标上会
    # 永远留着最后一截进度条。
    cleared: list[int] = []

    class _FakeTile2:
        def setContentView_(self, view):
            if view is None:
                cleared.append(1)

        def display(self):
            pass

    class _FakeApp2:
        def dockTile(self):
            return _FakeTile2()

    class _FakeNSApplication2:
        @staticmethod
        def sharedApplication():
            return _FakeApp2()

    fake_appkit2 = types.ModuleType("AppKit")
    fake_appkit2.NSApplication = _FakeNSApplication2
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit2)

    assert macos_status.set_dock_progress(0.0) is True
    assert cleared, "进度归零时没有真正恢复默认 Dock 图标"
