"""原生宿主（Native Messaging）测试。

契约 §4：帧格式、命令语义、配对 token、宿主清单。

验收要点：
- 帧是「4 字节小端长度 + UTF-8 JSON」，**一次 read 不读满是常态**
  （管道跨包），必须循环读到长度前缀说的那个数
- 坏 JSON / 超大负载 → 回错误帧，**不能带 traceback 崩掉**
- `pair` 发 token、`add` 校验 token、`ping` 通
- `add` 走的是**和 CLI 同一条路**（Unix socket → 守护进程 → 引擎），
  不是自己另起一个引擎
- 宿主清单有 name / description / path / type / allowed_origins
- 回环 HTTP 通道：必须带 token；`blob:` / `data:` 单独报错
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.ipc import dispatch_server as dispatch_mod
from sdm.ipc import native_host as nh
from sdm.ipc.client import ClientError

# -- 帧编解码 -------------------------------------------------------


def test_encode_frame_uses_4_byte_little_endian_length():
    """长度前缀是 4 字节小端，后面跟 UTF-8 JSON。"""
    frame = nh.encode_frame({"op": "ping"})
    payload = frame[4:]
    assert int.from_bytes(frame[:4], "little") == len(payload)
    assert json.loads(payload.decode("utf-8")) == {"op": "ping"}


def test_encode_frame_uses_utf8_for_non_ascii():
    """中文按 UTF-8 编码，长度前缀算的是**字节数**不是字符数。"""
    text = '{"op": "add", "filename": "下载.zip"}'
    frame = nh.encode_frame({"op": "add", "filename": "下载.zip"})
    payload = frame[4:]
    assert int.from_bytes(frame[:4], "little") == len(payload)
    # 字节数 > 字符数：说明确实是 UTF-8 多字节，长度前缀没算错
    assert len(payload) > len(text)
    assert json.loads(payload.decode("utf-8"))["filename"] == "下载.zip"


def test_encode_frame_rejects_oversized_payload():
    """超过上限直接报错，不要先编码再判断。"""
    with pytest.raises(nh.FrameError):
        nh.encode_frame({"blob": "x" * (nh.MAX_PAYLOAD_BYTES + 10)})


def test_decode_frame_round_trip():
    frame = nh.encode_frame({"op": "pair"})
    assert nh.decode_frame(frame[4:]) == {"op": "pair"}


def test_decode_frame_rejects_bad_json():
    """坏 JSON 抛 ValueError——由调用方回错误帧，不是崩掉。"""
    with pytest.raises(ValueError):
        nh.decode_frame(b"{not json")


def test_decode_frame_rejects_non_object():
    """帧必须是个 JSON 对象。"""
    with pytest.raises(ValueError):
        nh.decode_frame(b"[1,2,3]")


class _ChunkStream:
    """每次 read 只给几个字节——模拟管道的跨包到达。"""

    def __init__(self, data: bytes, chunk: int = 3) -> None:
        self._data = data
        self._chunk = chunk
        self._pos = 0

    def read(self, size: int) -> bytes:
        piece = self._data[self._pos : self._pos + self._chunk]
        self._pos += len(piece)
        return piece


def test_read_frame_handles_split_reads():
    """**一次 read 不一定读满。** 拆包到达时也要读出完整的一帧。"""
    frame = nh.encode_frame({"op": "ping"})
    reader = nh.FrameReader(_ChunkStream(frame, chunk=1))
    assert reader.read_frame() == frame[4:]


def test_frame_reader_reads_consecutive_frames():
    """同一个流里连着读两帧，中间不能串味。"""
    first = nh.encode_frame({"op": "ping"})
    second = nh.encode_frame({"op": "pair"})
    reader = nh.FrameReader(_ChunkStream(first + second, chunk=5))
    assert reader.read_frame() == first[4:]
    assert reader.read_frame() == second[4:]


def test_read_frame_returns_none_on_eof():
    """对端关掉了，返回 None（不是空帧）。"""
    assert nh.read_frame(io.BytesIO(b"")) is None


def test_read_frame_rejects_oversized_length_prefix():
    """长度前缀说超过上限 → 报错，不能照着这个数去读。"""
    header = (nh.MAX_PAYLOAD_BYTES + 1).to_bytes(4, "little")
    with pytest.raises(nh.FrameError):
        nh.read_frame(io.BytesIO(header))


def test_read_frame_rejects_truncated_frame():
    """帧被截断 → 报错。"""
    frame = nh.encode_frame({"op": "ping"})
    with pytest.raises(nh.FrameError):
        nh.read_frame(io.BytesIO(frame[:-3]))


# -- 配对 token -----------------------------------------------------


@pytest.fixture
def token_store(tmp_path, monkeypatch):
    """把 token 存档和设置文件都指到临时目录。"""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(nh.paths, "config_dir", lambda: config_dir)
    monkeypatch.setattr(nh.paths, "settings_path", lambda: config_dir / "settings.json")
    return config_dir


def test_ensure_pair_token_generates_and_persists(token_store):
    """第一次配对生成 token，第二次拿到的是同一个。"""
    first = nh.ensure_pair_token()
    assert first
    assert len(first) >= 32
    assert nh.ensure_pair_token() == first
    assert (token_store / nh.PAIR_TOKEN_FILENAME).read_text(encoding="utf-8").strip() == first


def test_pair_token_is_mirrored_into_settings(token_store):
    """设置文件里也有 `browser_pair_token`——GUI 要读它。"""
    token = nh.ensure_pair_token()
    settings = json.loads((token_store / "settings.json").read_text(encoding="utf-8"))
    assert settings[nh.PAIR_TOKEN_KEY] == token


def test_pair_token_mirror_keeps_other_settings_keys(token_store):
    """写镜像不能把设置文件里别的键弄丢——那是 GUI 的地盘。"""
    settings_path = token_store / "settings.json"
    settings_path.write_text(json.dumps({"locale": "zh-CN"}), encoding="utf-8")
    nh.ensure_pair_token()
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["locale"] == "zh-CN"
    assert settings[nh.PAIR_TOKEN_KEY]


def test_verify_pair_token(token_store):
    """token 对得上才认。"""
    token = nh.ensure_pair_token()
    assert nh.verify_pair_token(token) is True
    assert nh.verify_pair_token(token + "x") is False
    assert nh.verify_pair_token("") is False
    assert nh.verify_pair_token(None) is False


def test_verify_pair_token_rejects_when_no_token_configured(token_store):
    """应用还没配过对时，任何 token 都不认。"""
    assert nh.verify_pair_token("whatever") is False


# -- 命令处理 -------------------------------------------------------


class _FakeClient:
    """假 IPC 客户端：记录请求，返回预设的响应。"""

    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response or {
            "id": "x",
            "ok": True,
            "result": {"task_id": "task-1", "url": "https://example.com/a.zip", "state": "queued"},
        }
        self.error = error
        self.requests: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.requests.append(payload)
        if self.error is not None:
            raise self.error
        return self.response


def test_ping_returns_ok():
    """ping 是最简单的一条。"""
    response = nh.handle_message({"op": "ping"}, client=_FakeClient())
    assert response["ok"] is True
    assert response["pong"] is True


def test_pair_returns_token(token_store):
    """pair 发 token。"""
    response = nh.handle_message({"op": "pair"}, client=_FakeClient())
    assert response["ok"] is True
    assert response["token"] == nh.load_pair_token()


def test_unknown_op_is_rejected():
    """不认识的 op 要明确拒绝，不能默默成功。"""
    response = nh.handle_message({"op": "explode"}, client=_FakeClient())
    assert response["ok"] is False
    assert response["error"] == "unknown_op"


def test_missing_op_is_rejected():
    response = nh.handle_message({}, client=_FakeClient())
    assert response["ok"] is False
    assert response["error"] == "missing_op"


def test_add_without_token_is_unauthorized(token_store):
    """没带 token 一律拒绝。"""
    nh.ensure_pair_token()
    response = nh.handle_message(
        {"op": "add", "url": "https://example.com/a.zip"},
        client=_FakeClient(),
    )
    assert response["ok"] is False
    assert response["error"] == "unauthorized"


def test_add_with_wrong_token_is_unauthorized(token_store):
    token = nh.ensure_pair_token()
    response = nh.handle_message(
        {"op": "add", "token": token + "x", "url": "https://example.com/a.zip"},
        client=_FakeClient(),
    )
    assert response["ok"] is False
    assert response["error"] == "unauthorized"


@pytest.mark.parametrize("url", ["blob:https://example.com/abc", "data:text/plain;base64,aGk="])
def test_add_rejects_non_http_scheme(token_store, url):
    """blob: / data: 要单独报错——扩展靠这个码决定放行给浏览器。"""
    token = nh.ensure_pair_token()
    client = _FakeClient()
    response = nh.handle_message(
        {"op": "add", "token": token, "url": url},
        client=client,
    )
    assert response["ok"] is False
    assert response["error"] == "unsupported_scheme"
    assert client.requests == []


def test_add_goes_through_the_ipc_dispatcher(token_store):
    """派单走的是和 CLI 同一条路：method=add 的 IPC 请求。

    **不能自己另起一个引擎**——那会让 GUI 里的任务列表和实际下载
    的任务对不上。
    """
    token = nh.ensure_pair_token()
    client = _FakeClient()
    response = nh.handle_message(
        {
            "op": "add",
            "token": token,
            "url": "https://example.com/a.zip",
            "filename": "a.zip",
            "referer": "https://example.com/page",
            "cookie": "sid=1",
            "user_agent": "UA/1.0",
        },
        client=client,
    )
    assert response["ok"] is True
    assert response["task_id"] == "task-1"

    assert len(client.requests) == 1
    request = client.requests[0]
    assert request["method"] == "add"
    params = request["params"]
    assert params["url"] == "https://example.com/a.zip"
    assert params["filename"] == "a.zip"
    assert params["referer"] == "https://example.com/page"
    # Cookie / UA 要传下去——少了它们很多站点直接给 403。
    assert params["headers"]["Cookie"] == "sid=1"
    assert params["headers"]["User-Agent"] == "UA/1.0"


def test_add_without_url_is_rejected(token_store):
    token = nh.ensure_pair_token()
    response = nh.handle_message({"op": "add", "token": token}, client=_FakeClient())
    assert response["ok"] is False
    assert response["error"] == "missing_url"


def test_add_reports_app_unavailable_when_app_is_down(token_store):
    """应用没开时要说清楚是「应用不可用」，不是静默失败。"""
    token = nh.ensure_pair_token()
    client = _FakeClient(error=ClientError("守护进程不可达（socket 不存在）"))
    response = nh.handle_message(
        {"op": "add", "token": token, "url": "https://example.com/a.zip"},
        client=client,
        allow_http_fallback=False,
    )
    assert response["ok"] is False
    assert response["error"] == "app_unavailable"


def test_add_does_not_retry_after_timeout(token_store):
    """超时的请求**可能已经被处理了**——再走 HTTP 就是重复派单。"""
    token = nh.ensure_pair_token()
    client = _FakeClient(error=ClientError("守护进程超时未响应"))
    response = nh.handle_message(
        {"op": "add", "token": token, "url": "https://example.com/a.zip"},
        client=client,
        allow_http_fallback=True,
    )
    assert response["ok"] is False
    assert response["error"] == "app_timeout"


def test_add_surfaces_engine_error(token_store):
    """引擎侧的失败要原样带回来。"""
    token = nh.ensure_pair_token()
    client = _FakeClient(response={"id": "x", "ok": False, "error": "add: 'url' must be http(s)"})
    response = nh.handle_message(
        {"op": "add", "token": token, "url": "https://example.com/a.zip"},
        client=client,
    )
    assert response["ok"] is False
    assert "http(s)" in response["error"]


# -- 宿主清单 -------------------------------------------------------


def test_host_manifest_shape():
    """清单必须有 name / description / path / type / allowed_origins。"""
    manifest = nh.host_manifest("com.skyc8266.superdownloadmanager", "/tmp/host.sh")
    for key in ("name", "description", "path", "type", "allowed_origins"):
        assert key in manifest
    assert manifest["name"] == "com.skyc8266.superdownloadmanager"
    assert manifest["type"] == "stdio"
    assert manifest["path"] == "/tmp/host.sh"
    assert isinstance(manifest["allowed_origins"], list)
    assert manifest["allowed_origins"]


def test_host_manifest_firefox_lists_allowed_extensions():
    """Firefox 的清单用 `allowed_extensions`——它的 origin 是随机的。"""
    manifest = nh.host_manifest(nh.APP_ID, "/tmp/host.sh", browser="firefox")
    assert manifest["allowed_extensions"] == [nh.FIREFOX_EXTENSION_ID]


def test_host_manifest_expands_extension_ids():
    """传裸扩展 ID 时按浏览器补 origin 前缀。"""
    manifest = nh.host_manifest(nh.APP_ID, "/tmp/host.sh", browser="chrome", extension_ids=["abcdef"])
    assert manifest["allowed_origins"] == ["chrome-extension://abcdef/"]


def test_host_manifest_defaults_to_our_real_extension_id():
    """**默认就得写真 ID，不能留占位符。**

    宿主清单里的 `allowed_origins` 跟扩展的 origin 对不上时，
    Chrome 直接拒绝拉起宿主——症状只是「扩展说应用没响应」，
    根本看不出是清单写错了。
    """
    manifest = nh.host_manifest(nh.APP_ID, "/tmp/host.sh")
    assert manifest["allowed_origins"] == [
        f"chrome-extension://{nh.CHROMIUM_EXTENSION_ID}/"
    ]
    assert not any("REPLACE_WITH" in origin for origin in manifest["allowed_origins"])


def test_firefox_manifest_has_no_allowed_origins():
    """Firefox 的清单只留 `allowed_extensions`。

    它的运行时 origin 是随机的 `moz-extension://<uuid>/`，写
    `allowed_origins` 只会多一份永远对不上的垃圾。
    """
    manifest = nh.host_manifest(nh.APP_ID, "/tmp/host.sh", browser="firefox")
    assert "allowed_origins" not in manifest
    assert manifest["allowed_extensions"] == [nh.FIREFOX_EXTENSION_ID]


def test_install_host_manifests_writes_and_is_idempotent(tmp_path, monkeypatch):
    """写进各浏览器的 NativeMessagingHosts 目录，重复调用结果一样。"""
    chrome_dir = tmp_path / "chrome"
    edge_dir = tmp_path / "edge"
    monkeypatch.setattr(
        nh,
        "host_manifest_dirs",
        lambda: [("chrome", chrome_dir), ("edge", edge_dir)],
    )
    first = nh.install_host_manifests(nh.APP_ID, "/tmp/host.sh")
    assert len(first) == 2
    for path in first:
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["name"] == nh.APP_ID
        assert data["path"] == "/tmp/host.sh"
        assert data["type"] == "stdio"

    before = {path: path.read_text(encoding="utf-8") for path in first}
    nh.install_host_manifests(nh.APP_ID, "/tmp/host.sh")
    after = {path: path.read_text(encoding="utf-8") for path in first}
    assert before == after


def test_install_host_manifests_creates_parent_directories(tmp_path, monkeypatch):
    """父目录要自动建——装完 Chrome 常常还没有这个目录。"""
    target = tmp_path / "deep" / "nested" / "NativeMessagingHosts"
    monkeypatch.setattr(nh, "host_manifest_dirs", lambda: [("chrome", target)])
    written = nh.install_host_manifests(nh.APP_ID, "/tmp/host.sh")
    assert target.is_dir()
    assert written[0].exists()


def test_host_launcher_script_shell():
    """macOS 的启动脚本是 sh，转调打包好的宿主。"""
    script = nh.host_launcher_script("/Applications/SDM/sdmd-native-host", windows=False)
    assert script.startswith("#!/bin/sh")
    assert "exec " in script
    assert "/Applications/SDM/sdmd-native-host" in script


def test_host_launcher_script_windows():
    """Windows 的启动脚本是 .cmd。"""
    script = nh.host_launcher_script(r"C:\SDM\sdmd-native-host.exe", windows=True)
    assert "@echo off" in script
    assert "sdmd-native-host.exe" in script


# -- 回环 HTTP 通道 -------------------------------------------------


def _post(port: int, path: str, body: dict, headers: dict | None = None) -> tuple[int, dict]:
    """往回环服务发一个 POST，返回 (状态码, 解析后的 JSON)。"""
    import socket

    payload = json.dumps(body).encode("utf-8")
    head = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
    )
    for key, value in (headers or {}).items():
        head += f"{key}: {value}\r\n"
    head += "\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head.encode("utf-8") + payload)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8")
    status = int(raw.split(" ", 2)[1])
    body_text = raw.split("\r\n\r\n", 1)[1]
    return status, json.loads(body_text)


def _get(port: int, path: str) -> tuple[int, dict]:
    import socket

    head = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(head.encode("utf-8"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks).decode("utf-8")
    status = int(raw.split(" ", 2)[1])
    body_text = raw.split("\r\n\r\n", 1)[1]
    return status, json.loads(body_text)


@pytest.fixture
def dispatch_server():
    """起一个回环 dispatch 服务，token 固定。"""
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    received: list[dispatch_mod.AddRequest] = []

    def on_add(request: dispatch_mod.AddRequest) -> dict:
        received.append(request)
        return {"task_id": "task-1"}

    server = dispatch_mod.DispatchServer(
        port=port,
        on_add_full=on_add,
        token_provider=lambda: "test-token",
    )
    server.start()
    try:
        yield port, received
    finally:
        server.stop()


def test_ping_stays_open(dispatch_server):
    """/ping 不校验 token——扩展用它探活。"""
    port, _ = dispatch_server
    status, body = _get(port, "/ping")
    assert status == 200
    assert body["ok"] is True


def test_add_without_token_is_rejected(dispatch_server):
    """**没有 token 一律拒绝**——这个端口任何网页都能 fetch。"""
    port, received = dispatch_server
    status, body = _post(port, "/add", {"url": "https://example.com/a.zip"})
    assert status == 401
    assert body["error"] == "unauthorized"
    assert received == []


def test_add_with_wrong_token_is_rejected(dispatch_server):
    port, received = dispatch_server
    status, body = _post(
        port,
        "/add",
        {"url": "https://example.com/a.zip"},
        {"X-SDM-Token": "nope"},
    )
    assert status == 401
    assert body["error"] == "unauthorized"
    assert received == []


def test_add_with_token_accepts_post_body(dispatch_server):
    """POST 体里的字段要能被读到——扩展把 cookie 放在体里。"""
    port, received = dispatch_server
    status, body = _post(
        port,
        "/add",
        {
            "url": "https://example.com/a.zip",
            "filename": "a.zip",
            "referer": "https://example.com/page",
            "cookie": "sid=1",
            "user_agent": "UA/1.0",
        },
        {"X-SDM-Token": "test-token"},
    )
    assert status == 200
    assert body["ok"] is True
    assert body["result"]["task_id"] == "task-1"
    assert len(received) == 1
    request = received[0]
    assert request.url == "https://example.com/a.zip"
    assert request.filename == "a.zip"
    assert request.referer == "https://example.com/page"
    assert request.cookie == "sid=1"
    assert request.user_agent == "UA/1.0"


def test_add_accepts_token_in_query(dispatch_server):
    """token 也可以放 query——扩展在不同浏览器里发的位置不一样。"""
    port, received = dispatch_server
    status, _ = _get(port, "/add?token=test-token&url=https%3A%2F%2Fexample.com%2Fa.zip")
    assert status == 200
    assert len(received) == 1


@pytest.mark.parametrize("url", ["blob:https://example.com/abc", "data:text/plain;base64,aGk="])
def test_add_rejects_unsupported_scheme_with_distinct_error(dispatch_server, url):
    """`unsupported_scheme` 是**单独的**错误码。

    扩展靠它决定「放行给浏览器」。跟别的错误混在一起，扩展就
    只能当失败处理——然后它会把用户的下载取消掉，文件就没了。
    """
    port, received = dispatch_server
    status, body = _post(
        port,
        "/add",
        {"url": url},
        {"X-SDM-Token": "test-token"},
    )
    assert status == 400
    assert body["error"] == "unsupported_scheme"
    assert received == []


def test_add_keeps_cors_header(dispatch_server):
    """CORS 头保持原样——扩展在别的源上调这个接口。"""
    import socket

    port, _ = dispatch_server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n\r\n")
        raw = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw += chunk
    assert b"Access-Control-Allow-Origin: *" in raw


def test_legacy_on_add_callback_still_works():
    """老的 `on_add(url, filename, referer)` 接线不能被弄坏。

    GUI 现在就是三参数的 lambda。
    """
    seen: list[tuple] = []
    server = dispatch_mod.DispatchServer(
        port=0,
        on_add=lambda url, filename, referer: seen.append((url, filename, referer)),
        token_provider=lambda: "test-token",
    )
    request = dispatch_mod.AddRequest(url="https://example.com/a.zip", filename="a.zip")
    server._dispatch(request)
    assert seen == [("https://example.com/a.zip", "a.zip", None)]
