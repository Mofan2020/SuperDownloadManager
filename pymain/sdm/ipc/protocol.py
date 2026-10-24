"""IPC 协议：请求与响应的数据结构。

对应 Swift 版 Engine/IPCProtocol.swift。

请求：
    {"id": "...", "method": "add", "params": {...}}
响应：
    {"id": "...", "ok": true,  "result": {...}}
    {"id": "...", "ok": false, "error": "..."}

方法：
    add(url, referer?, filename?, category?, threads?)  → 返回 task_id
    list()                                              → 全部任务快照
    pause(id) / resume(id) / cancel(id)                 → 控制任务
    config(key, value)                                  → 读/写配置
    relink(id, url)                                     → 换链接续传
    ping()                                              → {"pong": true}
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

# 方法名常量——两边都用这些字符串，别写错字
METHOD_ADD = "add"
METHOD_LIST = "list"
METHOD_PAUSE = "pause"
METHOD_RESUME = "resume"
METHOD_CANCEL = "cancel"
METHOD_CONFIG = "config"
METHOD_RELINK = "relink"
METHOD_PING = "ping"

ALL_METHODS = frozenset({
    METHOD_ADD, METHOD_LIST, METHOD_PAUSE, METHOD_RESUME,
    METHOD_CANCEL, METHOD_CONFIG, METHOD_RELINK, METHOD_PING,
})


@dataclass
class IPCRequest:
    id: str
    method: str
    params: dict[str, Any] | None = None

    @classmethod
    def from_json(cls, raw: str | bytes) -> "IPCRequest":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request must be a JSON object")
        request_id = data.get("id") or str(uuid.uuid4())
        method = data.get("method")
        if not isinstance(method, str):
            raise ValueError("request missing 'method'")
        params = data.get("params")
        if params is not None and not isinstance(params, dict):
            raise ValueError("'params' must be an object")
        return cls(id=str(request_id), method=method, params=params)

    def to_json(self) -> str:
        payload: dict[str, Any] = {"id": self.id, "method": self.method}
        if self.params is not None:
            payload["params"] = self.params
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class IPCResponse:
    id: str
    ok: bool
    result: Any = None
    error: str | None = None

    @classmethod
    def success(cls, request_id: str, result: Any = None) -> "IPCResponse":
        return cls(id=request_id, ok=True, result=result)

    @classmethod
    def failure(cls, request_id: str, error: str) -> "IPCResponse":
        return cls(id=request_id, ok=False, error=error)

    def to_json(self) -> str:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.ok:
            payload["result"] = self.result
        else:
            payload["error"] = self.error
        return json.dumps(payload, ensure_ascii=False)
