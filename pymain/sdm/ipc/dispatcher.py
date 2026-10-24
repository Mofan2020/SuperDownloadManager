"""IPC 分发器：把请求路由到引擎。

对应 Swift 版 Daemon/IPC/Dispatcher.swift。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import paths
from ..engine.categories import DownloadCategory, category_for_extension
from ..engine.download_engine import DownloadEngine, StartSpec
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
        except Exception as err:  # noqa: BLE001
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
                [task.to_dict() for task in self.engine.snapshot()],
            )

        if method in (METHOD_PAUSE, METHOD_RESUME, METHOD_CANCEL):
            return await self._control(request, method, params)

        if method == METHOD_CONFIG:
            return await self._config(request, params)

        if method == METHOD_RELINK:
            return await self._relink(request, params)

        return IPCResponse.failure(request.id, f"unknown method '{method}'")

    # -- 各方法 ------------------------------------------------------

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

        if not isinstance(filename, str) or not filename:
            filename = DownloadEngine.filename_for(url)

        base = paths.download_dir()
        if isinstance(category, str) and category:
            cat = category_for_extension(category)
            folder = cat.folder_name if cat else category.capitalize()
        else:
            from ..engine.categories import category_from_url

            folder = category_from_url(url).folder_name
        destination_dir = base / folder
        destination_dir.mkdir(parents=True, exist_ok=True)

        spec = StartSpec(
            url=url,
            destination_directory=destination_dir,
            filename_override=filename,
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
