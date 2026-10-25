"""浏览器原生宿主（Native Messaging Host）。

契约 §4.1：浏览器接管的主通道是 **Native Messaging**，帧格式是
「4 字节小端长度前缀 + UTF-8 JSON」，走 stdio。

为什么是 stdio 而不是再开一个端口：
    浏览器启动宿主进程时把管道的两端交给它，别的本地进程连都
    连不上——安全边界在浏览器那边。原来的回环 HTTP 端口
    （7955）任何网页都能 fetch（`Access-Control-Allow-Origin: *`），
    所以它只是**降级通道**，不是主通道。

**stdout 上只能有协议帧。** 浏览器把 stdout 当成二进制帧流读，
多一个字节（哪怕只是一行日志）后面全乱。所以这个模块里没有一处
往 stdout 写日志——出错信息一律走 stderr / 日志文件。

派单**不绕过引擎状态机**：宿主是个短命进程，它自己不持有引擎，
而是把请求转给正在运行的应用（守护进程的 Unix socket，和 CLI
走的是同一条路）。这样 GUI 里看到的任务状态、队列顺序、断点续传
都是同一份。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .. import paths
from ..logging_setup import CAT_IPC, get_logger
from ..version import BUNDLE_ID, DISPATCH_PORT
from .client import ClientError, IPCClient
from .protocol import METHOD_ADD, PARAM_HEADERS, PARAM_METHOD, PARAM_POST_DATA

log = get_logger()

#: 原生宿主的应用 ID。契约 §4.1 规定，和 `version.BUNDLE_ID` 同一个。
#:
#: 浏览器按这个名字找宿主清单：宿主清单的文件名必须是
#: `<app id>.json`，里面的 `name` 也要一样。写错一个字符，扩展侧
#: 只会看到一句 "Specified native messaging host not found"。
APP_ID = BUNDLE_ID

#: 单帧负载上限。1 MB。
#:
#: 正常消息只有几百字节（URL + referer + cookie）。但浏览器是
#: 不可信的输入源——一个坏掉或恶意的扩展可以声明一个 4 GB 的长度
#: 前缀。没有上限的话宿主会照着这个数字去读，等于让对方决定我们
#: 吃多少内存。超了直接回错误帧并断开。
MAX_PAYLOAD_BYTES = 1024 * 1024

#: 配对 token 的键名（设置文件里的键，契约 §6）。
#:
#: 跟 `sdm/settings.py` 的 `Settings.browser_pair_token` 同名。
#: 设置模块由别的同事维护，这里不依赖它的 dataclass 字段——
#: 直接读写设置 JSON，未知键原样保留，谁写都不打架。
PAIR_TOKEN_KEY = "browser_pair_token"

#: 配对 token 的独立存档。
#:
#: **为什么要多存一份**：GUI 的 `SettingsStore` 是在内存里改设置、
#: 保存时整个写回去的。它启动时如果还没配过对，内存里的
#: `browser_pair_token` 就是空的；这时它保存一次设置，就把我们
#: 写进设置文件的 token 抹掉了——扩展配对过了，应用却不认了。
#: 这个文件是兜底的真身。
PAIR_TOKEN_FILENAME = "browser_pair_token"

#: Chrome / Edge 的扩展 ID 是商店分配的，装扩展的时候才知道。
#:
#: 清单里的 `allowed_origins` 必须是精确的扩展 ID（`chrome-extension://<id>/`），
#: 写错浏览器会拒绝启动宿主。安装器要用真实 ID 重写清单。
PLACEHOLDER_EXTENSION_ID = "REPLACE_WITH_EXTENSION_ID"

#: Firefox 的扩展 ID 写在 manifest 的 `browser_specific_settings.gecko.id`，
#: 是我们自己定的，固定不变。Firefox 的宿主清单认这个 ID
#: （`allowed_extensions`），不用管运行时随机的 `moz-extension://<uuid>/`。
FIREFOX_EXTENSION_ID = "sdm-firefox@mofan2020.dev"

#: 各浏览器的扩展 origin 前缀。
_ORIGIN_PREFIX = {
    "chrome": "chrome-extension://",
    "chromium": "chrome-extension://",
    "edge": "chrome-extension://",
    "firefox": "moz-extension://",
    "safari": "safari-web-extension://",
}

#: 错误分类（契约 §1）。宿主返回的错误也带分类，前端能直接分流。
_CATEGORY_AUTH = "auth"
_CATEGORY_CLIENT = "client_error"
_CATEGORY_UNKNOWN = "unknown"


class FrameError(Exception):
    """帧格式错误（长度前缀不合法、负载超限、帧被截断）。"""


# -- 帧编解码 -------------------------------------------------------


def encode_frame(payload: dict[str, Any]) -> bytes:
    """把一条消息编码成原生宿主帧：4 字节小端长度 + UTF-8 JSON。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise FrameError(f"消息超过上限（{MAX_PAYLOAD_BYTES // 1024} KB）")
    return len(data).to_bytes(4, "little") + data


def decode_frame(raw: bytes) -> dict[str, Any]:
    """解一帧。坏 JSON 抛 ValueError，调用方负责回错误帧。"""
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise FrameError(f"消息超过上限（{MAX_PAYLOAD_BYTES // 1024} KB）")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise ValueError(f"消息不是合法 JSON: {err}") from err
    if not isinstance(data, dict):
        raise ValueError("消息必须是 JSON 对象")
    return data


def read_frame(stream) -> bytes | None:
    """从流里读一帧。返回 None 表示对端关闭（EOF）。

    连续读多帧请用 `FrameReader`——这个一次性封装会丢掉流里剩下的
    字节（正常情况流里不会有剩下的，但别在循环里用它）。
    """
    return FrameReader(stream).read_frame()


class FrameReader:
    """帧读取器。内部带缓冲，能处理「一次 read 只给几个字节」。

    **必须缓冲，不能假设一次 read 读满。** 管道的 read 返回多少
    取决于当时内核缓冲里有多少，跨包到达是常态。早返回会把半帧
    当完整帧解析，JSON 解析失败，报错还指向完全错误的方向。

    缓冲还解决了「多读」的问题：有些流（自己实现的包装器）不保证
    不多给字节，多出来的部分必须留着给下一帧，不然数据就丢了。
    """

    def __init__(self, stream) -> None:
        self._stream = stream
        self._buffer = b""

    def _read_more(self) -> bool:
        chunk = self._stream.read(4096)
        if not chunk:
            return False
        self._buffer += chunk
        return True

    def read_frame(self) -> bytes | None:
        """读一帧。EOF 返回 None。"""
        while len(self._buffer) < 4:
            if not self._read_more():
                return None
        length = int.from_bytes(self._buffer[:4], "little")
        if length > MAX_PAYLOAD_BYTES:
            raise FrameError(f"消息超过上限（{MAX_PAYLOAD_BYTES // 1024} KB）")
        self._buffer = self._buffer[4:]
        while len(self._buffer) < length:
            if not self._read_more():
                raise FrameError("帧被截断")
        payload = self._buffer[:length]
        self._buffer = self._buffer[length:]
        return payload


# -- 配对 token -----------------------------------------------------


def token_path() -> Path:
    """token 存档路径。"""
    return paths.config_dir() / PAIR_TOKEN_FILENAME


def _read_settings_json() -> dict[str, Any]:
    """读设置文件的原始 JSON。读不了就当空字典。"""
    try:
        raw = paths.settings_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_settings_token(token: str) -> None:
    """把 token 镜像到设置文件的 `browser_pair_token` 键。

    **只改这一个键**，别的键原样写回——设置文件是 GUI 的地盘，
    我们没有资格重排它。写失败就算了，token 已经在自己的文件里。
    """
    path = paths.settings_path()
    data = _read_settings_json()
    if data.get(PAIR_TOKEN_KEY) == token:
        return
    data[PAIR_TOKEN_KEY] = token
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as err:
        log.warning(CAT_IPC, f"配对 token 写设置文件失败（不影响配对）: {err}")


def _write_token_file(token: str) -> None:
    """写 token 存档。权限 0600——别的用户不该读到浏览器的凭据。"""
    path = token_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(token, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
        os.chmod(path, 0o600)
    except OSError as err:
        log.error(CAT_IPC, f"配对 token 落盘失败: {err}")


def load_pair_token() -> str:
    """读当前配对 token。没有就返回空串。

    顺序：**设置文件优先，token 存档兜底**。

    设置文件优先是因为 GUI 可能会重置配对（写一个新 token 进
    设置文件）。那次重置必须真的生效——不然用户在界面上点了
    「重新配对」，扩展手里的旧 token 还能继续用，重置就是假的。
    存档兜底是为了扛住 GUI 用启动时的旧内存副本保存设置、把镜像
    抹掉的情况（GUI 启动时还没配过对，内存里那个字段就是空的）。
    """
    settings_token = _read_settings_json().get(PAIR_TOKEN_KEY)
    if isinstance(settings_token, str) and settings_token:
        return settings_token
    try:
        stored = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return stored


def ensure_pair_token() -> str:
    """拿配对 token，没有就生成一个。

    32 字节随机（`secrets.token_urlsafe`）——这是长期凭据，
    不能用 `random`，也不能短。短 token 撞上本机其它进程的
    概率不是零，而撞上就等于 anybody 都能往下载器塞任务。
    """
    token = load_pair_token()
    if not token:
        token = secrets.token_urlsafe(32)
        _write_token_file(token)
        log.info(CAT_IPC, "已生成新的浏览器配对 token")
    else:
        # 两边保持同步——不管这次是从哪边拿到的，都写回另一边，
        # 别让「设置文件里的」和「存档里的」打起来。
        _write_token_file(token)
    _write_settings_token(token)
    return token


def verify_pair_token(token: Any) -> bool:
    """校验扩展带来的 token。

    用常数时间比较（`hmac.compare_digest`）：普通的 `==` 会在
    第一个不同的字节上就返回，攻击者可以靠响应时间差一位一位
    试出来。本机进程理论上能读到这个 token 文件，但多花这点
    成本没坏处。
    """
    if not isinstance(token, str) or not token:
        return False
    expected = load_pair_token()
    if not expected:
        return False
    return hmac.compare_digest(expected, token)


# -- 宿主清单 -------------------------------------------------------


def host_manifest(
    app_id: str = APP_ID,
    host_path: str | Path = "",
    *,
    browser: str = "chrome",
    extension_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """生成原生宿主清单。

    Chrome 认 `allowed_origins`（精确的扩展 origin），
    Firefox 认 `allowed_extensions`（扩展自己的 gecko id）——
    Firefox 的运行时 origin 是随机的 `moz-extension://<uuid>/`，
    没法写死，所以用 `allowed_extensions`。
    """
    manifest: dict[str, Any] = {
        "name": app_id,
        "description": "Super Download Manager 浏览器接管宿主",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": _allowed_origins(browser, extension_ids),
    }
    if browser == "firefox":
        manifest["allowed_extensions"] = [FIREFOX_EXTENSION_ID]
    return manifest


def _allowed_origins(browser: str, extension_ids: Sequence[str] | None) -> list[str]:
    """算 `allowed_origins`。

    传进来的既可以是完整 origin（`chrome-extension://xxx/`），
    也可以是裸的扩展 ID——裸 ID 要按浏览器补前缀。
    """
    prefix = _ORIGIN_PREFIX.get(browser, _ORIGIN_PREFIX["chrome"])
    ids = list(extension_ids) if extension_ids else [PLACEHOLDER_EXTENSION_ID]
    origins: list[str] = []
    for ext_id in ids:
        if "://" in ext_id:
            origins.append(ext_id if ext_id.endswith("/") else ext_id + "/")
        else:
            origins.append(f"{prefix}{ext_id}/")
    return origins


def host_manifest_dirs() -> list[tuple[str, Path]]:
    """当前平台要写清单的目录：`(浏览器标识, 目录)`。

    契约 §4.1 列的路径。**只写当前平台的**——Windows 上给 macOS
    的目录建出来没有意义，反过来也一样。
    """
    home = Path.home()
    if paths.is_windows():
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [
            ("chrome", local / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts"),
            ("edge", local / "Microsoft" / "Edge" / "User Data" / "NativeMessagingHosts"),
            ("firefox", roaming / "Mozilla" / "NativeMessagingHosts"),
        ]
    support = home / "Library" / "Application Support"
    return [
        ("chrome", support / "Google" / "Chrome" / "NativeMessagingHosts"),
        ("chromium", support / "Chromium" / "NativeMessagingHosts"),
        ("edge", support / "Microsoft Edge" / "NativeMessagingHosts"),
        ("firefox", support / "Mozilla" / "NativeMessagingHosts"),
    ]


def install_host_manifests(
    app_id: str = APP_ID,
    host_path: str | Path = "",
    *,
    extension_ids: Sequence[str] | None = None,
    browsers: Sequence[str] | None = None,
) -> list[Path]:
    """把宿主清单写进各浏览器的 NativeMessagingHosts 目录。

    幂等：同样的参数再跑一次，写出来的文件一模一样。父目录会自动
    建（Chrome 装完常常还没有这个目录）。返回写出的文件路径。

    每个浏览器写的是**自己那一份**清单——Firefox 的那份带
    `allowed_extensions`，Chrome/Edge 的那份只带
    `allowed_origins`。一份清单同时装到两边，Chrome 会因为多出来的
    键拒绝加载。
    """
    wanted = set(browsers) if browsers else None
    written: list[Path] = []
    for browser, directory in host_manifest_dirs():
        if wanted is not None and browser not in wanted:
            continue
        manifest = host_manifest(
            app_id,
            host_path,
            browser=browser,
            extension_ids=extension_ids,
        )
        written.append(_write_manifest(directory, f"{app_id}.json", manifest))
    return written


def _write_manifest(directory: Path, filename: str, manifest: dict[str, Any]) -> Path:
    """写一份清单。原子写（临时文件 + replace），避免浏览器读到半截 JSON。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def host_launcher_script(executable: str, *, windows: bool | None = None) -> str:
    """生成宿主启动脚本的内容。

    契约 §4.1：清单里的 `path` 指向安装目录下的启动脚本
    （macOS 是 sh，Windows 是 .cmd），脚本转调打包好的
    `sdmd-native-host`。浏览器只认清单里的 `path`，不认可执行文件
    本身——macOS 上没有 `.app` 包装的可执行文件直接跑不起来。
    """
    if windows is None:
        windows = paths.is_windows()
    if windows:
        return (
            "@echo off\r\n"
            "REM Super Download Manager 原生宿主启动脚本（契约 §4.1）\r\n"
            f'"{executable}" %*\r\n'
        )
    return (
        "#!/bin/sh\n"
        "# Super Download Manager 原生宿主启动脚本（契约 §4.1）\n"
        "# 浏览器按清单里的 path 拉起这个脚本，脚本转调打包好的宿主。\n"
        f'exec "{executable}" "$@"\n'
    )


def write_host_launcher(
    directory: str | Path,
    executable: str,
    *,
    windows: bool | None = None,
    name: str = "sdmd-native-host",
) -> Path:
    """把启动脚本写到安装目录并加上可执行权限。"""
    if windows is None:
        windows = paths.is_windows()
    # 清单里的 `path` 必须是绝对路径——浏览器是按字面路径去执行的，
    # 相对路径会按它自己的工作目录解释，多半找不到。
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{name}.cmd" if windows else name
    path = directory / filename
    path.write_text(host_launcher_script(executable, windows=windows), encoding="utf-8")
    if not windows:
        os.chmod(path, 0o755)
    return path


def install_native_host(
    executable: str | Path,
    *,
    app_id: str = APP_ID,
    host_dir: str | Path | None = None,
    extension_ids: Sequence[str] | None = None,
) -> list[Path]:
    """一步装好：写启动脚本 + 写各浏览器的宿主清单。

    GUI 的「安装浏览器扩展」按钮调这个就完事——返回写出的文件
    路径，方便在界面上告诉用户清单装到哪儿了。
    """
    directory = Path(host_dir).expanduser() if host_dir else paths.config_dir() / "native-host"
    launcher = write_host_launcher(directory, str(executable))
    install_host_manifests(app_id, launcher, extension_ids=extension_ids)
    return [launcher]


# -- 消息处理 -------------------------------------------------------


@dataclass
class _AddParams:
    """扩展发来的派单参数。"""

    url: str
    filename: str | None = None
    referer: str | None = None
    cookie: str | None = None
    user_agent: str | None = None
    method: str = "GET"
    post_data: str | None = None


def _optional_str(value: Any) -> str | None:
    """只接受非空字符串。空串当没有——扩展偶尔会传空字段。"""
    if isinstance(value, str) and value:
        return value
    return None


def handle_message(
    message: dict[str, Any],
    *,
    client: Any = None,
    token_provider: Callable[[], str] | None = None,
    allow_http_fallback: bool = True,
) -> dict[str, Any]:
    """处理一条扩展发来的消息，返回响应字典。

    `client` 是 IPC 客户端（`IPCClient` 或测试里的假货），
    `token_provider` 用来替换 token 来源。
    """
    op = message.get("op")
    if not isinstance(op, str):
        return {"ok": False, "error": "missing_op", "error_category": _CATEGORY_CLIENT}

    if op == "ping":
        return {"ok": True, "pong": True}

    if op == "pair":
        if token_provider is not None:
            token = token_provider()
        else:
            token = ensure_pair_token()
        return {"ok": True, "token": token}

    if op == "add":
        return _handle_add(
            message,
            client=client,
            token_provider=token_provider,
            allow_http_fallback=allow_http_fallback,
        )

    return {"ok": False, "error": "unknown_op", "error_category": _CATEGORY_UNKNOWN}


def _handle_add(
    message: dict[str, Any],
    *,
    client: Any,
    token_provider: Callable[[], str] | None,
    allow_http_fallback: bool,
) -> dict[str, Any]:
    """派单。校验顺序是有讲究的：先认 token，再看协议。

    先认 token 是因为「谁能派单」比「派什么」更重要——没配对的
    调用方连 URL 都不该看见结果。
    """
    if token_provider is None:
        token_ok = verify_pair_token(message.get("token"))
    else:
        expected = token_provider()
        token_ok = bool(expected) and message.get("token") == expected
    if not token_ok:
        return {"ok": False, "error": "unauthorized", "error_category": _CATEGORY_AUTH}

    url = message.get("url")
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "missing_url", "error_category": _CATEGORY_CLIENT}

    scheme = urlsplit(url.strip()).scheme.lower()
    if scheme not in ("http", "https"):
        # 扩展本地已经放过一遍，但它是不可信输入源——这里再挡一次。
        # 区别于其他错误：这个错误码告诉扩展「放行给浏览器，别取消」，
        # 取消了用户的文件就没了。
        return {"ok": False, "error": "unsupported_scheme", "error_category": _CATEGORY_CLIENT}

    params = _AddParams(
        url=url.strip(),
        filename=_optional_str(message.get("filename")),
        referer=_optional_str(message.get("referer")),
        cookie=_optional_str(message.get("cookie")),
        user_agent=_optional_str(message.get("user_agent")),
        method=_optional_str(message.get("method")) or "GET",
        post_data=_optional_str(message.get("post_data")),
    )
    return _dispatch_add(
        params,
        client=client,
        allow_http_fallback=allow_http_fallback,
    )


def _ipc_params(params: _AddParams) -> dict[str, Any]:
    """转成守护进程 `add` 方法的参数。

    `headers` 里的 Cookie / User-Agent 是契约 §4.1 要求「传下去」
    的浏览器上下文。**注意**：守护进程侧的 `Dispatcher._add` 目前
    只读 url / filename / category / threads / referer，
    `headers` 会被丢掉——`Dispatcher._add` 要把 `headers` 塞进
    `StartSpec` 才能真正生效（那部分不在本模块的改动范围内，
    字段名约定见 `protocol.PARAM_HEADERS`）。
    """
    payload: dict[str, Any] = {"url": params.url}
    if params.filename:
        payload["filename"] = params.filename
    if params.referer:
        payload["referer"] = params.referer
    headers: dict[str, str] = {}
    if params.cookie:
        headers["Cookie"] = params.cookie
    if params.user_agent:
        headers["User-Agent"] = params.user_agent
    if headers:
        payload[PARAM_HEADERS] = headers
    if params.method and params.method.upper() != "GET":
        payload[PARAM_METHOD] = params.method.upper()
    if params.post_data:
        payload[PARAM_POST_DATA] = params.post_data
    return payload


def _dispatch_add(
    params: _AddParams,
    *,
    client: Any,
    allow_http_fallback: bool,
) -> dict[str, Any]:
    """把派单送到正在运行的应用。"""
    payload = _ipc_params(params)
    try:
        response = _send_via_ipc(payload, client=client)
    except ClientError as err:
        message = str(err)
        # 超时意味着请求**可能已经被处理了**（守护进程收了，只是回包慢）。
        # 这时候再走 HTTP 通道就是重复派单——用户会看到两个一样的任务。
        if "超时" in message:
            return {
                "ok": False,
                "error": "app_timeout",
                "error_category": _CATEGORY_UNKNOWN,
                "detail": message,
            }
        if not allow_http_fallback:
            return {
                "ok": False,
                "error": "app_unavailable",
                "error_category": _CATEGORY_UNKNOWN,
                "detail": message,
            }
        try:
            response = _send_via_http(payload, token=load_pair_token())
        except ClientError as http_err:
            return {
                "ok": False,
                "error": "app_unavailable",
                "error_category": _CATEGORY_UNKNOWN,
                "detail": str(http_err),
            }
    return _normalize_add_response(response, params.url)


def _send_via_ipc(payload: dict[str, Any], *, client: Any) -> dict[str, Any]:
    """走守护进程的 Unix socket——和 CLI 完全同一条路。"""
    if client is None:
        client = IPCClient()
    request = {"id": str(uuid.uuid4()), "method": METHOD_ADD, "params": payload}
    return client.send(request)


def _send_via_http(payload: dict[str, Any], *, token: str) -> dict[str, Any]:
    """降级：走 GUI 进程里的回环 HTTP 通道。

    守护进程没跑、但 GUI 在跑的时候，扩展还是能把任务塞进 GUI 的
    引擎里——GUI 自己也开着 dispatch 服务。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{DISPATCH_PORT}/add",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "X-SDM-Token": token,
        },
    )
    try:
        with urlopen(request, timeout=8.0) as response:
            raw = response.read().decode("utf-8")
    except (URLError, OSError) as err:
        raise ClientError(f"回环通道也不可达: {err}") from err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ClientError(f"回环通道响应不是 JSON: {err}") from err
    if not isinstance(data, dict):
        raise ClientError("回环通道响应格式不对")
    return data


def _normalize_add_response(response: dict[str, Any], url: str) -> dict[str, Any]:
    """把应用侧的响应统一成契约 §4.3 的形状。"""
    if response.get("ok"):
        result = response.get("result")
        if not isinstance(result, dict):
            result = {}
        return {
            "ok": True,
            "task_id": result.get("task_id", ""),
            "url": result.get("url", url),
            "state": result.get("state"),
        }
    error = response.get("error")
    return {
        "ok": False,
        "error": error if isinstance(error, str) and error else "dispatch_failed",
        "error_category": _CATEGORY_UNKNOWN,
    }


# -- 入口 -----------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """宿主进程入口。浏览器拉起它，读一帧回一帧。

    **循环读帧**而不是「读一帧就退」：Chrome / Firefox 启动宿主后
    会把管道留着，后续消息走同一个进程。宿主读完就退的话，浏览器
    会认为宿主崩了（Firefox 直接报 "native host has exited"），
    每下一次载都要重新拉一个 Python 进程——几十毫秒的冷启动，
    乘上每个文件都很明显。对端关闭管道（EOF）时才退出。

    **stdout 只有协议帧**，日志走 stderr。
    """
    args = list(sys.argv if argv is None else argv)
    # 浏览器调用宿主时传两个参数：应用 ID 和调用方的 origin。
    # 应用 ID 不对说明清单配错了——回错误帧比默默跑着强。
    app_id = args[1] if len(args) > 1 else ""
    origin = args[2] if len(args) > 2 else ""
    if app_id and app_id != APP_ID:
        _write_frame(
            sys.stdout.buffer,
            {
                "ok": False,
                "error": "wrong_app_id",
                "error_category": _CATEGORY_CLIENT,
            },
        )
        return 2

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    reader = FrameReader(stdin)
    while True:
        try:
            raw = reader.read_frame()
        except FrameError as err:
            # 帧长已经不可信了，后面全是垃圾，不能再往下读。
            _write_frame(
                stdout,
                {"ok": False, "error": str(err), "error_category": _CATEGORY_UNKNOWN},
            )
            return 1
        if raw is None:
            return 0
        try:
            message = decode_frame(raw)
        except ValueError as err:
            _write_frame(
                stdout,
                {
                    "ok": False,
                    "error": f"bad_request: {err}",
                    "error_category": _CATEGORY_CLIENT,
                },
            )
            continue
        try:
            response = handle_message(message)
        except Exception as err:
            # 宿主绝不能带着 traceback 死掉——浏览器只看到「宿主崩了」，
            # 排查时连日志都读不到。
            log.error(CAT_IPC, f"处理原生消息失败: {err}")
            response = {
                "ok": False,
                "error": f"internal_error: {err}",
                "error_category": _CATEGORY_UNKNOWN,
            }
        _write_frame(stdout, response)
        if origin:
            log.debug(CAT_IPC, f"已处理来自 {origin} 的消息")


def _write_frame(stream, payload: dict[str, Any]) -> None:
    """写一帧并立刻 flush——浏览器在等回包，不 flush 它就一直等。"""
    try:
        stream.write(encode_frame(payload))
        stream.flush()
    except (OSError, ValueError, FrameError):
        # 管道被关了就没办法了，但**不能抛**——异常会打到 stderr，
        # 浏览器那边只看到宿主莫名退出。
        pass


if __name__ == "__main__":
    sys.exit(main())
