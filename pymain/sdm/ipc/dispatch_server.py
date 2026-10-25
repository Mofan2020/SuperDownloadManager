"""浏览器扩展 dispatch 服务（降级通道）。

对应 Swift 版 Engine/DispatchServer.swift。

契约 §4.1：**主通道是 Native Messaging**（`sdm/ipc/native_host.py`），
这里的回环 HTTP 只在原生宿主没装（Firefox 没装宿主、Safari 没有
Chrome 式宿主）时兜底。

**这个端口必须带 Pair Token。** 它绑在回环地址上，但回环不等于
安全：这个服务带着 `Access-Control-Allow-Origin: *`，**任何网页**
都能发 `fetch("http://127.0.0.1:7955/add?url=...")` 让用户的浏览器
往下载器里塞任务（合约 §3.1 的原始需求就是拦下载，不是被人下指令）。
token 就是挡这个的。

**`blob:` / `data:` 必须放行。** 这两种协议的 URL 浏览器自己能下，
而且没有可重放的 HTTP 请求——下载器拿它们没法干活。扩展拿到服务端的
`unsupported_scheme` 之后要**放行给浏览器，不能取消浏览器的下载**，
否则用户的文件直接没了。
"""

from __future__ import annotations

import hmac
import json
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from ..logging_setup import CAT_NET, get_logger

log = get_logger()

DISPATCH_PORT = 7955

#: 请求体上限。1 MB。
#:
#: 派单消息正常只有几百字节（URL + referer + cookie）。但这是
#: 一个对外开着的端口，没有上限的话对方可以让我们一直往内存里读。
MAX_BODY_BYTES = 1024 * 1024

#: 请求头 + 请求行的上限。64 KB。
MAX_HEADER_BYTES = 64 * 1024

#: 允许派单的协议。其它一律 `unsupported_scheme`。
ALLOWED_SCHEMES = ("http", "https")


@dataclass
class AddRequest:
    """一次派单。

    `cookie` / `user_agent` / `method` / `post_data` 是浏览器上下文：
    很多站点要 Referer + Cookie 才肯给文件，少了它们下载器拿到的
    就是一个 403 页面。
    """

    url: str
    filename: str | None = None
    referer: str | None = None
    cookie: str | None = None
    user_agent: str | None = None
    method: str = "GET"
    post_data: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "filename": self.filename,
            "referer": self.referer,
            "cookie": self.cookie,
            "user_agent": self.user_agent,
            "method": self.method,
            "post_data": self.post_data,
            "headers": dict(self.headers),
        }


class DispatchServer:
    """给浏览器扩展用的回环 HTTP 服务。"""

    def __init__(
        self,
        port: int = DISPATCH_PORT,
        on_add: Callable[[str, str | None, str | None], None] | None = None,
        *,
        on_add_full: Callable[[AddRequest], object] | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.port = port
        self.on_add = on_add
        #: 带完整浏览器上下文的回调。**有它就用它**，没有才退回
        #: 老的 `on_add(url, filename, referer)`——GUI 现在的接线
        #: 还是老签名，不能因为这里改了就把 GUI 弄崩。
        self.on_add_full = on_add_full
        #: 配对 token 的来源。默认读设置里的 `browser_pair_token`。
        self.token_provider = token_provider or _default_token_provider
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._server is not None:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("127.0.0.1", self.port))
        except OSError as err:
            server.close()
            raise OSError(f"扩展 dispatch 端口 {self.port} 绑定失败: {err}") from err
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="sdm-dispatch", daemon=True)
        self._thread.start()
        log.info(CAT_NET, f"扩展 dispatch 服务已启动: 127.0.0.1:{self.port}")

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    # -- HTTP --------------------------------------------------------

    def _handle(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            head, body = self._read_request(client)
            if head is None:
                return
            response = self._respond(head, body)
            client.sendall(response)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    @staticmethod
    def _read_request(client: socket.socket) -> tuple[str, str] | None:
        """读请求头和请求体。

        **请求体要按 Content-Length 读。** 原来的实现读到
        `\\r\\n\\r\\n` 就停，POST 的 body 全丢——扩展把 cookie 和
        POST 体放在 body 里就全没了。

        返回 `(head, body)`；对端没发完整请求头就返回 None。
        """
        buffer = b""
        while b"\r\n\r\n" not in buffer and len(buffer) < MAX_HEADER_BYTES:
            chunk = client.recv(4096)
            if not chunk:
                break
            buffer += chunk
        if b"\r\n\r\n" not in buffer:
            return None
        head_bytes, _, rest = buffer.partition(b"\r\n\r\n")
        head = head_bytes.decode("utf-8", errors="replace")

        length = _content_length(head)
        if length > MAX_BODY_BYTES:
            # 太大就不读了——留着后面的连接也没用，回一个错误得了。
            return head, ""
        body_bytes = rest
        while len(body_bytes) < length:
            chunk = client.recv(4096)
            if not chunk:
                break
            body_bytes += chunk
        return head, body_bytes[:length].decode("utf-8", errors="replace")

    def _respond(self, head: str, body: str = "") -> bytes:
        first_line = head.split("\n", 1)[0].strip()
        parts = first_line.split(" ")
        if len(parts) < 2:
            return self._http(400, '{"ok":false,"error":"bad request"}')

        method = parts[0].upper()
        target = parts[1]
        parsed = urlparse(target)
        path = parsed.path
        query = parse_qs(parsed.query)
        headers = _parse_headers(head)

        # 浏览器发的跨域预检。扩展要带自定义头（token）就必须先过
        # 这一关——原来的实现直接 404，预检失败，扩展根本发不出去。
        if method == "OPTIONS":
            return self._http(204, "", extra_headers=(
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Content-Type, X-SDM-Token, Authorization\r\n"
                "Access-Control-Max-Age: 600\r\n"
            ))

        if path in ("/ping", "/ping/"):
            return self._http(200, '{"ok":true,"result":{"pong":true}}')

        if path in ("/add", "/add/"):
            return self._add(query, headers, body)

        return self._http(404, '{"ok":false,"error":"not found"}')

    def _add(self, query: dict, headers: dict[str, str], body: str) -> bytes:
        """处理派单。"""
        fields = _collect_fields(query, headers, body)

        token = _extract_token(query, headers)
        expected = self.token_provider() or ""
        if not expected:
            # 应用还没配过对——扩展也就没有 token。这一步要挡住的
            # 是「谁都能往下载器塞任务」，不能因为「还没配对」就
            # 敞开大门。
            return self._http(401, '{"ok":false,"error":"not_paired"}')
        # 常数时间比较——token 是长期凭据，普通的 `==` 会把
        # 「前几位对上了」这件事通过响应时间差泄出去。
        if not token or not hmac.compare_digest(expected, token):
            return self._http(401, '{"ok":false,"error":"unauthorized"}')

        url = fields.get("url") or ""
        if not url:
            return self._http(400, '{"ok":false,"error":"missing url"}')

        scheme = urlparse(url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            # 扩展靠这个错误码决定「放行给浏览器」。写别的词扩展
            # 就看不懂，只能当成失败——然后它可能把用户的下载取消了。
            return self._http(400, '{"ok":false,"error":"unsupported_scheme"}')

        request = AddRequest(
            url=url,
            filename=fields.get("filename") or None,
            referer=fields.get("referer") or None,
            cookie=fields.get("cookie") or None,
            user_agent=fields.get("user_agent") or None,
            method=(fields.get("method") or "GET").upper(),
            post_data=fields.get("post_data") or None,
        )
        if request.cookie:
            request.headers["Cookie"] = request.cookie
        if request.user_agent:
            request.headers["User-Agent"] = request.user_agent

        try:
            result = self._dispatch(request)
        except Exception as err:
            log.error(CAT_NET, f"扩展派单失败: {err}")
            return self._http(500, '{"ok":false,"error":"dispatch failed"}')

        payload = {"ok": True, "result": {"accepted": True, "url": url}}
        if isinstance(result, dict) and result.get("task_id"):
            payload["result"]["task_id"] = result["task_id"]
        return self._http(200, json.dumps(payload, ensure_ascii=False))

    def _dispatch(self, request: AddRequest) -> object:
        """把派单交给应用。

        有 `on_add_full` 就用它（能拿到 cookie / UA）；
        没有就退回老的 `on_add(url, filename, referer)`——GUI 现在的
        接线是三参数的，不能因为这里改了签名就把 GUI 弄崩。
        """
        if self.on_add_full is not None:
            return self.on_add_full(request)
        if self.on_add is not None:
            return self.on_add(request.url, request.filename, request.referer)
        return None

    @staticmethod
    def _http(code: int, body: str, extra_headers: str = "") -> bytes:
        reason = {
            200: "OK",
            204: "No Content",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            500: "Internal Server Error",
        }.get(code, "OK")
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            f"{extra_headers}"
            "\r\n"
        ).encode("ascii")
        return head + payload


# -- 解析helpers ---------------------------------------------------


def _default_token_provider() -> str:
    """默认的 token 来源：设置里的配对 token。

    这里**故意延迟 import** `native_host`：那个模块顶层会拉起日志
    单例，回环服务只想知道 token，不该顺带把整套原生宿主拖进来；
    将来 `native_host` 反过来要用回环服务也不会成环。
    """
    from .native_host import load_pair_token

    return load_pair_token()


def _parse_headers(head: str) -> dict[str, str]:
    """解析请求头。键统一小写。"""
    headers: dict[str, str] = {}
    for line in head.split("\n")[1:]:
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def _content_length(head: str) -> int:
    """从请求头里读 Content-Length。读不出来当 0。"""
    for line in head.split("\n")[1:]:
        key, _, value = line.partition(":")
        if key.strip().lower() == "content-length":
            try:
                return int(value.strip())
            except ValueError:
                return 0
    return 0


def _collect_fields(query: dict, headers: dict[str, str], body: str) -> dict[str, str]:
    """把 query、请求头、JSON 请求体里的字段合成一份。

    **请求体优先**——扩展用 POST 发完整消息时，query 里往往只有
    token。
    """
    fields: dict[str, str] = {}
    for key, values in query.items():
        if values:
            fields[key] = values[0]
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value:
                    fields[key] = value
                elif isinstance(value, int | float) and not isinstance(value, bool):
                    fields[key] = str(value)
    # 头里的 UA / Referer 作为兜底——扩展没单独发 user_agent 时用得上。
    user_agent = headers.get("user-agent")
    if user_agent and not fields.get("user_agent"):
        fields["user_agent"] = user_agent
    referer = headers.get("referer")
    if referer and not fields.get("referer"):
        fields["referer"] = referer
    return fields


def _extract_token(query: dict, headers: dict[str, str]) -> str:
    """取配对 token。

    三个地方都收：query 参数（`?token=`）、自定义头（`X-SDM-Token`）、
    `Authorization: Bearer <token>`。扩展在不同浏览器里能用不同的
    方式发（Firefox 的 fetch 加自定义头会先过 CORS 预检，
    Chromium 的 background fetch 不一定），都收着省事。
    """
    value = (query.get("token") or [""])[0]
    if value:
        return value
    value = headers.get("x-sdm-token") or ""
    if value:
        return value
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""
