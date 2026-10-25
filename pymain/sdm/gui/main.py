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


def _install_status_bar(bridge, window) -> None:
    """装 macOS 菜单栏图标。

    Swift 版有（AppDelegate 手写的 NSStatusItem，MenuBarExtra 在
    macOS 26 上不可靠）。菜单项和快捷键跟 Swift 版一致。

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
        _install_status_bar(bridge, window)

    def on_url_request() -> None:
        """处理 `superdownloadmanager://` URL scheme。

        Swift 版实现了 handleURLEvent 但从没在 Info.plist 注册
        （风险项 R-13），所以那个入口实际是死的。Python 版把处理逻辑
        补齐并做了端到端验证；scheme 的系统级注册在打包配置里
        （macOS 的 Info.plist / Windows 的注册表）。
        """
        try:
            url = window.get_current_url()
        except Exception:
            return
        if not url or not url.startswith("superdownloadmanager://"):
            return
        # superdownloadmanager://add?url=<目标地址>
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(url)
        if parsed.netloc == "add":
            target = (parse_qs(parsed.query).get("url") or [""])[0]
            if target:
                bridge._run(bridge.add_url(target))
        window.evaluate_js("window.history.replaceState({}, '', 'index.html')")

    def on_closing() -> None:
        clipboard.stop()
        dispatch.stop()
        engine_loop.call_soon_threadsafe(engine_loop.stop)

    window.events.loaded += on_loaded
    window.events.closing += on_closing

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
