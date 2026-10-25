"""macOS 菜单栏（NSStatusItem）的行为。

菜单结构照 Swift 版的 AppDelegate.makeMenu() 抄的——菜单项的标题、
分组、快捷键都要对得上，不然跟 Swift 版行为不一致。

真正碰 AppKit 的部分（建 status item、SF Symbol 图标）靠探针脚本
验证，见 `pymain/scripts/probe_status_bar.py`。这里测的是不需要
Cocoa 就能验的部分。
"""

from __future__ import annotations

import sys

import pytest

from sdm.gui import macos_status


def test_menu_spec_matches_swift_version():
    """菜单项的标题和快捷键要跟 Swift 版一致。"""
    titles = [s.title for s in macos_status.MENU_SPEC if not s.separator]
    assert titles == [
        "Open Main Window",
        "Add URL…",
        "Show Downloads in Finder",
        "About Super Download Manager",
        "Settings…",
        "Quit Super Download Manager",
    ]


def test_menu_spec_shortcuts():
    """Swift 版：⌘0 打开、⌘N 加 URL、⌘, 设置、⌘Q 退出。"""
    by_action = {s.action: s for s in macos_status.MENU_SPEC if not s.separator}
    assert by_action["openMainWindow"].key_equivalent == "0"
    assert by_action["addURL"].key_equivalent == "n"
    assert by_action["openSettings"].key_equivalent == ","
    assert by_action["quitApp"].key_equivalent == "q"
    for action in ("openMainWindow", "addURL", "openSettings", "quitApp"):
        assert by_action[action].command is True, f"{action} 该带 Command 修饰键"


def test_menu_spec_separators():
    """分组：主窗口/加 URL | Finder | 关于/设置 | 退出。"""
    kinds = ["sep" if s.separator else s.action for s in macos_status.MENU_SPEC]
    assert kinds == [
        "openMainWindow",
        "addURL",
        "sep",
        "revealDownloads",
        "sep",
        "showAbout",
        "openSettings",
        "sep",
        "quitApp",
    ]


def test_every_action_has_a_handler_slot():
    """菜单里的每个动作都要有对应的处理方法名。

    少一个就是点了没反应——Swift 版的 makeMenu 里也是一一对应的。
    """
    expected = {
        "openMainWindow",
        "addURL",
        "revealDownloads",
        "showAbout",
        "openSettings",
        "quitApp",
    }
    actual = {s.action for s in macos_status.MENU_SPEC if not s.separator}
    assert actual == expected


def test_handler_dispatch():
    """注册了处理器就调得到，没注册的安静跳过。"""
    controller = macos_status.StatusBarController(bridge=None, window=None)
    seen: list[str] = []
    controller.on("addURL", lambda: seen.append("addURL"))
    controller.on("quitApp", lambda: seen.append("quitApp"))

    macos_status._dispatch(controller, "addURL")
    macos_status._dispatch(controller, "quitApp")
    macos_status._dispatch(controller, "notRegistered")

    assert seen == ["addURL", "quitApp"]


def test_handler_exception_does_not_propagate():
    """菜单动作炸了不能把 app 带下去。"""
    controller = macos_status.StatusBarController(bridge=None, window=None)

    def boom() -> None:
        raise RuntimeError("炸了")

    controller.on("addURL", boom)
    macos_status._dispatch(controller, "addURL")  # 不抛异常就算过


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
    from Foundation import NSSelectorFromString

    controller = macos_status.StatusBarController(bridge=None, window=None)
    target = macos_status._make_target(controller)
    for spec in macos_status.MENU_SPEC:
        if spec.separator:
            continue
        selector = NSSelectorFromString(f"{spec.action}:")
        assert target.respondsToSelector_(selector), f"{spec.action} 没被 target 响应"
