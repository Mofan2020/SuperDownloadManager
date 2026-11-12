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
import threading
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


#: 托盘看门狗的间隔（秒）。
#:
#: 5 秒是权衡：Explorer 重启之后图标消失，用户再注意到并点开程序之前
#: 一般都要好几秒，5 秒足够早；而一次 `FindWindowW` 是微秒级的，跑
#: 5 秒一次对性能没有影响。
TRAY_WATCH_INTERVAL = 5.0


def _shell_tray_hwnd() -> int:
    """任务栏窗口（Shell_TrayWnd）的句柄。**Explorer 重启之后这个值会变。**

    Windows 上托盘图标挂在任务栏上，Explorer 一重启（崩溃、更新、
    手动重启资源管理器），系统会把所有第三方图标清掉，但进程还在跑、
    pystray 也以为自己还挂着。用户看到的就是「过一会儿托盘自己没了」。

    拿句柄是判断这件事最便宜的信号：不用轮询图标是不是还在（那要
    Shell_NotifyIcon，还有副作用），比一次 FindWindowW 就够了。

    非 Windows 或者调用失败都返回 0，看门狗看到 0 就当没事发生。
    """
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        return int(user32.FindWindowW("Shell_TrayWnd", None) or 0)
    except Exception:
        return 0


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
        #: 装图标时那块任务栏的句柄。看门狗靠它判断 Explorer 有没有重启。
        self._tray_hwnd = 0
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

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
            self._tray_hwnd = _shell_tray_hwnd()
            self._start_watchdog()
            return True
        except Exception as err:
            log.warning(CAT_GUI, f"托盘图标安装失败: {err}")
            self._icon = None
            return False

    def remove(self) -> None:
        """把托盘图标摘掉。"""
        self._stop_watchdog()
        if self._icon is None:
            return
        try:
            self._icon.stop()
        except Exception:
            pass
        self._icon = None

    # -- 看门狗 ------------------------------------------------------

    def _start_watchdog(self) -> None:
        """起一个后台线程盯着任务栏有没有被重建。

        **为什么需要它。** 托盘图标是挂在任务栏上的，Explorer 一重启，
        系统会把第三方图标全清掉，而进程还在跑、pystray 也以为自己还
        挂着。用户那边的表现就是「用一会儿托盘自己没了」，而且关掉
        窗口之后应用就再也找不回来。
        """
        if self._watch_thread is not None:
            return
        self._watch_stop.clear()
        thread = threading.Thread(
            target=self._watch_loop, name="sdm-tray-watchdog", daemon=True
        )
        self._watch_thread = thread
        thread.start()

    def _stop_watchdog(self) -> None:
        self._watch_stop.set()
        self._watch_thread = None

    def _watch_loop(self) -> None:
        while not self._watch_stop.wait(TRAY_WATCH_INTERVAL):
            self._watch_tick()

    def _watch_tick(self) -> None:
        """看门狗的一拍：任务栏换了就把图标重新挂上。

        单独拆出来是为了能直接测——不用真起线程、也不用等 5 秒。
        """
        if self._icon is None:
            return
        hwnd = _shell_tray_hwnd()
        hwnd = _shell_tray_hwnd()
        if hwnd == 0 or hwnd == self._tray_hwnd:
            return
        self._tray_hwnd = hwnd
        self._readd()

    def _readd(self) -> None:
        """重新把图标挂到（新的）任务栏上。

        pystray 的 `visible` 走的是 Shell_NotifyIcon：置 False 发
        NIM_DELETE，置 True 发 NIM_ADD。任务栏重建之后旧图标已经不在了，
        NIM_DELETE 失败无所谓，NIM_ADD 才是真正把它挂回去的那一步。
        """
        try:
            self._icon.visible = False
            self._icon.visible = True
            log.info(CAT_GUI, "任务栏重建过，托盘图标已重新挂上")
        except Exception as err:
            log.warning(CAT_GUI, f"托盘图标重新挂上失败: {err}")

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

        **走 bridge 的统一出口**（`GuiBridge.set_clipboard_monitor`）——
        写的是同一个设置键（`capture_clipboard`），写完还会推一次
        `settings_updated`，设置页上的开关跟着变。

        之前托盘自己写设置（`set_clipboard_monitor_enabled`），不推
        事件：用户从托盘勾完，回头看设置页还是「关」的状态。
        """
        try:
            new_value = not self._clipboard_checked
            setter = getattr(self.bridge, "set_clipboard_monitor", None)
            if callable(setter):
                result = setter(new_value)
                if not result.get("ok"):
                    log.debug(CAT_GUI, f"托盘切换剪贴板监控失败: {result.get('error')}")
                    return
            else:
                # 没有这个接口的假 bridge（测试里用）→ 退回老写法。
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
