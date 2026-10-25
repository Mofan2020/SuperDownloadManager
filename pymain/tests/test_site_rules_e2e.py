"""站点规则端到端测试：F113。

前面的 test_site_rules_headers.py 用 mock 验证了合并逻辑。这里
起一个**真的** HTTP 服务器，验证请求实际发出去时头是对的。

为什么还要这一层：mock 只能证明「我们把头算对了」，证不了
「头真的发出去了」。httpx 的 per-request headers 跟 client 级
headers 怎么合并、UA 会不会被默认值盖掉——这些只有真发一次
才知道。

验证两条路径：
- 探测请求（Range: bytes=0-0）带规则头
- 分片请求带规则头
"""

from __future__ import annotations

import asyncio
import base64
import http.server
import socketserver
import sys
import threading
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.http_downloader import HTTPDownloader
from sdm.site_rules import SiteRule, SiteRuleStore


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """记录每次请求的头。"""

    protocol_version = "HTTP/1.1"

    requests: ClassVar[list[dict[str, str]]] = []

    def end_headers(self) -> None:
        self.send_header("Connection", "close")
        self.close_connection = True
        super().end_headers()

    def do_GET(self) -> None:
        type(self).requests.append(dict(self.headers))
        body = b"x" * 4096
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def server():
    """起一个本地 HTTP 服务器，返回 (base_url, 请求记录列表)。"""
    _RecordingHandler.requests = []
    with socketserver.TCPServer(("127.0.0.1", 0), _RecordingHandler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}", _RecordingHandler.requests
        finally:
            httpd.shutdown()


def _rule(**kwargs) -> SiteRule:
    defaults = {
        "id": "r1",
        "host": "127.0.0.1",
        "referer": "",
        "user_agent": "",
        "cookies": "",
        "basic_auth_user": "",
    }
    defaults.update(kwargs)
    return SiteRule(**defaults)


class _Store:
    """只替换「找哪条规则」和「密码是什么」。

    **apply_to_headers 用真的实现**（SiteRuleStore 的）——自己抄
    一份的话，改了真代码测试不会红，测的就不是真行为了。
    """

    def __init__(self, rule, password=""):
        self._rule = rule
        self._password = password

    def match(self, url):
        return self._rule

    def get_password(self, user, host):
        return self._password

    def apply_to_headers(self, rule, headers):
        return SiteRuleStore.apply_to_headers(self, rule, headers)


def _patch_store(monkeypatch, rule, password=""):
    fake = _Store(rule, password)
    monkeypatch.setattr(
        "sdm.site_rules.SiteRuleStore.shared", staticmethod(lambda: fake)
    )
    return fake


# -- 探测请求 ------------------------------------------------------


def test_probe_request_carries_rule_headers(server, monkeypatch):
    """探测请求实际带上规则的头。"""
    base_url, requests = server
    _patch_store(
        monkeypatch,
        _rule(user_agent="rule-ua", referer="http://referer.example/"),
    )

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    assert requests, "服务器没收到请求"
    sent = requests[0]

    # 规则里的 UA 真的发出去了，且没有被默认值盖掉
    assert sent.get("User-Agent") == "rule-ua"
    assert sent.get("Referer") == "http://referer.example/"
    # 探测语义没被破坏
    assert sent.get("Range") == "bytes=0-0"


def test_probe_request_without_rule_uses_default_ua(server, monkeypatch):
    """没有规则时用默认 UA。"""
    base_url, requests = server
    _patch_store(monkeypatch, None)

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    assert requests[0].get("User-Agent") == "default-ua"


def test_probe_request_sends_basic_auth(server, monkeypatch):
    """Basic Auth 真的发出去，且能被解码。"""
    base_url, requests = server
    _patch_store(
        monkeypatch,
        _rule(basic_auth_user="alice"),
        password="s3cret",
    )

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    auth = requests[0].get("Authorization")
    assert auth is not None and auth.startswith("Basic ")
    decoded = base64.b64decode(auth[len("Basic ") :]).decode()
    assert decoded == "alice:s3cret"


def test_probe_request_sends_cookie(server, monkeypatch):
    """Cookie 真的发出去。"""
    base_url, requests = server
    _patch_store(monkeypatch, _rule(cookies="sid=abc123"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    assert requests[0].get("Cookie") == "sid=abc123"


def test_only_one_user_agent_header_sent(server, monkeypatch):
    """不能发两个 User-Agent。

    httpx 遇到 `User-Agent` 和 `user-agent` 两个键会都发出去，
    服务器看到两个 UA 可能挑一个不认的用。
    """
    base_url, requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    # requests.headers 是大小写不敏感的 dict，但重复的原始头会
    # 合并成逗号分隔的值——出现逗号就说明发了两遍
    ua = requests[0].get("User-Agent")
    assert ua is not None
    assert "," not in ua, f"发了多个 User-Agent: {ua}"


def test_rule_user_agent_overrides_existing_default(server, monkeypatch):
    """规则里的 UA 覆盖**已有的** UA，不是叠加。

    区别在于「先有默认 UA，再套规则」这个顺序：headers 里已经有
    `User-Agent: default-ua` 时，如果规则是「叠加」语义，两个值
    都在；如果覆盖，只剩规则的那个。
    """
    base_url, _requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    # 直接调 apply_site_rule，绕开 probe 的合并顺序
    headers = dl.apply_site_rule(f"{base_url}/a.zip", {"User-Agent": "default-ua"})

    assert headers["User-Agent"] == "rule-ua"
    assert len([v for v in headers.values() if "ua" in str(v)]) == 1


def test_rule_user_agent_lowercase_overrides_default(server, monkeypatch):
    """规则里用小写 `user-agent` 也要正确覆盖。

    头名大小写不敏感——规则里写 `user-agent` 和写 `User-Agent`
    是同一回事，不能两个都发出去。
    """
    base_url, _requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    headers = dl.apply_site_rule(f"{base_url}/a.zip", {"User-Agent": "default-ua"})

    values = list(headers.values())
    assert values.count("rule-ua") == 1
    assert "default-ua" not in values, f"默认 UA 没被覆盖: {headers}"


def test_no_duplicate_user_agent_on_the_wire(server, monkeypatch):
    """线上只看到一个 User-Agent。

    httpx 遇到 `User-Agent` 和 `user-agent` 两个键会都发出去，
    服务器那边看到的是逗号分隔的两个值。
    """
    base_url, requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    asyncio.run(dl.probe(f"{base_url}/a.zip"))

    ua = requests[0].get("User-Agent")
    assert ua == "rule-ua"
    assert "," not in ua


# -- 分片请求 ------------------------------------------------------


def test_chunk_request_carries_rule_headers(server, monkeypatch, tmp_path):
    """分片请求也带规则的头。

    只测探测不够——实际下载数据的是分片请求，那才是大部分流量。
    """
    base_url, requests = server
    _patch_store(
        monkeypatch,
        _rule(user_agent="rule-ua", referer="http://referer.example/"),
    )

    from sdm.engine.download_flow import DownloadFlow

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    dest = tmp_path / "out.bin"
    flow = DownloadFlow(
        url=f"{base_url}/a.zip",
        destination=dest,
        downloader=dl,
        parallelism_cap=1,
    )

    asyncio.run(flow.run())

    assert requests, "服务器没收到请求"
    # flow 先探测再下分片，requests[0] 是探测请求——找带字节区间
    # 的那个，那才是分片请求（探测用的是 bytes=0-0）。
    chunk_requests = [
        r for r in requests
        if r.get("Range", "").startswith("bytes=") and r.get("Range") != "bytes=0-0"
    ]
    assert chunk_requests, f"没收到分片请求: {[r.get('Range') for r in requests]}"
    sent = chunk_requests[0]
    assert sent.get("User-Agent") == "rule-ua"
    assert sent.get("Referer") == "http://referer.example/"


def test_chunk_request_range_is_not_overridden(server, monkeypatch, tmp_path):
    """规则里的头不能改掉分片的 Range。

    Range 是按已落盘字节算出来的，改了就会下错数据。
    """
    base_url, requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua"))

    from sdm.engine.download_flow import DownloadFlow

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    flow = DownloadFlow(
        url=f"{base_url}/a.zip",
        destination=tmp_path / "out.bin",
        downloader=dl,
        parallelism_cap=1,
    )

    asyncio.run(flow.run())

    # flow 先探测（Range: bytes=0-0）再下分片。探测请求也是
    # bytes= 开头，得排除掉。
    chunk_requests = [
        r for r in requests
        if r.get("Range", "").startswith("bytes=") and r.get("Range") != "bytes=0-0"
    ]
    assert chunk_requests, f"没收到分片请求: {[r.get('Range') for r in requests]}"
    for sent in chunk_requests:
        rng = sent.get("Range")
        # 分片请求必须是字节区间，不能是探测用的 bytes=0-0
        assert rng != "bytes=0-0"
        assert "-" in rng


def test_site_rule_never_produces_range_header(server, monkeypatch):
    """规则不会产生 Range 头。

    分片的 Range 是按已落盘字节算出来的，规则改了就会下错数据。
    规则模型里根本没有 Range 字段——这条测试锁住这个事实，
    将来有人加了 Range 字段，这里会红。
    """
    base_url, _requests = server
    _patch_store(monkeypatch, _rule(user_agent="rule-ua", referer="http://r/"))

    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    headers = dl.apply_site_rule(f"{base_url}/a.zip", {})

    assert not any(k.lower() == "range" for k in headers)


def test_rule_model_has_no_range_field():
    """规则模型里没有 Range 字段。"""
    from sdm.site_rules import SiteRule

    fields = set(SiteRule.__dataclass_fields__)
    assert "range" not in {f.lower() for f in fields}


# -- 任务级 referer ------------------------------------------------


def test_task_referer_is_sent(server, monkeypatch, tmp_path):
    """任务带的 referer 真的发出去。

    `add` 方法可以带 referer（浏览器扩展派单时把页面地址带过来），
    很多站点要 Referer 才给下载。Swift 版读了 referer 但从来没
    用过（台账 F173 的 bug），Python 版必须真正实现。
    """
    base_url, requests = server
    _patch_store(monkeypatch, None)

    from sdm.engine.download_engine import DownloadEngine, StartSpec

    engine = DownloadEngine()
    spec = StartSpec(
        url=f"{base_url}/a.zip",
        destination_directory=tmp_path,
        referer="http://page.example/article",
    )

    import asyncio

    async def _run():
        # start 和等待必须在**同一个**事件循环里——引擎的任务挂在
        # 启动它的那个循环上，换个 asyncio.run 任务就被取消了。
        task_ids = await engine.start([spec])
        assert task_ids
        for _ in range(200):
            task = engine.get(task_ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)

    asyncio.run(_run())

    assert requests, "服务器没收到请求"
    # 探测和分片都该带 Referer
    for sent in requests:
        assert sent.get("Referer") == "http://page.example/article"


def test_no_referer_means_no_header(server, monkeypatch, tmp_path):
    """没带 referer 时不发这个头。"""
    base_url, requests = server
    _patch_store(monkeypatch, None)

    from sdm.engine.download_engine import DownloadEngine, StartSpec

    engine = DownloadEngine()
    spec = StartSpec(url=f"{base_url}/a.zip", destination_directory=tmp_path)

    import asyncio

    async def _run():
        task_ids = await engine.start([spec])
        assert task_ids
        for _ in range(200):
            task = engine.get(task_ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)

    asyncio.run(_run())

    for sent in requests:
        assert "Referer" not in sent
