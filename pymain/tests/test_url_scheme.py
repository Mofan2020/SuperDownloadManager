"""URL scheme 测试：F064。

覆盖：
- `superdownloadmanager://add?url=<目标>` 的解析
- 非本 scheme 的 URL 不解析
- handler 装配后能从 Apple Event 里取出目标地址
- 注册失败不抛异常

**这些测试不真的注册系统 handler。** 注册那一步用 monkeypatch 挡掉，
只测纯逻辑（解析、事件取值、装配）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.gui import url_scheme

# -- 假的 PyObjC ----------------------------------------------------
#
# handler 的构造函数要 `import objc` 和 `from Foundation import NSObject`。
# **Linux 上没有 PyObjC**——真 Foundation 根本 import 不了。
# 之前的测试在 macOS 上因为真 PyObjC 兜住了才没暴露，Linux runner
# 上直接 ImportError。
#
# handler 用 `type(name, (NSObject,), {...})` 建子类，再 `cls.alloc().init()`，
# 所以假的 NSObject 得有两样：能当基类用，自带 alloc()/init()。


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


@pytest.fixture(autouse=True)
def _fake_pyobjc(monkeypatch):
    """装上假的 objc 和 Foundation。"""
    import types

    objc = types.ModuleType("objc")
    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject

    monkeypatch.setitem(sys.modules, "objc", objc)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    yield


# -- 解析 ----------------------------------------------------------


def test_parse_add_scheme_with_query():
    """标准写法：?url=<目标>。"""
    got = url_scheme.parse_scheme_url(
        "superdownloadmanager://add?url=http://example.com/a.zip"
    )
    assert got == "http://example.com/a.zip"


def test_parse_add_scheme_url_encoded():
    """目标地址带 query 也能正确取出来。"""
    got = url_scheme.parse_scheme_url(
        "superdownloadmanager://add?url=http%3A%2F%2Fexample.com%2Fa.zip%3Fx%3D1"
    )
    assert got == "http://example.com/a.zip?x=1"


def test_parse_direct_netloc_form():
    """直接把目标地址当 netloc 的写法也认。"""
    got = url_scheme.parse_scheme_url("superdownloadmanager://example.com/a.zip")
    assert got == "example.com/a.zip"


def test_parse_rejects_other_scheme():
    """不是本 scheme 的一律不解析。"""
    assert url_scheme.parse_scheme_url("https://example.com/a.zip") == ""
    assert url_scheme.parse_scheme_url("") == ""
    assert url_scheme.parse_scheme_url("superdownloadmanagerx://add?url=x") == ""


def test_parse_add_scheme_without_url_param():
    """没有 url 参数就返回空串。"""
    assert url_scheme.parse_scheme_url("superdownloadmanager://add") == ""
    assert url_scheme.parse_scheme_url("superdownloadmanager://add?other=1") == ""


# -- 四字符码 ------------------------------------------------------


def test_fourcc_conversion():
    """四字符码转整数。'GURL' = 0x4755524c。"""
    assert url_scheme._fourcc("GURL") == 0x4755524C
    assert url_scheme._fourcc("----") == 0x2D2D2D2D


# -- 事件取值 ------------------------------------------------------


class _FakeDescriptor:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeEvent:
    def __init__(self, data):
        self._descriptor = _FakeDescriptor(data)

    def paramDescriptorForKeyword_(self, keyword):
        assert keyword == url_scheme._KEY_DIRECT_OBJECT
        return self._descriptor


def test_extract_url_from_event():
    """从 Apple Event 里取出 URL 字符串。"""
    event = _FakeEvent(b"superdownloadmanager://add?url=http://example.com/a.zip")
    got = url_scheme._extract_url(event)
    assert got == "superdownloadmanager://add?url=http://example.com/a.zip"


def test_extract_url_from_event_with_str():
    """data 返回 str 也能处理。"""
    event = _FakeEvent("superdownloadmanager://add?url=http://example.com/a.zip")
    got = url_scheme._extract_url(event)
    assert got.startswith("superdownloadmanager://")


def test_extract_url_missing_descriptor():
    """取不到描述符时返回空串，不抛异常。"""

    class _Empty:
        def paramDescriptorForKeyword_(self, keyword):
            return None

    assert url_scheme._extract_url(_Empty()) == ""


# -- handler 装配 --------------------------------------------------


def test_handler_invokes_callback_with_url():
    """handler 收到事件后，把取出的 URL 交给回调。"""
    received: list[str] = []
    handler = url_scheme._URLSchemeHandler(received.append)

    event = _FakeEvent(b"superdownloadmanager://add?url=http://example.com/a.zip")
    # 直接调 ObjC 方法
    handler._delegate.handleURL_withReplyEvent_(event, None)

    assert received == ["superdownloadmanager://add?url=http://example.com/a.zip"]


def test_handler_empty_event_does_not_call_back():
    """空事件不触发回调。"""
    received: list[str] = []
    handler = url_scheme._URLSchemeHandler(received.append)

    handler._delegate.handleURL_withReplyEvent_(_FakeEvent(b""), None)

    assert received == []


def test_handler_callback_exception_is_contained():
    """回调抛异常不能把事件处理带崩。"""

    def boom(_url: str) -> None:
        raise RuntimeError("下载入队失败")

    handler = url_scheme._URLSchemeHandler(boom)
    event = _FakeEvent(b"superdownloadmanager://add?url=http://example.com/a.zip")
    # 不抛异常就算过
    handler._delegate.handleURL_withReplyEvent_(event, None)


def test_handler_classes_have_unique_names():
    """每次建 handler 的 ObjC 类名都不同。

    PyObjC 按类名往 ObjC 运行时注册，同名类第二次定义会抛
    「is overriding existing Objective-C class」。建两个 handler
    验证不炸。
    """
    a = url_scheme._URLSchemeHandler(lambda u: None)
    b = url_scheme._URLSchemeHandler(lambda u: None)
    assert type(a._delegate) is not type(b._delegate)


def test_install_on_non_darwin_returns_false(monkeypatch):
    """非 macOS 上注册直接返回 False，不抛异常。"""
    monkeypatch.setattr(url_scheme.sys, "platform", "linux")
    assert url_scheme.install(lambda u: None) is False
