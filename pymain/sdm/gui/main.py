"""GUI 入口：创建窗口、绑定桥接层。

对应 Swift 版 App/SDMApp.swift。

用法：
    python -m sdm.gui.main
    python -m sdm.gui.main --debug    # 打开开发者工具
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

from .. import paths
from ..clipboard import ClipboardMonitor
from ..console import force_utf8_stdio
from ..engine.download_engine import DownloadEngine
from ..i18n import I18n
from ..ipc.dispatch_server import DispatchServer
from ..logging_setup import CAT_GUI, get_logger
from ..settings import SettingsStore
from .bridge import GuiBridge

log = get_logger()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sdm", description="Super Download Manager")
    parser.add_argument("--debug", action="store_true", help="打开开发者工具并输出详细日志")
    parser.add_argument("--self-test", action="store_true", help="跑一遍无界面自检")
    parser.add_argument("--report", help="自检报告输出路径")
    return parser.parse_args(argv)


def run_self_test(report_path: str | None = None) -> int:
    """无界面自检：检查依赖、路径、引擎能否起来。

    CI 用它挡住「打包出来才发现起不来」。
    """
    force_utf8_stdio()
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 依赖
    try:
        import httpx

        check("httpx", True, httpx.__version__)
    except ImportError as err:
        check("httpx", False, str(err))

    try:
        import webview  # noqa: F401

        check("pywebview", True, "已安装")
    except ImportError as err:
        check("pywebview", False, str(err))

    # 路径
    try:
        paths.ensure_dirs()
        check("配置目录", True, str(paths.config_dir()))
    except OSError as err:
        check("配置目录", False, str(err))

    # 引擎
    try:
        engine = DownloadEngine()
        check("引擎初始化", True, f"并发上限 {engine.parallelism_cap}")
    except Exception as err:
        check("引擎初始化", False, str(err))

    # 文案
    try:
        catalog = I18n("zh-Hans").catalog()
        check("文案表", len(catalog) > 100, f"{len(catalog)} 条")
    except Exception as err:
        check("文案表", False, str(err))

    # 前端资源
    try:
        assets = Path(__file__).parent / "assets"
        index = assets / "index.html"
        check("前端资源", index.exists(), str(index))
    except Exception as err:
        check("前端资源", False, str(err))

    # 通知通道
    #
    # 打包成 .app 时有 bundle identity，通知走 UNUserNotificationCenter
    # （能点通知回主窗口）；从源码跑没有，退回 osascript。
    # 两种都得能跑通——这里只探「能不能用」，不真发通知。
    try:
        from .. import notifications

        if notifications._bundle_identifier():
            detail = "UNUserNotificationCenter"
            if notifications._un_available():
                detail += "（框架缺失，已退回系统通道）"
        else:
            detail = "系统通知（无 bundle identity）"
        check("通知通道", True, detail)
    except Exception as err:
        check("通知通道", False, str(err))

    # URL scheme 接收器
    #
    # 这个模块是 F064 的实现（Apple Event 接收 URL scheme）。
    # PyInstaller 靠静态分析找模块——`from . import url_scheme` 写在
    # main() 里，静态分析能找到，但「新模块忘了加 hidden import」
    # 这种坑只有真打包才知道。自检里 import 一次，进包了就说明
    # 分析器认得它。
    try:
        from . import url_scheme

        parsed = url_scheme.parse_scheme_url(
            "superdownloadmanager://add?url=http://example.com/a.zip"
        )
        check("URL scheme", parsed == "http://example.com/a.zip", parsed or "解析失败")
    except Exception as err:
        check("URL scheme", False, str(err))

    # Dock 图标策略（F148）
    #
    # 跟 URL scheme 同一个道理：这个模块是后加的，PyInstaller
    # 静态分析认不认得它，只有打包后才知道。这里 import 一次
    # 并调一次函数，进了包就说明两件事都对。
    try:
        from . import macos_status

        policy = macos_status.current_activation_policy()
        check(
            "Dock 策略",
            policy != -1 or not macos_status.available(),
            f"当前策略 {policy}，宽限 {macos_status.DOCK_HIDE_GRACE_SECONDS}s",
        )
    except Exception as err:
        check("Dock 策略", False, str(err))

    # 分类解析（F058）
    #
    # 分类名和扩展名都得能解析——这个函数在 GUI 之外没法触发，
    # 打包错了只有用户点了下载才发现。
    try:
        from ..engine.categories import DownloadCategory
        from .bridge import _resolve_category

        by_name = _resolve_category("video")
        by_ext = _resolve_category("mp4")
        check(
            "分类解析",
            by_name is DownloadCategory.VIDEO and by_ext is DownloadCategory.VIDEO,
            f"分类名 {by_name and by_name.value}，扩展名 {by_ext and by_ext.value}",
        )
    except Exception as err:
        check("分类解析", False, str(err))

    # 中英文案键一致（F076 新增的列名等）
    #
    # 漏一边的话，另一种语言下那条会显示成 key 本身。
    #
    # **直接读原始 JSON，不走 I18n.catalog()**——后者返回的是
    # 「en 打底 + 当前语言覆盖」的合并表，key 集合恒等，
    # 用它检查永远发现不了缺键。
    try:
        from ..i18n import CATALOGS, _load_catalogs

        _load_catalogs()
        zh = set(CATALOGS.get("zh-Hans") or {})
        en = set(CATALOGS.get("en") or {})
        missing = zh ^ en
        check(
            "文案键一致",
            bool(zh) and bool(en) and not missing,
            f"缺 {sorted(missing)[:5]}" if missing else f"{len(zh)} 键",
        )
    except Exception as err:
        check("文案键一致", False, str(err))

    ok = all(passed for _, passed, _ in checks)
    for name, passed, detail in checks:
        print(f"  {'OK  ' if passed else 'FAIL'} {name}: {detail}")

    if report_path:
        Path(report_path).write_text(
            json.dumps(
                {"ok": ok, "checks": [{"name": n, "ok": p, "detail": d} for n, p, d in checks]},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0 if ok else 1


def _install_cli_async(bridge) -> None:
    """后台线程：把 CLI 装进 PATH、注册 daemon，然后把结果推给前端。

    失败只记日志，不弹窗——启动时出点小状况不该挡住用户用软件。
    """
    try:
        from .bootstrap import ensure_installed

        result = ensure_installed()
    except Exception as err:
        log.warning(CAT_GUI, f"安装命令行工具失败: {err}")
        return

    cli = result.get("cli", {})
    daemon = result.get("daemon", {})
    if cli.get("ok"):
        log.info(CAT_GUI, f"命令行工具: {cli.get('path')}（{cli.get('reason')}）")
    elif cli.get("reason") not in ("not_bundle",):
        log.warning(CAT_GUI, f"命令行工具没装上: {cli.get('reason')} {cli.get('path', '')}")
    if daemon.get("ok"):
        log.info(CAT_GUI, f"后台守护: {daemon.get('path')}（{daemon.get('reason')}）")
    elif daemon.get("reason") not in ("not_bundle",):
        log.warning(CAT_GUI, f"后台守护没注册上: {daemon.get('reason')}")

    try:
        bridge._call_js("onCliInstall", result)
    except Exception:
        pass


def _install_status_bar(bridge, window, on_quit=None, on_show=None) -> None:
    """装 macOS 菜单栏图标。

    Swift 版有（AppDelegate 手写的 NSStatusItem，MenuBarExtra 在
    macOS 26 上不可靠）。菜单项和快捷键跟 Swift 版一致。

    `on_quit` 是「退出」的回调——关窗不退出之后，菜单栏的退出要走
    它（先置「正在退出」标志位，再 destroy 窗口），否则会被
    closing 拦截挡住。

    `on_show` 是「打开主窗口」的回调——关窗后 Dock 图标会隐藏，
    从菜单栏唤回窗口时要把 Dock 图标恢复。

    装不上就当没有——菜单栏入口是加分项，不该挡住 app 启动。
    """
    from . import macos_status

    if not macos_status.available():
        return

    controller = macos_status.StatusBarController(bridge, window)

    def open_main_window() -> None:
        try:
            window.show()
            window.restore()
        except Exception as err:
            log.debug(CAT_GUI, f"唤起主窗口失败: {err}")
        # 关窗后 Dock 图标是隐藏的，唤回来就得恢复。
        if on_show is not None:
            on_show()

    def add_url() -> None:
        bridge._call_js("openAddDialog", None)

    def reveal_downloads() -> None:
        try:
            from .. import paths

            bridge.reveal_in_finder(str(paths.download_dir()))
        except Exception as err:
            log.debug(CAT_GUI, f"打开下载目录失败: {err}")

    def show_about() -> None:
        try:
            from AppKit import NSAlert

            from ..version import __version__

            alert = NSAlert.alloc().init()
            alert.setMessageText_("Super Download Manager")
            alert.setInformativeText_(f"版本 {__version__}\n开源的跨平台下载管理器")
            alert.runModal()
        except Exception as err:
            log.debug(CAT_GUI, f"关于对话框失败: {err}")

    def open_settings() -> None:
        bridge._call_js("setView", "settings")

    def quit_app() -> None:
        if on_quit is not None:
            on_quit()
            return
        try:
            window.destroy()
        except Exception as err:
            log.debug(CAT_GUI, f"退出失败: {err}")

    controller.on("openMainWindow", open_main_window)
    controller.on("addURL", add_url)
    controller.on("revealDownloads", reveal_downloads)
    controller.on("showAbout", show_about)
    controller.on("openSettings", open_settings)
    controller.on("quitApp", quit_app)

    if controller.install():
        log.info(CAT_GUI, "菜单栏图标已装上")
    else:
        log.warning(CAT_GUI, "菜单栏图标没装上")


def main(argv: list[str] | None = None) -> int:
    force_utf8_stdio()
    args = parse_args(argv)

    if args.self_test:
        return run_self_test(args.report)

    import webview

    paths.ensure_dirs()
    logger = get_logger()
    logger.start(to_stderr=args.debug)

    settings = SettingsStore.shared()
    engine = DownloadEngine()
    bridge = GuiBridge(engine)

    # 通知：权限申请 + delegate + 点击回窗口。
    #
    # Swift 版在 AppDelegate 里做这三件事（App/SDMApp.swift:255-281），
    # 顺序也一样：先装 delegate，再申请权限。
    #
    # 全部要等窗口创建出来才能完成——点击通知要把主窗口拉到
    # 前面，没窗口就无从谈起。所以放在 window 创建之后。

    # 引擎跑在自己的事件循环里——GUI 主线程被 webview 占着，
    # 引擎不能挤在同一个循环里。
    engine_loop = asyncio.new_event_loop()
    engine_thread = threading.Thread(
        target=engine_loop.run_forever,
        name="sdm-engine",
        daemon=True,
    )
    engine_thread.start()
    bridge.set_loop(engine_loop)

    # 扩展 dispatch 服务
    dispatch = DispatchServer(
        on_add=lambda url, filename, referer: bridge._run(
            bridge.add_url(url, filename=filename)
        )
    )
    try:
        dispatch.start()
    except OSError as err:
        log.warning(CAT_GUI, f"扩展 dispatch 服务启动失败（浏览器接管不可用）: {err}")

    # 剪贴板监听
    clipboard = ClipboardMonitor(on_candidate=bridge.set_clipboard_candidate)
    if settings.settings.capture_clipboard:
        clipboard.start()

    assets = Path(__file__).parent / "assets"
    index = assets / "index.html"

    window = webview.create_window(
        "Super Download Manager",
        url=str(index),
        js_api=bridge,
        width=settings.settings.window_width,
        height=settings.settings.window_height,
        min_size=(640, 440),
        resizable=True,
        background_color="#EEF1F6",
        text_select=True,
    )
    bridge.set_window(window)

    # 启动时同步开机自启状态。
    #
    # Swift 对照：App/SDMApp.swift:239 调
    # `LaunchAtLogin.syncFromStorage()`，实现见
    # App/LaunchAtLogin.swift:69-74。
    #
    # **幂等**：设置里说开了、系统里实际状态不一致才动手。已一致
    # 就不重复写 plist、不重复跑 launchctl。
    #
    # 失败不影响启动——开机自启是加分项。
    try:
        result = bridge.sync_open_at_login()
        if result.get("changed"):
            log.info(CAT_GUI, f"开机自启已同步为 {result.get('enabled')}")
    except Exception as err:
        log.debug(CAT_GUI, f"同步开机自启失败: {err}")

    # 通知接线（对应 Swift 版 App/SDMApp.swift 的 setupNotifications）。
    #
    # 三件事，顺序跟 Swift 版一致：
    #   1. 装 delegate——前台也弹横幅（v1.0.7 曾静默吞掉），
    #      点通知把主窗口拉到前面。
    #   2. 申请权限——在启动时就申请，不等窗口 onAppear。
    #
    # 都失败了也不影响启动——通知是锦上添花。
    def _setup_notifications() -> None:
        from .. import notifications

        try:
            if notifications.install_delegate():
                log.info(CAT_GUI, "通知 delegate 已装上")
            else:
                log.info(CAT_GUI, "当前环境无 bundle identity，通知走系统兜底通道")
        except Exception as err:
            log.debug(CAT_GUI, f"通知 delegate 安装失败: {err}")

        try:
            notifications.request_permission()
        except Exception as err:
            log.debug(CAT_GUI, f"通知权限申请失败: {err}")

        # 点通知 → 把主窗口拉到前面。
        #
        # Swift 版是 NSApp.activate + makeKeyAndOrderFront。
        # pywebview 的 show() 内部就是这两步（platforms/cocoa.py:
        # makeKeyAndOrderFront_ + activateIgnoringOtherApps_），
        # 所以直接用 show()。
        def bring_main_window_forward() -> None:
            try:
                window.show()
                window.restore()
            except Exception as err:
                log.debug(CAT_GUI, f"通知点击后唤起主窗口失败: {err}")

        notifications.set_click_handler(bring_main_window_forward)

    _setup_notifications()

    def on_loaded() -> None:
        """页面加载完：把初始状态推给前端。"""
        state = bridge.bootstrap()
        window.evaluate_js(f"onBootstrap({json.dumps(state, ensure_ascii=False)})")
        # 首次启动把 CLI 装进 PATH、注册 daemon。放后台线程里做——
        # launchctl 那几步要读写系统目录，不能挡着窗口起不来。
        threading.Thread(
            target=_install_cli_async,
            args=(bridge,),
            name="sdm-install-cli",
            daemon=True,
        ).start()
        # 菜单栏图标必须在主线程装（NSStatusItem 的要求），这里正好
        # 是主线程。
        _install_status_bar(bridge, window, on_quit=_request_quit, on_show=_restore_dock)

    def on_scheme_url(target: str) -> None:
        """系统送来一个 `superdownloadmanager://` URL。

        目标地址已经剥掉 scheme 外壳了（`url_scheme.parse_scheme_url`
        干的）。这里直接入队下载。

        Swift 版这块是死的——它实现了 handleURLEvent 但没在
        Info.plist 注册 scheme，系统不会送 URL 过来（台账 R-13）。
        Python 版注册了（build.py 写 CFBundleURLTypes），
        接收靠 NSAppleEventManager。
        """
        if not target:
            return
        log.info(CAT_GUI, f"URL scheme 收到下载请求: {target}")
        try:
            bridge._run(bridge.add_url(target))
        except Exception as err:
            log.warning(CAT_GUI, f"URL scheme 入队失败: {err}")

    # 注册 URL scheme 接收器。
    #
    # 非 macOS 返回 False（Windows 那边靠注册表 + 启动参数，
    # 不走 Apple Event）。注册失败也不影响启动。
    try:
        from . import url_scheme

        url_scheme.install(on_scheme_url)
    except Exception as err:
        log.debug(CAT_GUI, f"URL scheme 注册跳过: {err}")

    # -- 关窗不退出 + Dock 图标 ------------------------------------
    #
    # Swift 对照：App/SDMApp.swift:283-289 的
    # `applicationShouldTerminateAfterLastWindowClosed` 返回 false，
    # 关窗后应用继续在菜单栏活着。
    #
    # pywebview 这边靠 closing handler **返回 False** 取消关闭。
    # 源码依据（不是猜的）：
    #   webview/window.py:163  → Event(self, True)，should_lock=True
    #   webview/event.py:53-63 → should_lock 时 handler 同步执行，
    #                            返回值收进 return_values，
    #                            set() 返回 `len(false_values) != 0`
    #   webview/platforms/cocoa.py:1315-1317
    #                          → should_cancel 为真就返回
    #                            Foundation.NO（取消关闭）
    #
    # 注意 handler 的返回值必须是 `False` 本身——返回 None 不算取消。
    quitting = False

    def on_closing() -> bool | None:
        """关窗时拦住，让应用继续在菜单栏活着。

        返回 False = 取消这次关闭。
        """
        if quitting:
            # 正在退出（菜单栏的「退出」）：不拦。
            _shutdown()
            return None

        try:
            window.hide()
        except Exception as err:
            log.debug(CAT_GUI, f"隐藏窗口失败: {err}")

        # 关窗后按设置隐藏 Dock 图标。
        _schedule_dock_hide()
        # 返回 False = 取消关闭，应用继续活着。
        return False

    def _shutdown() -> None:
        """真正退出前的清理。"""
        clipboard.stop()
        dispatch.stop()
        try:
            engine_loop.call_soon_threadsafe(engine_loop.stop)
        except RuntimeError:
            pass

    def _schedule_dock_hide() -> None:
        """关窗后延迟切 Dock 策略。

        Swift 对照：App/SDMApp.swift:444-541 —— **0.15 秒宽限**，
        避免 cmd-W 再点托盘时 Dock 图标闪一下。

        延迟期间如果窗口又变可见了（用户很快点了托盘），就不切。
        """
        if not settings.settings.hide_dock_when_window_closed:
            return

        def _hide() -> None:
            # 宽限期里窗口回来了 → 不切。
            if _window_visible():
                return
            from . import macos_status

            macos_status.set_dock_visible(False)

        from . import macos_status

        timer = threading.Timer(macos_status.DOCK_HIDE_GRACE_SECONDS, _hide)
        timer.daemon = True
        timer.start()

    def _window_visible() -> bool:
        """窗口现在可见吗（用来判断宽限期里有没有被唤回来）。"""
        from . import macos_status

        try:
            return macos_status.has_visible_key_window()
        except Exception:
            return False

    def _restore_dock() -> None:
        """窗口回到前台时把 Dock 图标恢复。

        Swift 对照：App/SDMApp.swift 的 updateDockVisibility
        ——任一窗口变 key 立刻切回 .regular。
        """
        from . import macos_status

        macos_status.set_dock_visible(True)

    # 菜单栏的「退出」要先置标志位，否则会被 closing 拦下来。
    def _request_quit() -> None:
        global quitting
        quitting = True
        try:
            window.destroy()
        except Exception as err:
            log.debug(CAT_GUI, f"退出失败: {err}")

    window.events.loaded += on_loaded
    window.events.closing += on_closing

    # 窗口被唤回时恢复 Dock 图标。
    #
    # pywebview 没有「窗口变 key」事件，shown / restored 是最接近的
    # 信号（窗口显示或从最小化恢复）。菜单栏的「打开主窗口」走
    # window.show() + restore()，也会触发 restored。
    def _on_shown() -> None:
        _restore_dock()

    window.events.shown += _on_shown
    window.events.restored += _on_shown

    try:
        webview.start(debug=args.debug)
    except KeyboardInterrupt:
        pass
    finally:
        clipboard.stop()
        dispatch.stop()
        try:
            engine_loop.call_soon_threadsafe(engine_loop.stop)
        except RuntimeError:
            pass
        logger.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
