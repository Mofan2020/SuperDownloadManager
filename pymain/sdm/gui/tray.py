"""Windows 系统托盘（pystray）。

对应契约 §5.2（规格 §4.5）：Windows 上没有 macOS 的菜单栏，必须有
一个**常驻**的入口，否则关掉窗口就等于把应用关掉了。

**菜单跟 macOS 菜单栏逐项一致**（契约 §5.2：「与 macOS 菜单栏行为
一致的菜单」）。两边共用 `macos_status.MENU_SPEC` 这一份规格——
**不复制一份**。复制的话迟早两边会不一样：加了一个菜单项只改了一边，
用户就会发现「Windows 上少一项」。

**pystray 是可选依赖。** 没装、或者不在 Windows 上时，
import 这个模块不会失败——只是 `available()` 返回 False，
托盘不会出现。托盘是常驻入口，不该挡住应用启动。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from ..logging_setup import CAT_GUI, get_logger
from .macos_status import (
    CLIPBOARD_SETTING_KEY,
    MENU_SPEC,
    clipboard_monitor_enabled,
    format_status_line,
    set_clipboard_monitor_enabled,
)

log = get_logger()

try:  # pragma: no cover — 只在装了 pystray 的 Windows 上有
    import pystray
    from pystray import Menu, MenuItem
except ImportError:  # pragma: no cover
    pystray = None  # type: ignore[assignment]
    Menu = None  # type: ignore[assignment]
    MenuItem = None  # type: ignore[assignment]


#: 托盘菜单的规格。**跟 macOS 菜单栏是同一份**（同一个 list 对象），
#: 所以两边天生一致。
TRAY_MENU_SPEC = MENU_SPEC


def available() -> bool:
    """这个平台能不能装托盘图标。

    两个条件都要满足：Windows + 装了 pystray。
    """
    return sys.platform == "win32" and pystray is not None


class TrayController:
    """pystray 托盘图标。

    跟 `macos_status.StatusBarController` 是**平级的两个实现**：
    菜单项、动作、状态行文案都一样，只是底下的控件不同
    （NSStatusItem vs pystray.Icon）。

    状态行的刷新方式也一样：由 GUI 那边按 ~1.5 秒的周期调
    `update_status()`，不自己起线程。
    """

    def __init__(
        self,
        bridge: Any,
        on_show: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self.bridge = bridge
        self.on_show = on_show
        self.on_quit = on_quit
        self._icon: Any = None
        self._status_text = "空闲"
        self._clipboard_checked = False

    # -- 状态 --------------------------------------------------------

    @property
    def status_text(self) -> str:
        """最近一次的状态行文案。"""
        return self._status_text

    def update_status(self, speed: float, active_count: int, limit: float = 0.0) -> str:
        """更新状态行。返回新的文案。"""
        self._status_text = format_status_line(speed, active_count, limit)
        self._refresh_menu()
        return self._status_text

    def set_clipboard_checked(self, checked: bool) -> None:
        """同步「剪贴板监控」的勾选状态。

        pystray 的 MenuItem 的 `checked` 是个回调，每次菜单打开时
        才求值——所以这里只要把状态记下来就行，不用重建菜单。
        """
        self._clipboard_checked = bool(checked)
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        """菜单里有动态属性（状态行文案、勾选状态）。

        **外部改了动态属性之后要调 `Icon.update_menu()`**——pystray 的
        文档明说了：不是所有平台都在菜单显示的那一刻才生成菜单，
        外部改了动态属性不同步的话，用户看到的还是旧值。
        """
        if self._icon is None:
            return
        try:
            self._icon.update_menu()
        except Exception as err:
            log.debug(CAT_GUI, f"托盘菜单刷新失败: {err}")

    # -- 生命周期 ----------------------------------------------------

    def install(self) -> bool:
        """把托盘图标装上。返回是否成功。"""
        if not available():
            return False
        try:
            image = self._make_image()
        except Exception as err:
            log.warning(CAT_GUI, f"托盘图标生成失败: {err}")
            return False
        if image is None:
            return False
        try:
            self._icon = pystray.Icon("sdm", image, "Super Download Manager", self._build_menu())
            # **run_detached 而不是 run。** run() 会占住当前线程不返回，
            # GUI 主线程就停在那儿了。pystray 自己起一个线程跑循环。
            self._icon.run_detached()
            return True
        except Exception as err:
            log.warning(CAT_GUI, f"托盘图标安装失败: {err}")
            self._icon = None
            return False

    def remove(self) -> None:
        """把托盘图标摘掉。"""
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None

    # -- 内部 --------------------------------------------------------

    def _make_image(self) -> Any:
        """托盘图标。pystray 要一个 PIL Image。"""
        from PIL import Image, ImageDraw

        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((2, 2, size - 2, size - 2), fill=(30, 110, 210, 255))
        # 一个向下的箭头，意思跟 macOS 那个 SF Symbol 差不多。
        draw.polygon(
            [(size // 2 - 6, size // 2 - 14), (size // 2 + 6, size // 2 - 14), (size // 2, size // 2 + 4)],
            fill=(255, 255, 255, 255),
        )
        draw.rectangle((size // 2 - 2, size // 2 + 6, size // 2 + 2, size // 2 + 16), fill=(255, 255, 255, 255))
        return image

    def _build_menu(self) -> Any:
        """按 `MENU_SPEC` 拼出托盘菜单。"""
        items = []
        for spec in TRAY_MENU_SPEC:
            if spec.separator:
                items.append(Menu.SEPARATOR)
                continue
            if spec.action == "statusLine":
                # 状态行：跟 macOS 一样，只显示不能点。
                #
                # **动态属性的回调可能带一个 item 参数**（pystray 的
                # 文档里 `checked=lambda item: state` 就是这么写的），
                # 所以写成 `lambda item=None:`——带不带参数都能接住。
                items.append(
                    MenuItem(
                        lambda item=None: self._status_text,
                        None,
                        enabled=lambda item=None: False,
                    )
                )
                continue
            if spec.action == "clipboardMonitor":
                items.append(
                    MenuItem(
                        spec.title,
                        self._toggle_clipboard,
                        checked=lambda item=None: self._clipboard_checked,
                    )
                )
                continue
            items.append(MenuItem(spec.title, self._action_for(spec.action)))

        return Menu(*items)

    def _action_for(self, action: str) -> Callable[..., None]:
        """菜单动作 → 处理函数。

        **pystray 的 action 签名是 `(icon, item)`，不是无参。**
        pystray 点菜单项的时候是 `action(icon, item)` 这样调的——
        写成无参的话点一下就 TypeError，菜单看着在、点了没反应。
        所以这里统一用 `(*_args)` 收下那两个参数。
        """

        def _noop(*_args: Any) -> None:
            return None

        if action == "openMainWindow":
            return self._show_window
        if action == "pauseAll":
            return self._pause_all
        if action == "resumeAll":
            return self._resume_all
        if action == "quitApp":
            return self._quit
        return _noop

    def _show_window(self, *_args: Any) -> None:
        if self.on_show is not None:
            try:
                self.on_show()
            except Exception as err:
                log.debug(CAT_GUI, f"托盘唤起主窗口失败: {err}")

    def _pause_all(self, *_args: Any) -> None:
        try:
            self.bridge.pause_all()
        except Exception as err:
            log.debug(CAT_GUI, f"托盘暂停全部失败: {err}")

    def _resume_all(self, *_args: Any) -> None:
        try:
            self.bridge.resume_all()
        except Exception as err:
            log.debug(CAT_GUI, f"托盘继续全部失败: {err}")

    def _toggle_clipboard(self, *_args: Any) -> None:
        """切换「剪贴板监控」。

        **写的是同一个设置键**（`capture_clipboard`）——菜单栏、托盘、
        设置页说的是同一件事。
        """
        try:
            new_value = not self._clipboard_checked
            set_clipboard_monitor_enabled(new_value)
            self._clipboard_checked = new_value
        except Exception as err:
            log.debug(CAT_GUI, f"托盘切换剪贴板监控失败: {err}")

    def _quit(self, *_args: Any) -> None:
        """退出：先把托盘摘掉，再退出应用。"""
        self.remove()
        if self.on_quit is not None:
            try:
                self.on_quit()
            except Exception as err:
                log.debug(CAT_GUI, f"托盘退出失败: {err}")


def install(
    bridge: Any,
    on_show: Callable[[], None] | None = None,
    on_quit: Callable[[], None] | None = None,
) -> TrayController | None:
    """装托盘图标。装不上返回 None。

    返回 controller 是为了让调用方能 `update_status()` / `remove()`——
    **局部变量一出函数就被 GC 的话，托盘图标会凭空消失**。
    """
    controller = TrayController(bridge, on_show=on_show, on_quit=on_quit)
    try:
        controller._clipboard_checked = clipboard_monitor_enabled()
    except Exception as err:
        log.debug(CAT_GUI, f"读剪贴板监控开关失败: {err}")
    if controller.install():
        log.info(CAT_GUI, "系统托盘图标已装上")
        return controller
    log.info(CAT_GUI, "系统托盘图标没装上（不是 Windows 或缺 pystray）")
    return None


#: 设置键名对外可见，方便别处（设置页）引用同一个键。
CLIPBOARD_SETTING = CLIPBOARD_SETTING_KEY
