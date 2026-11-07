"""GUI 托管的 CLI 端点。

**为什么 GUI 要自己开一个 IPC 端点。**

CLI 原来只连 daemon。daemon 有自己的引擎，GUI 也有自己的引擎——
两个进程、两个引擎、两份任务表。命令行加的任务进了 daemon，
GUI 的列表里**根本看不到**，用户会以为命令没生效。

GUI 自己托管一个端点之后，CLI 优先连它：连上了，命令行加的任务
直接进 GUI 那个引擎，界面立刻就能看到。GUI 没跑的时候 CLI 退回
daemon，命令照样能加任务（后台下，GUI 打开后看不到而已）。

**端点跟 daemon 的不是同一个。**

- Unix：`sdm-gui.sock` vs `sdmd.sock`
- Windows：TCP 7957 vs TCP 7956

（7955 是浏览器扩展的 HTTP dispatch 端口，协议完全不一样，
别混。）

**这个端点跟 GUI 走的是同一个引擎。** 处理器把请求丢到引擎自己的
事件循环上跑——引擎的方法有一半是协程，在别的循环里 await 会
直接坏掉。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from typing import Any

from ..ipc.dispatcher import Dispatcher
from ..ipc.server import SocketServer
from ..logging_setup import CAT_IPC, get_logger
from ..version import GUI_CLI_PORT_WINDOWS

log = get_logger()


class GUIIPCServer:
    """GUI 进程内的 IPC 服务端。接的是 GUI 自己的引擎。"""

    def __init__(
        self,
        engine: Any,
        loop: asyncio.AbstractEventLoop,
        timeout: float = 30.0,
    ) -> None:
        self.engine = engine
        self.loop = loop
        self.timeout = timeout
        self._dispatcher = Dispatcher(engine)
        self._server: SocketServer | None = None

    # -- 生命周期 ----------------------------------------------------

    def start(self) -> None:
        """绑定端点并开始服务。

        **绑不上不能挡 GUI 启动。** 端口被占、socket 路径太长、
        权限不够——这些都不该让用户开不了窗口。CLI 会自动退回
        daemon，功能不丢。
        """
        from .. import paths

        server = SocketServer(
            path=paths.gui_socket_path(),
            port=GUI_CLI_PORT_WINDOWS,
            handler=self._handle,
        )
        try:
            server.start()
        except OSError as err:
            log.warning(CAT_IPC, f"GUI 的命令行端点启动失败（CLI 会退回守护进程）: {err}")
            return
        self._server = server
        endpoint = (
            f"127.0.0.1:{GUI_CLI_PORT_WINDOWS}" if paths.is_windows() else paths.gui_socket_path()
        )
        log.info(CAT_IPC, f"GUI 命令行端点已启动: {endpoint}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    # -- 处理器 ------------------------------------------------------

    def _handle(self, raw: str) -> str:
        """处理一条 IPC 请求。

        **请求要丢到引擎的事件循环上跑。** 引擎的方法有一半是协程
        （`start` / `pause` / `resume` / `cancel` / `relink`），在别的
        循环里 await 会直接坏掉——协程绑死在创建它时的循环上。
        """
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._dispatcher.handle(raw), self.loop
            )
            return future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            return json.dumps(
                {"id": "?", "ok": False, "error": "引擎调用超时"}, ensure_ascii=False
            )
        except Exception as err:
            log.error(CAT_IPC, f"GUI 端点处理请求失败: {err}")
            return json.dumps(
                {"id": "?", "ok": False, "error": str(err)}, ensure_ascii=False
            )
