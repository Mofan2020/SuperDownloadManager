"""站点规则请求头测试：F113。

Swift 参照：`Engine/HTTPDownloader.swift:172-191` 的
`apply(siteRule:to:for:)`。

验收要点：
- 规则里的 User-Agent **覆盖**默认 UA（不是叠加在后面）
- Basic Auth 是 `Authorization: Basic <base64(user:pass)>`，解码后内容正确
- Referer / Cookie / UA 三种头都能注入
- **探测请求**也要带规则头（探测阶段就 403 的站点，任务根本起不来）
- 头部名大小写不敏感——`User-Agent` 和 `user-agent` 不能同时发出去
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine import content_sniffer
from sdm.engine.content_sniffer import ContentProbe
from sdm.engine.http_downloader import HTTPDownloader
from sdm.site_rules import SiteRule

# -- merge_headers：头部名大小写不敏感 ----------------------------


def test_merge_headers_later_overrides_earlier():
    """后面的头覆盖前面的。"""
    got = content_sniffer.merge_headers(
        {"User-Agent": "default"}, {"User-Agent": "rule-agent"}
    )
    assert got["User-Agent"] == "rule-agent"
    assert len(got) == 1


def test_merge_headers_is_case_insensitive():
    """大小写不同的同名头只留一个。

    httpx 会把两个都发出去，服务器看到两个 UA 可能挑一个不认的用。
    """
    got = content_sniffer.merge_headers(
        {"User-Agent": "default"}, {"user-agent": "rule-agent"}
    )
    assert len(got) == 1
    assert list(got.values()) == ["rule-agent"]


def test_merge_headers_keeps_distinct_keys():
    """不同名的头都保留。"""
    got = content_sniffer.merge_headers(
        {"User-Agent": "a"}, {"Referer": "http://x/"}
    )
    assert got == {"User-Agent": "a", "Referer": "http://x/"}


def test_merge_headers_handles_none():
    """None 源当成空字典。"""
    got = content_sniffer.merge_headers(None, {"A": "1"}, None)
    assert got == {"A": "1"}


# -- 站点规则本身 --------------------------------------------------


def _make_rule(**kwargs) -> SiteRule:
    defaults = {
        "id": "rule-1",
        "host": "example.com",
        "referer": "",
        "user_agent": "",
        "cookies": "",
        "basic_auth_user": "",
    }
    defaults.update(kwargs)
    return SiteRule(**defaults)


class _FakeStore:
    """假的规则存储，只认一个规则。

    `apply_to_headers` 的行为照抄 site_rules.py:324-338 的实现
    （Referer / UA / Cookie / Basic Auth 四种），不借用真类的方法
    ——免得测试跟着实现一起漂。
    """

    def __init__(self, rule: SiteRule | None = None, password: str = ""):
        self._rule = rule
        self._password = password

    def match(self, url):
        return self._rule

    def apply_to_headers(self, rule, headers):
        if rule.referer:
            headers["Referer"] = rule.referer
        if rule.user_agent:
            headers["User-Agent"] = rule.user_agent
        if rule.cookies:
            headers["Cookie"] = rule.cookies
        if rule.basic_auth_user:
            password = self.get_password(rule.basic_auth_user, rule.host)
            if password:
                raw = f"{rule.basic_auth_user}:{password}".encode()
                token = base64.b64encode(raw).decode("ascii")
                headers["Authorization"] = f"Basic {token}"
        return headers

    def get_password(self, user, host):
        return self._password


@pytest.fixture
def downloader(monkeypatch):
    """默认 UA 是 default-ua 的下载器。"""
    dl = HTTPDownloader(user_agent="default-ua", max_retries=0)
    return dl


def _patch_store(monkeypatch, dl, rule, password=""):
    """把 apply_site_rule 用的单例换成假的。"""
    fake = _FakeStore(rule, password)
    monkeypatch.setattr(
        "sdm.site_rules.SiteRuleStore.shared", staticmethod(lambda: fake)
    )
    return fake


# -- 规则头的应用 --------------------------------------------------


def test_apply_site_rule_sets_referer(downloader, monkeypatch):
    """Referer 注入。"""
    _patch_store(monkeypatch, downloader, _make_rule(referer="http://example.com/page"))
    got = downloader.apply_site_rule("http://example.com/a.zip", {})
    assert got["Referer"] == "http://example.com/page"


def test_apply_site_rule_overrides_user_agent(downloader, monkeypatch):
    """规则里的 UA **覆盖**默认 UA。

    这是验收要点：UA 是覆盖语义，不是叠加。追加在默认 UA 后面
    等于没填。
    """
    _patch_store(monkeypatch, downloader, _make_rule(user_agent="rule-ua"))
    got = downloader.apply_site_rule("http://example.com/a.zip", {})
    assert got["User-Agent"] == "rule-ua"
    assert got["User-Agent"] != "default-ua"


def test_apply_site_rule_sets_cookie(downloader, monkeypatch):
    """Cookie 注入。"""
    _patch_store(monkeypatch, downloader, _make_rule(cookies="sid=abc"))
    got = downloader.apply_site_rule("http://example.com/a.zip", {})
    assert got["Cookie"] == "sid=abc"


def test_apply_site_rule_sets_basic_auth(downloader, monkeypatch):
    """Basic Auth 是 `Basic <base64(user:pass)>`，解码后内容正确。"""
    _patch_store(
        monkeypatch,
        downloader,
        _make_rule(basic_auth_user="alice"),
        password="s3cret",
    )
    got = downloader.apply_site_rule("http://example.com/a.zip", {})
    assert got["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(got["Authorization"][len("Basic ") :]).decode()
    assert decoded == "alice:s3cret"


def test_apply_site_rule_no_match_returns_input(downloader, monkeypatch):
    """没命中规则时原样返回。"""
    _patch_store(monkeypatch, downloader, None)
    headers = {"User-Agent": "default-ua"}
    got = downloader.apply_site_rule("http://example.com/a.zip", headers)
    assert got["User-Agent"] == "default-ua"
    assert "Referer" not in got


def test_apply_site_rule_does_not_mutate_input(downloader, monkeypatch):
    """输入的 headers 不被就地改。"""
    _patch_store(monkeypatch, downloader, _make_rule(referer="http://ref/"))
    headers = {"User-Agent": "default-ua"}
    downloader.apply_site_rule("http://example.com/a.zip", headers)
    assert "Referer" not in headers


# -- 探测请求要带规则头 --------------------------------------------


def _make_probe(url: str) -> ContentProbe:
    return ContentProbe(url=url, content_length=1, content_type="application/zip")


def test_probe_sends_rule_headers(downloader, monkeypatch):
    """探测请求带上规则头。

    有的站点探测阶段就 403——规则不生效的话任务根本起不来。
    """
    sent: list[dict] = []

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        sent.append(dict(extra_headers or {}))
        return _make_probe(url)

    _patch_store(
        monkeypatch,
        downloader,
        _make_rule(user_agent="rule-ua", referer="http://ref/"),
    )
    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    asyncio.run(downloader.probe("http://example.com/a.zip"))

    assert sent, "探测请求没有带任何头"
    assert sent[0]["User-Agent"] == "rule-ua"
    assert sent[0]["Referer"] == "http://ref/"


def test_probe_task_headers_are_lower_priority_than_rule(downloader, monkeypatch):
    """规则优先于任务级的头。

    规则是用户为该主机显式配置的全局设置，任务级 referer 只是
    一次性补充。规则优先换来「配了规则就一定按规则走」。
    """
    sent: list[dict] = []

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        sent.append(dict(extra_headers or {}))
        return _make_probe(url)

    _patch_store(monkeypatch, downloader, _make_rule(user_agent="rule-ua"))
    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    asyncio.run(
        downloader.probe(
            "http://example.com/a.zip",
            extra_headers={"User-Agent": "task-ua", "X-Task": "1"},
        )
    )

    assert sent[0]["User-Agent"] == "rule-ua"
    assert sent[0]["X-Task"] == "1"


def test_probe_default_ua_used_when_no_rule(downloader, monkeypatch):
    """没有规则时用下载器自己的默认 UA。"""
    sent: list[dict] = []

    async def fake_probe(url, client=None, extra_headers=None, timeout=None):
        sent.append(dict(extra_headers or {}))
        return _make_probe(url)

    _patch_store(monkeypatch, downloader, None)
    monkeypatch.setattr("sdm.engine.http_downloader.sniffer_probe", fake_probe)

    asyncio.run(downloader.probe("http://example.com/a.zip"))

    assert sent[0]["User-Agent"] == "default-ua"
