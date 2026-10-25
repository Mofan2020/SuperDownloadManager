"""守护进程：后台跑引擎，监听 IPC。

对应 Swift 版 Daemon/main.swift。

在 Unix socket（或 Windows TCP 回环端口）上提供 JSONL 协议。
CLI 客户端 `sdm` 连到这里。

用法：
    SDM_DAEMON=1 python -m sdm.daemon.main

**必须显式设置 `SDM_DAEMON=1` 才进守护模式。** 这个二进制是
「后台服务」，不是给人手动敲的——用户误敲了应该得到一句提示
然后干净退出，而不是前台挂着一个不起眼的进程占着 socket。
Swift 版 Daemon/main.swift:17-26 是同样的守卫：环境变量不是
"1" 就往 stderr 打一行提示然后 exit 0。
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from .. import paths
from ..console import force_utf8_stdio
from ..engine.download_engine import DownloadEngine
from ..ipc.dispatcher import Dispatcher
from ..ipc.server import AsyncSocketServer
from ..logging_setup import CAT_IPC, get_logger

log = get_logger()

#: 进守护模式的环境变量。值必须正好是 "1"。
DAEMON_ENV_VAR = "SDM_DAEMON"

DAEMON_HINT = (
    "[sdmd] 这个程序是后台守护进程，不要直接运行。"
    "请通过 LaunchAgent / 计划任务启动，或设置 SDM_DAEMON=1。"
)


def daemon_mode_enabled() -> bool:
    """环境变量 SDM_DAEMON 等于 "1" 才进守护模式。

    别的值（"0"、"true"、空串）都不算——守卫要么放行要么拦住，
    半吊子的解析会让人猜「到底哪个值管用」。
    """
    return os.environ.get(DAEMON_ENV_VAR) == "1"


async def run() -> int:
    paths.ensure_dirs()
    engine = DownloadEngine()
    dispatcher = Dispatcher(engine)
    server = AsyncSocketServer(handler=dispatcher.handle)

    try:
        await server.start()
    except OSError as err:
        print(f"[sdmd] 启动失败: {err}", file=sys.stderr)
        return 1

    endpoint = f"127.0.0.1:{server.port}" if paths.is_windows() else server.path
    print(f"[sdmd] 监听中: {endpoint}", flush=True)
    log.info(CAT_IPC, f"守护进程启动，监听 {endpoint}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            pass

    try:
        await stop.wait()
    finally:
        await server.stop()
        await engine.aclose()
        log.info(CAT_IPC, "守护进程已停止")
    return 0


def main() -> int:
    # Windows 控制台默认是 GBK，日志里的中文路径会抛
    # UnicodeEncodeError，daemon 直接起不来
    force_utf8_stdio()
    if not daemon_mode_enabled():
        # 提示走 stderr，退出码 0——这不是错误，是「用法不对」。
        # Swift 版也是 exit(0)（Daemon/main.swift:21）。
        print(DAEMON_HINT, file=sys.stderr)
        return 0
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
