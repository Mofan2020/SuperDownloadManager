"""IPC 分发器：把请求路由到引擎。

对应 Swift 版 Daemon/IPC/Dispatcher.swift。
"""

from __future__ import annotations

import json
from typing import Any

from .. import paths
from ..engine.categories import category_for_extension, category_from_url
from ..engine.download_engine import DownloadEngine, StartSpec, TaskSummary
from ..logging_setup import CAT_IPC, get_logger
from .protocol import (
    METHOD_ADD,
    METHOD_CANCEL,
    METHOD_CONFIG,
    METHOD_LIST,
    METHOD_PAUSE,
    METHOD_PING,
    METHOD_RELINK,
    METHOD_RESUME,
    IPCRequest,
    IPCResponse,
)

log = get_logger()


def _classify_enabled() -> bool:
    """用户有没有关掉自动分类。

    读的是 GUI 和 CLI 共享的那份设置（`sdm.classify.enabled`）。
    读不到就当开着——默认行为是分类。
    """
    try:
        from ..settings import SettingsStore

        return bool(SettingsStore.shared().settings.classify_enabled)
    except Exception:
        return True


class Dispatcher:
    """把 IPC 请求转成引擎调用。"""

    def __init__(self, engine: DownloadEngine | None = None) -> None:
        self.engine = engine or DownloadEngine()

    async def handle(self, raw: str | bytes) -> str:
        """处理一条请求，返回响应 JSON。"""
        try:
            request = IPCRequest.from_json(raw)
        except (ValueError, json.JSONDecodeError) as err:
            return IPCResponse.failure("?", f"decode: {err}").to_json()
        try:
            response = await self.dispatch(request)
        except Exception as err:
            log.error(CAT_IPC, f"请求处理失败: {err}")
            response = IPCResponse.failure(request.id, str(err))
        return response.to_json()

    async def dispatch(self, request: IPCRequest) -> IPCResponse:
        params = request.params or {}
        method = request.method

        if method == METHOD_PING:
            return IPCResponse.success(request.id, {"pong": True})

        if method == METHOD_ADD:
            return await self._add(request, params)

        if method == METHOD_LIST:
            return IPCResponse.success(
                request.id,
                [self._task_entry(task) for task in self.engine.snapshot()],
            )

        if method in (METHOD_PAUSE, METHOD_RESUME, METHOD_CANCEL):
            return await self._control(request, method, params)

        if method == METHOD_CONFIG:
            return await self._config(request, params)

        if method == METHOD_RELINK:
            return await self._relink(request, params)

        return IPCResponse.failure(request.id, f"unknown method '{method}'")

    # -- 各方法 ------------------------------------------------------

    @staticmethod
    def _task_entry(task: TaskSummary) -> dict[str, Any]:
        """把任务转成 list 方法的条目。

        **必须带 `total` 键**——Swift 版
        `Daemon/IPC/Dispatcher.swift:103` 用的就是 `total`：

        ```swift
        "total": t.totalBytes.map { .int($0) } ?? AnyJSON.null,
        ```

        Python 内部的字段叫 `total_bytes`，但外部脚本是按 Swift
        协议写的，读 `total`。少了这个键就落空（取到 null，
        分不清「大小未知」和「没有这个字段」）。

        大小未知时是 **None 不是 0**——0 表示「服务器说文件是 0 字节」，
        None 才是「不知道多大」。Swift 那边也是 `?? AnyJSON.null`。

        Swift 协议里的另外四个键（`id` / `url` / `state` /
        `bytes_done`）保持原样。Python 多出来的字段
        （speed_bytes_per_sec / filename / category 等）是**增量**，
        不破坏按 Swift 协议写的读取方。
        """
        payload = task.to_dict()
        payload["total"] = task.total_bytes
        return payload

    async def _add(self, request: IPCRequest, params: dict[str, Any]) -> IPCResponse:
        url = params.get("url")
        if not isinstance(url, str) or not url:
            return IPCResponse.failure(request.id, "add: 'url' (string) required")
        if not url.lower().startswith(("http://", "https://")):
            return IPCResponse.failure(request.id, "add: 'url' must be http(s)")

        filename = params.get("filename")
        category = params.get("category")
        threads = params.get("threads")
        referer = params.get("referer")

        # **文件名没给就留空**，不要拿 URL 猜一个填进去。
        #
        # 引擎的判据是 `not spec.filename_override`（download_engine.py
        # 里探测成功后重锚到服务器 suggested_filename 的那段）。
        # 一旦这里填了猜测值，判据为假，**服务器返回的真实文件名
        # 就被挡掉了**——「用服务器给的文件名」这个功能在 CLI 路径
        # 直接失效。Swift 版对应位置传的是 nil（Engine/
        # DispatchServer.swift:93）。
        filename_override = filename if isinstance(filename, str) and filename else None

        base = paths.download_dir()
        # 关闭自动分类时一律落基础目录，不进分类子目录。
        #
        # Swift 对照：App/MainWindow/DownloadListStore.swift:367
        # 是 `if !classifyEnabled { return downloads }`。GUI 和 CLI
        # 共享同一个设置文件，两边语义必须一样——用户在设置里关了
        # 分类，从命令行加的任务也得落基础目录。
        if not _classify_enabled():
            folder = ""
        elif isinstance(category, str) and category:
            cat = category_for_extension(category)
            folder = cat.folder_name if cat else category.capitalize()
        else:
            folder = category_from_url(url).folder_name
        destination_dir = base / folder if folder else base
        destination_dir.mkdir(parents=True, exist_ok=True)

        spec = StartSpec(
            url=url,
            destination_directory=destination_dir,
            filename_override=filename_override,
            max_threads_override=int(threads) if isinstance(threads, int) else None,
            referer=referer if isinstance(referer, str) else None,
        )
        ids = await self.engine.start([spec])
        task_id = ids[0] if ids else ""
        task = self.engine.get(task_id)
        return IPCResponse.success(
            request.id,
            {
                "task_id": task_id,
                "url": url,
                "state": task.state if task else "queued",
                "destination": task.destination if task else "",
            },
        )

    async def _control(self, request: IPCRequest, method: str, params: dict[str, Any]) -> IPCResponse:
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            return IPCResponse.failure(request.id, f"{method}: 'id' required")
        if self.engine.get(task_id) is None:
            return IPCResponse.failure(request.id, f"{method}: unknown task '{task_id}'")

        if method == METHOD_PAUSE:
            await self.engine.pause(task_id)
        elif method == METHOD_RESUME:
            await self.engine.resume(task_id)
        elif method == METHOD_CANCEL:
            await self.engine.cancel(task_id)

        task = self.engine.get(task_id)
        return IPCResponse.success(
            request.id,
            {
                "method": method,
                "applied": True,
                "state": task.state if task else None,
            },
        )

    async def _config(self, request: IPCRequest, params: dict[str, Any]) -> IPCResponse:
        key = params.get("key")
        if not isinstance(key, str) or not key:
            return IPCResponse.failure(request.id, "config: 'key' required")
        value = params.get("value")

        if key == "speed":
            if isinstance(value, (int, float)):
                await self.engine.set_speed_limit(float(value))
            return IPCResponse.success(request.id, {"speed": value})
        if key == "threads":
            if isinstance(value, int):
                self.engine.set_max_threads(value)
            return IPCResponse.success(request.id, {"threads": value})
        return IPCResponse.failure(request.id, f"config: unknown key '{key}'")

    async def _relink(self, request: IPCRequest, params: dict[str, Any]) -> IPCResponse:
        task_id = params.get("id")
        url = params.get("url")
        if not isinstance(task_id, str) or not task_id:
            return IPCResponse.failure(request.id, "relink: 'id' required")
        if not isinstance(url, str) or not url:
            return IPCResponse.failure(request.id, "relink: 'url' required")
        if self.engine.get(task_id) is None:
            return IPCResponse.failure(request.id, f"relink: unknown task '{task_id}'")

        result = await self.engine.relink(task_id, url)
        payload = dict(result)
        payload["new_url"] = url
        return IPCResponse.success(request.id, payload)
