"""守护进程：后台跑引擎，监听 IPC。

对应 Swift 版 Daemon/main.swift。

在 Unix socket（或 Windows TCP 回环端口）上提供 JSONL 协议。
CLI 客户端 `sdm` 连到这里。

用法：
    python -m sdm.daemon.main
"""

from __future__ import annotations

import asyncio
import signal
import sys

from .. import paths
from ..engine.download_engine import DownloadEngine
from ..ipc.dispatcher import Dispatcher
from ..ipc.server import AsyncSocketServer
from ..logging_setup import CAT_IPC, get_logger

log = get_logger()


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
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
