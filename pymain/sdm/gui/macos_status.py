"""macOS 菜单栏常驻（NSStatusItem）。

Swift 版用 AppDelegate 手写的 NSStatusItem 提供常驻入口——菜单栏
一个下载图标，点开是：打开主窗口、加 URL、在访达显示下载目录、
关于、设置、退出。

Swift 版最初想用 `MenuBarExtra`（SwiftUI），但那个在 macOS 26 上
不可靠（`App/SDMApp.swift` 里的注释写明了），所以改成原生
NSStatusItem。Python 侧没有 SwiftUI，但 PyObjC 能直接调
AppKit——macOS 自带，不给用户加依赖。

菜单项的标题和快捷键跟 Swift 版保持一致。

只在 macOS 上生效。其他平台 `install()` 返回 None，调用方不用
判断平台。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


#: 菜单项的纯数据定义。跟 Swift 版的 makeMenu() 一一对应。
#:
#: action 是 selector 名（不含冒号），key_equivalent 是快捷键字符，
#: command 标记表示要按住 Command。
@dataclass(frozen=True)
class MenuItemSpec:
    action: str
    title: str
    key_equivalent: str = ""
    command: bool = False
    separator: bool = False


#: 菜单结构。顺序和分组跟 Swift 版一致。
MENU_SPEC: list[MenuItemSpec] = [
    MenuItemSpec("openMainWindow", "Open Main Window", "0", command=True),
    MenuItemSpec("addURL", "Add URL…", "n", command=True),
    MenuItemSpec("", "", separator=True),
    MenuItemSpec("revealDownloads", "Show Downloads in Finder"),
    MenuItemSpec("", "", separator=True),
    MenuItemSpec("showAbout", "About Super Download Manager"),
    MenuItemSpec("openSettings", "Settings…", ",", command=True),
    MenuItemSpec("", "", separator=True),
    MenuItemSpec("quitApp", "Quit Super Download Manager", "q", command=True),
]


def available() -> bool:
    """这个平台能不能装菜单栏图标。"""
    return sys.platform == "darwin"


class StatusBarController:
    """NSStatusItem 的包装。

    只在 macOS 上真的调 AppKit。其他平台这个类仍然可以实例化，
    但 `install()` 什么都不做——这样单元测试不用 skip 整块逻辑。
    """

    def __init__(self, bridge: Any, window: Any) -> None:
        self.bridge = bridge
        self.window = window
        self._status_item: Any = None
        self._target: Any = None
        self._handlers: dict[str, Callable[[], None]] = {}

    # -- 回调注册 ----------------------------------------------------

    def on(self, action: str, handler: Callable[[], None]) -> None:
        """注册一个菜单动作的处理器。"""
        self._handlers[action] = handler

    # -- 生命周期 ----------------------------------------------------

    def install(self) -> bool:
        """把图标装到菜单栏。返回是否成功。"""
        if not available():
            return False
        try:
            from AppKit import (
                NSApplication,
                NSImage,
                NSMenu,
                NSMenuItem,
                NSStatusBar,
                NSVariableStatusItemLength,
            )
        except ImportError:
            # 没装 pyobjc（开发环境可能没装）。不算错误，菜单栏入口
            # 只是不可用。
            return False

        try:
            self._target = _make_target(self)
            menu = NSMenu.alloc().init()
            for spec in MENU_SPEC:
                menu.addItem_(self._build_item(NSMenuItem, spec))

            status_bar = NSStatusBar.systemStatusBar()
            # variableLength 在 Objective-C 里是宏不是类方法，PyObjC
            # 拿不到；对应的常量是 NSVariableStatusItemLength（-1），
            # 意思是图标宽度按内容自适应。
            item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
            button = item.button()
            if button is not None:
                image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "arrow.down.circle", "Super Download Manager"
                )
                if image is not None:
                    image.setTemplate_(True)
                    button.setImage_(image)
                button.setToolTip_("Super Download Manager")
            item.setMenu_(menu)
            self._status_item = item
            # NSApplication 要 active 状态菜单栏才显示
            NSApplication.sharedApplication().activateIgnoringOtherApps_(False)
            return True
        except Exception:
            self._status_item = None
            self._target = None
            return False

    def remove(self) -> None:
        """把图标从菜单栏拿掉。"""
        if self._status_item is None:
            return
        try:
            import objc  # noqa: F401
            from AppKit import NSStatusBar

            NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
        except Exception:
            pass
        self._status_item = None
        self._target = None

    def _build_item(self, NSMenuItem: Any, spec: MenuItemSpec) -> Any:
        if spec.separator:
            return NSMenuItem.separatorItem()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            spec.title, f"{spec.action}:", spec.key_equivalent
        )
        if spec.command:
            item.setKeyEquivalentModifierMask_(1 << 20)  # NSEventModifierFlagCommand
        item.setTarget_(self._target)
        return item


def _dispatch(controller: StatusBarController, name: str) -> None:
    """跑一个菜单动作的处理器。出错只吞掉——菜单点坏了不该崩 app。"""
    handler = controller._handlers.get(name)
    if handler is None:
        return
    try:
        handler()
    except Exception:
        pass


def _make_target(controller: StatusBarController) -> Any:
    """造一个能被 NSMenuItem 当 target 的 Objective-C 对象。

    PyObjC 里 action 通过 selector 派发，方法名要写成
    `openMainWindow_` 这种带下划线的形式，对应 selector
    `openMainWindow:`。

    类里只能放符合 ObjC 约定的方法——内部辅助函数得放在类外面，
    否则 PyObjC 会把它也当成 selector，签名对不上就
    BadPrototypeError。
    """
    import objc
    from Foundation import NSObject

    class _Target(NSObject):
        def initWithController_(self, ctrl):
            self = objc.super(_Target, self).init()
            if self is None:
                return None
            self._controller = ctrl
            return self

        def openMainWindow_(self, sender) -> None:
            _dispatch(self._controller, "openMainWindow")

        def addURL_(self, sender) -> None:
            _dispatch(self._controller, "addURL")

        def revealDownloads_(self, sender) -> None:
            _dispatch(self._controller, "revealDownloads")

        def showAbout_(self, sender) -> None:
            _dispatch(self._controller, "showAbout")

        def openSettings_(self, sender) -> None:
            _dispatch(self._controller, "openSettings")

        def quitApp_(self, sender) -> None:
            _dispatch(self._controller, "quitApp")

    return _Target.alloc().initWithController_(controller)
