"""macOS 菜单栏常驻（NSStatusItem）与 Dock 图标进度。

对应契约 §5.1（菜单栏菜单项 + 1.5 秒状态行）和 §4.4（Dock tile 总体进度）。

**为什么用原生 AppKit 而不是 rumps：** Swift 版最初想用 `MenuBarExtra`
（SwiftUI），那个在 macOS 26 上不可靠（`App/SDMApp.swift` 里的注释写明了），
所以改成了原生 NSStatusItem。Python 侧没有 SwiftUI，但 PyObjC 能直接调
AppKit——macOS 自带，不给用户加依赖。

**结构上分两层：**

- 纯函数（菜单结构、状态行文案、总体进度）——不碰 AppKit，单元测试
  在任何平台上都能跑。
- `StatusBarController`——真正碰 AppKit 的部分，只在 macOS 上跑。

菜单栏和 Windows 托盘**共用同一份菜单规格**（`MENU_SPEC`），
两边行为必须一致——Windows 那边不能比 macOS 少一项。

只在 macOS 上生效。其他平台 `install()` 返回 False，调用方不用判断平台。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..logging_setup import CAT_GUI, get_logger

log = get_logger()


#: 菜单项的纯数据定义。
#:
#: action 是 selector 名（不含冒号），key_equivalent 是快捷键字符，
#: command 标记表示要按住 Command。
#:
#: checkable 表示这是勾选项（勾上 = 开）；enabled=False 表示
#: 「只显示、不能点」——状态行就是这种。
@dataclass(frozen=True)
class MenuItemSpec:
    action: str
    title: str
    key_equivalent: str = ""
    command: bool = False
    separator: bool = False
    checkable: bool = False
    enabled: bool = True


#: 菜单结构。顺序跟契约 §5.1 一字不差：
#: 状态行 → 显示主窗口 → 暂停全部 → 继续全部 → 剪贴板监控 → 退出。
#:
#: **状态行放第一项、而且不可点。** 它是「此刻在干什么」的一行摘要，
#: 用户扫菜单栏第一眼要看的就是它。菜单项的顺序不能改——契约钉死了。
#:
#: 分隔线只为了好看，不算菜单项（测试按「非分隔线项」数个数）。
MENU_SPEC: list[MenuItemSpec] = [
    MenuItemSpec("statusLine", "空闲", enabled=False),
    MenuItemSpec("", "", separator=True),
    MenuItemSpec("openMainWindow", "显示主窗口", "0", command=True),
    MenuItemSpec("pauseAll", "暂停全部"),
    MenuItemSpec("resumeAll", "继续全部"),
    MenuItemSpec("clipboardMonitor", "剪贴板监控", checkable=True),
    MenuItemSpec("", "", separator=True),
    MenuItemSpec("quitApp", "退出", "q", command=True),
]


#: 状态行的刷新周期（秒）。契约 §5.1：「状态行以 ~1.5 秒为周期刷新
#: （不是每帧）」。**不是每帧**——每帧刷会把菜单栏点得一顿一顿的。
STATUS_REFRESH_SECONDS = 1.5

#: Dock 进度条的刷新周期（秒）。比状态行快一点——进度是连续量，
#: 1 秒一跳看着才像在走。
DOCK_REFRESH_SECONDS = 1.0

#: 「剪贴板监控」对应的设置键。菜单栏的勾选、托盘的勾选、设置页的开关
#: 说的是**同一件事**，所以都落到这一个键上。
CLIPBOARD_SETTING_KEY = "capture_clipboard"

#: 算「下载中」的任务状态。跟引擎的 `TaskState.is_active` 保持一致
#: （queued + downloading）。awaiting_new_link 不算——它等的是用户换链接，
#: 不是在下载。
ACTIVE_STATES = frozenset({"queued", "downloading"})

#: NSControlStateValue。AppKit 里是 NSControlStateValueOn = 1 /
#: NSControlStateValueOff = 0（PyObjC 暴露了这两个常量，这里自己写死，
#: 免得为一个常量多 import 一次）。
NS_CONTROL_STATE_OFF = 0
NS_CONTROL_STATE_ON = 1


def available() -> bool:
    """这个平台能不能装菜单栏图标。"""
    return sys.platform == "darwin"


def format_rate(bytes_per_sec: float) -> str:
    """把字节/秒格式化成菜单栏用的短串。

    **跟引擎的 `speed_display.format_rate` 故意不一样：** 引擎那个给
    详情面板用，保留两位小数（2.40 MB/s）；菜单栏是扫一眼的地方，
    契约 §5.1 写的就是「2.4 MB/s」「512 KB/s」——一位小数、整 KB。
    """
    value = max(0.0, float(bytes_per_sec or 0.0))
    if value >= 1_048_576:
        return f"{value / 1_048_576:.1f} MB/s"
    if value >= 1024:
        return f"{value / 1024:.0f} KB/s"
    return f"{value:.0f} B/s"


def format_status_line(speed: float, active_count: int, limit: float = 0.0) -> str:
    """菜单栏状态行的文案。

    - 有任务在跑：`下载中 3 个 · 2.4 MB/s`
    - 设了全局限速：末尾追加 `· 限速 512 KB/s`
    - 空闲：`空闲`
    """
    count = max(0, int(active_count or 0))
    if count == 0:
        return "空闲"
    parts = [f"下载中 {count} 个", format_rate(speed)]
    if limit and float(limit) > 0:
        parts.append(f"限速 {format_rate(limit)}")
    return " · ".join(parts)


def compute_overall_progress(tasks: list[dict]) -> float:
    """所有在跑的任务合起来的总体进度（0~1）。

    **只算活跃任务。** 已经下完的任务留着算进去的话，进度条会一直
    停在 99%——新任务一来就掉回去，看着像在倒退。

    **大小未知的任务直接跳过。** `total_bytes` 是 None 的任务算进来
    会把分母算错。
    """
    done = 0
    total = 0
    for task in tasks or []:
        if (task.get("state") or "") not in ACTIVE_STATES:
            continue
        size = task.get("total_bytes")
        if not size:
            continue
        total += int(size)
        done += int(task.get("bytes_done") or 0)
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, done / total))


def clipboard_monitor_enabled(store: Any = None) -> bool:
    """「剪贴板监控」当前是不是开着。"""
    if store is None:
        from ..settings import SettingsStore

        store = SettingsStore.shared()
    try:
        return bool(store.get(CLIPBOARD_SETTING_KEY))
    except Exception as err:
        log.debug(CAT_GUI, f"读剪贴板监控开关失败: {err}")
        return False


def set_clipboard_monitor_enabled(enabled: bool, store: Any = None) -> bool:
    """开关「剪贴板监控」。

    写的是 `settings.capture_clipboard`——菜单栏、托盘、设置页说的是
    同一件事，不能各写各的（否则菜单栏勾上了、设置页还是关的）。
    """
    if store is None:
        from ..settings import SettingsStore

        store = SettingsStore.shared()
    value = bool(enabled)
    store.set(CLIPBOARD_SETTING_KEY, value)
    return value


def run_on_main_thread(func: Callable[[], None]) -> None:
    """把函数排到 macOS 主线程执行。

    **AppKit 不是线程安全的。** 改 NSMenuItem 的标题、画 NSDockTile 都得
    在主线程做；从后台线程调，轻则图标不刷新，重则整个 app 崩。

    `PyObjCTools.AppHelper.callAfter` 内部是往主线程的 run loop 里投递消息，
    从任何线程调都安全（pywebview 自己的 `window.show()` 也是这么干的）。

    非 macOS、或者 PyObjCTools 不可用时，直接同步调用。
    """
    if sys.platform != "darwin":
        func()
        return
    try:
        from PyObjCTools import AppHelper
    except ImportError:
        func()
        return
    AppHelper.callAfter(func)


# -- Dock 图标显隐 -------------------------------------------------


#: NSApplicationActivationPolicy。实测 AppKit.NSApplicationActivationPolicyRegular
#: = 0、Accessory = 1（不是猜的，PyObjC 暴露了这两个常量）。
POLICY_REGULAR = 0
POLICY_ACCESSORY = 1

#: 关窗后延迟多久再切 Dock 策略。
#:
#: Swift 对照：`App/SDMApp.swift:444-541` 的 updateDockVisibility
#: ——0.15 秒宽限，避免 cmd-W 再点托盘时 Dock 图标闪一下。
DOCK_HIDE_GRACE_SECONDS = 0.15


def current_activation_policy() -> int:
    """当前的激活策略。非 macOS 或 AppKit 不可用时返回 -1。"""
    if sys.platform != "darwin":
        return -1
    try:
        from AppKit import NSApplication

        return int(NSApplication.sharedApplication().activationPolicy())
    except Exception:
        return -1


def set_dock_visible(visible: bool) -> bool:
    """显示或隐藏 Dock 图标。

    `visible=True` → `.regular`（正常出现在 Dock 里）
    `visible=False` → `.accessory`（不占 Dock，只在菜单栏待着）

    Swift 对照：`App/SDMApp.swift:444-541` 的
    `NSApp.setActivationPolicy(.accessory / .regular)`。

    返回是否真的切了。
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication

        target = POLICY_REGULAR if visible else POLICY_ACCESSORY
        app = NSApplication.sharedApplication()
        if int(app.activationPolicy()) == target:
            return False
        app.setActivationPolicy_(target)
        return True
    except Exception:
        return False


def has_visible_key_window() -> bool:
    """现在有没有可见的主窗口。

    **sheet 和 nonactivating panel 不算。** Swift 版
    `App/SDMApp.swift` 的 willClose 处理里明确忽略这两类临时窗口——
    选目录的 NSOpenPanel 关闭时不该触发 Dock 策略切换。

    判据：窗口可见 + 是 key window + 不是 sheet 的父窗口 +
    不是 nonactivating panel。
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication

        for win in NSApplication.sharedApplication().windows():
            try:
                if not win.isVisible():
                    continue
                # sheet 的父窗口在 sheet 打开期间不算「主窗口在前」
                if win.sheetParent() is not None:
                    continue
                # nonactivating panel（工具面板之类）不抢焦点
                style = int(win.styleMask())
                if style & NS_NONACTIVATING_PANEL_MASK:
                    continue
                if win.isKeyWindow():
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


#: NSNonactivatingPanelWindowMask。AppKit 没在 PyObjC 里暴露这个常量
#: （实测 NSNonactivatingPanelWindowMask = N/A），但 AppKit 头文件里
#: 它是 1 << 7 = 128。
NS_NONACTIVATING_PANEL_MASK = 128


# -- Dock 图标进度 --------------------------------------------------


#: Dock 进度图标的绘制边长（点）。128 足够清楚，Dock 会自己缩放。
DOCK_TILE_SIZE = 128


def set_dock_progress(fraction: float) -> bool:
    """在 Dock 图标上画总体下载进度。

    `fraction` 取 0~1。传 0 或者没有活跃任务时恢复默认图标。

    返回是否真的更新了。**非 macOS、或者 AppKit 不可用时是安全的空操作。**
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication
    except ImportError:
        return False
    try:
        tile = NSApplication.sharedApplication().dockTile()
        if tile is None:
            return False
        value = min(1.0, max(0.0, float(fraction or 0.0)))
        if value <= 0.0:
            return clear_dock_progress()
        tile.setContentView_(_render_progress_image(value))
        tile.display()
        return True
    except Exception as err:
        # **别把异常吞干净。** Dock 进度是锦上添花，画不出来不该
        # 把整个 app 带下去——但日志里得留一句。
        log.debug(CAT_GUI, f"Dock 进度更新失败: {err}")
        return False


def clear_dock_progress() -> bool:
    """恢复默认 Dock 图标（去掉进度条）。"""
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication

        tile = NSApplication.sharedApplication().dockTile()
        if tile is None:
            return False
        # **置回 None 才是「恢复默认」。** 留着上一次画的 contentView，
        # 图标就会一直带着上次的进度条。
        tile.setContentView_(None)
        tile.display()
        return True
    except Exception as err:
        log.debug(CAT_GUI, f"Dock 进度清除失败: {err}")
        return False


def _render_progress_image(fraction: float, size: int = DOCK_TILE_SIZE) -> Any:
    """画一张「应用图标 + 底部进度条」的 NSImage。

    **自己画，不用外部素材**——打包时少一个资源文件要带，
    而且进度条的比例得跟着实际下载走，静态图做不到。

    图标本身取 `NSApplication.applicationIconImage()`（打包后是
    .app 的图标）。取不到就画一个圆角底板，总比空白强。
    """
    from AppKit import NSBezierPath, NSColor, NSImage, NSMakeRect
    from Foundation import NSZeroRect

    image = NSImage.alloc().initWithSize_((size, size))
    image.lockFocus()
    try:
        base = _application_icon()
        if base is not None:
            base.drawInRect_fromRect_operation_fraction_(
                NSMakeRect(0, 0, size, size), NSZeroRect, 1.0
            )
        else:
            _draw_fallback_icon(size)

        # 进度条：底下垫一条半透明的深色轨道，上面画一条亮色的进度。
        bar_height = size * 0.13
        margin = size * 0.08
        track = NSMakeRect(margin, margin, size - margin * 2, bar_height)
        radius = bar_height / 2

        NSColor.colorWithCalibratedWhite_alpha_(0.0, 0.45).set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            track, radius, radius
        ).fill()

        # 进度条至少要有一个圆角那么宽，不然小进度会画成一个点。
        width = max(bar_height, (size - margin * 2) * min(1.0, max(0.0, fraction)))
        fill = NSMakeRect(track.origin.x, track.origin.y, width, bar_height)
        try:
            accent = NSColor.systemBlueColor()
        except Exception:
            accent = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.48, 1.0, 1.0)
        accent.set()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            fill, radius, radius
        ).fill()
    finally:
        image.unlockFocus()
    return image


def _application_icon() -> Any:
    """应用图标。取不到返回 None。"""
    try:
        from AppKit import NSApplication

        icon = NSApplication.sharedApplication().applicationIconImage()
        if icon is None:
            return None
        return icon
    except Exception:
        return None


def _draw_fallback_icon(size: int) -> None:
    """没有应用图标时的占位：一个带下箭头的圆角方块。"""
    from AppKit import NSBezierPath, NSColor, NSMakeRect

    body = NSMakeRect(size * 0.12, size * 0.12, size * 0.76, size * 0.76)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.20, 0.45, 0.85, 1.0).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        body, size * 0.18, size * 0.18
    ).fill()


# -- 菜单栏 ---------------------------------------------------------


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
        #: 状态行那一项（NSMenuItem）。菜单建好之后要拿它改标题。
        self._status_menu_item: Any = None
        #: 「剪贴板监控」那一项（NSMenuItem）。要拿它打勾。
        self._clipboard_menu_item: Any = None
        #: 最近一次的状态行文案。
        self._status_text = "空闲"

    # -- 回调注册 ----------------------------------------------------

    def on(self, action: str, handler: Callable[[], None]) -> None:
        """注册一个菜单动作的处理器。"""
        self._handlers[action] = handler

    # -- 状态 --------------------------------------------------------

    @property
    def status_text(self) -> str:
        """最近一次的状态行文案。"""
        return self._status_text

    def update_status(self, speed: float, active_count: int, limit: float = 0.0) -> str:
        """更新状态行。

        **必须在主线程调**（AppKit 不是线程安全的）——调用方用
        `run_on_main_thread()` 包一层。

        返回新的文案，方便调用方记日志 / 测试。
        """
        text = format_status_line(speed, active_count, limit)
        self._status_text = text
        item = self._status_menu_item
        if item is not None:
            try:
                item.setTitle_(text)
            except Exception as err:
                log.debug(CAT_GUI, f"状态行更新失败: {err}")
        return text

    def set_clipboard_checked(self, checked: bool) -> None:
        """同步「剪贴板监控」的勾选状态。"""
        item = self._clipboard_menu_item
        if item is None:
            return
        try:
            item.setState_(NS_CONTROL_STATE_ON if checked else NS_CONTROL_STATE_OFF)
        except Exception as err:
            log.debug(CAT_GUI, f"剪贴板勾选状态更新失败: {err}")

    # -- 生命周期 ----------------------------------------------------

    def install(self) -> bool:
        """把图标装到菜单栏。返回是否成功。"""
        if not available():
            return False
        try:
            from AppKit import (
                NSImage,
                NSMenu,
                NSMenuItem,
                NSStatusBar,
                NSVariableStatusItemLength,
            )
        except ImportError:
            # 没装 pyobjc（开发环境可能没装）。不算错误，菜单栏入口
            # 只是不可用。
            log.warning(
                CAT_GUI,
                "菜单栏图标安装失败：缺少 pyobjc-framework-Cocoa，"
                "菜单栏常驻入口不可用",
            )
            return False

        try:
            self._target = _make_target(self)
            menu = NSMenu.alloc().init()
            for spec in MENU_SPEC:
                item = self._build_item(NSMenuItem, spec)
                menu.addItem_(item)
                if spec.action == "statusLine":
                    self._status_menu_item = item
                elif spec.action == "clipboardMonitor":
                    self._clipboard_menu_item = item

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
            # **不要调 activateIgnoringOtherApps_。**
            # 传 False 本来就是不抢焦点，这行等于什么都没干；但它是
            # AppKit 调用，在非主线程上调会等主线程配合——实测在
            # pywebview 的 loaded 事件回调里卡了 2.4 秒。
            # NSStatusItem 建好之后菜单栏就会显示，不需要额外激活。
            return True
        except Exception as err:
            # **别把异常吞了。** 之前这里只有 `except Exception: return False`，
            # 失败时日志里只有一句「菜单栏图标没装上」，看不到原因。
            log.warning(CAT_GUI, f"菜单栏图标安装失败: {err}")
            self._status_item = None
            self._target = None
            return False

    def remove(self) -> None:
        """把图标从菜单栏拿掉。

        **「退出」必须显式调这个。** 不调的话图标不是被摘掉，而是
        跟着进程一起消失——Dock 上还会闪一下半透明的残影。
        """
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
        self._status_menu_item = None
        self._clipboard_menu_item = None

    def _build_item(self, NSMenuItem: Any, spec: MenuItemSpec) -> Any:
        if spec.separator:
            return NSMenuItem.separatorItem()
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            spec.title, f"{spec.action}:", spec.key_equivalent
        )
        if spec.command:
            item.setKeyEquivalentModifierMask_(1 << 20)  # NSEventModifierFlagCommand
        item.setTarget_(self._target)
        if not spec.enabled:
            # 状态行：只显示，不能点。
            item.setEnabled_(False)
        if spec.checkable:
            # 勾选项默认不勾。真正的状态由 set_clipboard_checked 同步。
            item.setState_(NS_CONTROL_STATE_OFF)
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

        def statusLine_(self, sender) -> None:
            # 状态行是禁用的，点不到。这里只是让 target 响应
            # 菜单里用到的每个 selector（少一个就是点了没反应）。
            _dispatch(self._controller, "statusLine")

        def openMainWindow_(self, sender) -> None:
            _dispatch(self._controller, "openMainWindow")

        def pauseAll_(self, sender) -> None:
            _dispatch(self._controller, "pauseAll")

        def resumeAll_(self, sender) -> None:
            _dispatch(self._controller, "resumeAll")

        def clipboardMonitor_(self, sender) -> None:
            _dispatch(self._controller, "clipboardMonitor")

        def quitApp_(self, sender) -> None:
            _dispatch(self._controller, "quitApp")

    return _Target.alloc().initWithController_(controller)
