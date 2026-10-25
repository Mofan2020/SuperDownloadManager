"""sdm 命令行客户端。

对应 Swift 版 CLI/SDMDM.swift。

子命令（与 Swift 版一一对应）：
    add <url> [--referer R] [--filename F] [--category C] [--threads N]
    list
    pause <id> | resume <id> | cancel <id>
    relink <id> <new_url>
    config speed <bytes/sec> | config threads <count>
    ping

输出：每次请求输出一行 JSON。结果走 stdout，错误走 stderr，
失败时退出码非 0——方便 AI agent 和 shell 脚本调用。
"""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

from ..console import force_utf8_stdio
from ..ipc.client import ClientError, IPCClient
from ..ipc.protocol import (
    METHOD_ADD,
    METHOD_CANCEL,
    METHOD_CONFIG,
    METHOD_LIST,
    METHOD_PAUSE,
    METHOD_PING,
    METHOD_RELINK,
    METHOD_RESUME,
)

USAGE = """用法:
  sdm add <url> [--referer R] [--filename F] [--category C] [--threads N]
  sdm list
  sdm pause <id> | resume <id> | cancel <id>
  sdm relink <id> <new_url>
  sdm config speed <bytes/sec> | config threads <count>
  sdm ping
"""


class ParseError(Exception):
    pass


def parse_args(args: list[str]) -> dict:
    """把命令行参数转成 IPC 请求。"""
    if not args:
        raise ParseError("缺少子命令")

    subcommand = args[0]
    rest = args[1:]

    request_id = str(uuid.uuid4())

    if subcommand == "add":
        if not rest:
            raise ParseError("缺少 URL")
        url = rest[0]
        params: dict[str, Any] = {"url": url}
        i = 1
        while i < len(rest):
            flag = rest[i]
            if flag in ("--referer", "--filename", "--category", "--threads"):
                if i + 1 >= len(rest):
                    raise ParseError(f"缺少 {flag} 的值")
                value = rest[i + 1]
                if flag == "--threads":
                    try:
                        params["threads"] = int(value)
                    except ValueError as err:
                        raise ParseError("--threads 必须是整数") from err
                else:
                    params[flag[2:]] = value
                i += 2
            else:
                raise ParseError(f"未知参数 {flag}")
        return {"id": request_id, "method": METHOD_ADD, "params": params}

    if subcommand == "list":
        return {"id": request_id, "method": METHOD_LIST}

    if subcommand in ("pause", "resume", "cancel"):
        if not rest:
            raise ParseError("缺少任务 ID")
        method = {
            "pause": METHOD_PAUSE,
            "resume": METHOD_RESUME,
            "cancel": METHOD_CANCEL,
        }[subcommand]
        return {"id": request_id, "method": method, "params": {"id": rest[0]}}

    if subcommand == "relink":
        if len(rest) < 2:
            raise ParseError("relink 需要 <id> <new_url>")
        return {
            "id": request_id,
            "method": METHOD_RELINK,
            "params": {"id": rest[0], "url": rest[1]},
        }

    if subcommand == "config":
        if len(rest) < 2:
            raise ParseError("config 需要 <key> <value>")
        key = rest[0]
        if key not in ("speed", "threads"):
            raise ParseError(f"未知的 config 键 '{key}'")
        try:
            value = int(rest[1])
        except ValueError as err:
            raise ParseError("值必须是整数") from err
        return {
            "id": request_id,
            "method": METHOD_CONFIG,
            "params": {"key": key, "value": value},
        }

    if subcommand == "ping":
        return {"id": request_id, "method": METHOD_PING}

    raise ParseError(f"未知子命令 '{subcommand}'")


def run(args: list[str] | None = None) -> int:
    """CLI 入口。返回退出码。"""
    # Windows 控制台默认是 GBK，输出中文路径会抛 UnicodeEncodeError
    force_utf8_stdio()
    if args is None:
        args = sys.argv[1:]

    try:
        request = parse_args(args)
    except ParseError as err:
        print(f"sdm: {err}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 64

    try:
        response = IPCClient().send(request)
    except ClientError as err:
        print(f"sdm: {err}", file=sys.stderr)
        return 1

    if response.get("ok"):
        result = response.get("result")
        if isinstance(result, (dict, list)):
            print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        elif result is None:
            print("{}")
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0

    print(f"sdm: {response.get('error', '未知错误')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(run())
