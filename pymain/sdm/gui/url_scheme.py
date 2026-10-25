"""macOS URL scheme 处理：`superdownloadmanager://add?url=<目标>`。

**Swift 版这块是死的**（台账里的 R-13）：`App/SDMApp.swift` 实现了
`handleURLEvent`，但从没在 Info.plist 注册 `CFBundleURLTypes`，系统
根本不会把 URL 送过来。Python 版把注册补上了（`build.py` 往 .app 的
Info.plist 写 `CFBundleURLTypes`），比 Swift 好——但**光注册不够**，
还得有东西接住系统送来的 URL。

**这里为什么用 NSAppleEventManager 而不是 NSApplication 委托：**

pywebview 自己创建并持有 NSApplication 实例，要挂
`application:openURLs:` 就得去改它的委托，风险大。而 URL scheme 的
系统级入口本来就是 Apple Event——`kInternetEventClass` /
`kAEGetURL`。用 `NSAppleEventManager` 注册一个 handler 就能接住，
**不碰 pywebview 的任何东西**。

Windows 那边靠注册表里的 scheme 注册 + 启动参数，不在这个模块里。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..logging_setup import CAT_GUI, get_logger

log = get_logger()

#: 我们注册的 scheme。跟 build.py 写进 Info.plist 的
#: `CFBundleURLSchemes` 一致。
URL_SCHEME = "superdownloadmanager"


def _fourcc(code: str) -> int:
    """四字符码转成 Apple Event 用的整数。

    `'GURL'` → 0x4755524c。`NSAppleEventManager` 的
    `setEventHandler:andSelector:forEventClass:andEventID:` 要的是
    整数，传字符串会报
    `ValueError: depythonifying 'unsigned int', got 'str'`。
    """
    return (
        (ord(code[0]) << 24)
        | (ord(code[1]) << 16)
        | (ord(code[2]) << 8)
        | ord(code[3])
    )


#: kInternetEventClass
_INTERNET_EVENT_CLASS = _fourcc("GURL")
#: kAEGetURL
_AE_GET_URL = _fourcc("GURL")
#: keyDirectObject（Apple Event 描述符里的 '----'）
_KEY_DIRECT_OBJECT = _fourcc("----")


def parse_scheme_url(raw: str) -> str:
    """从 `superdownloadmanager://add?url=<目标>` 里取出目标 URL。

    不是这个格式（或者没带 url 参数）就返回空串。

    支持两种写法：

    - `superdownloadmanager://add?url=http://example.com/a.zip`
    - `superdownloadmanager://http://example.com/a.zip`（直接把目标
      当 netloc 用）
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.lower().startswith(f"{URL_SCHEME}://"):
        return ""

    parsed = urlparse(raw)
    if parsed.netloc == "add":
        target = (parse_qs(parsed.query).get("url") or [""])[0]
        return target.strip()

    # netloc 直接就是目标地址的情况
    if parsed.netloc and not parsed.netloc.startswith("add"):
        rest = parsed.netloc + (parsed.path or "")
        return rest.strip()

    return ""


class _URLSchemeHandler:
    """接住 kAEGetURL 的 ObjC handler。

    **类名必须唯一**——PyObjC 按类名往 Objective-C 运行时注册，
    同名类第二次定义会抛
    `is overriding existing Objective-C class`。所以名字里带一个
    自增序号。

    **方法参数个数要对上。** ObjC 的
    `handleURL:withReplyEvent:` 有两个参数（url event、reply event），
    PyObjC 的 Python 方法就要写 `self, event, reply` 三个——self
    对应 ObjC 的第一个参数。少一个会抛 `BadPrototypeError`。
    """

    _seq = 0

    def __init__(self, on_url: Callable[[str], None]) -> None:
        type(self)._seq += 1
        cls_name = f"_SDMURLSchemeHandler{type(self)._seq}"

        import objc  # noqa: F401
        from Foundation import NSObject

        # 方法体放进类字典的闭包——但 self 要显式接住。
        def _handle(self_, event, reply) -> None:
            url = _extract_url(event)
            if url:
                try:
                    on_url(url)
                except Exception as err:
                    log.warning(CAT_GUI, f"URL scheme 处理失败: {err}")

        cls = type(
            cls_name,
            (NSObject,),
            {"handleURL_withReplyEvent_": _handle},
        )
        self._delegate = cls.alloc().init()

    def install(self) -> bool:
        """把 handler 注册到 NSAppleEventManager。

        返回是否注册成功。非 macOS 直接返回 False。
        """
        if sys.platform != "darwin":
            return False
        try:
            from AppKit import NSAppleEventManager
        except ImportError:
            log.debug(CAT_GUI, "AppKit 不可用，URL scheme 不注册")
            return False

        try:
            manager = NSAppleEventManager.sharedAppleEventManager()
            manager.setEventHandler_andSelector_forEventClass_andEventID_(
                self._delegate,
                "handleURL:withReplyEvent:",
                _INTERNET_EVENT_CLASS,
                _AE_GET_URL,
            )
            return True
        except Exception as err:
            log.warning(CAT_GUI, f"URL scheme 注册失败: {err}")
            return False


def _extract_url(event: Any) -> str:
    """从 kAEGetURL 的 Apple Event 里取出 URL 字符串。

    事件结构：`kAEGetURL` 的 keyDirectObject 参数是
    `typeURL`（'----' 里放的是 URL 字符串本身）。也有版本用
    `typeAlias`，取不到就退回整串。
    """
    try:
        descriptor = event.paramDescriptorForKeyword_(_KEY_DIRECT_OBJECT)
        if descriptor is None:
            return ""
        # paramDescriptorForKeyword_ 返回的是 type 描述符，
        # 真正要取数据得用它的 data。
        data = descriptor.data()
        if data is None:
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", errors="replace")
        return str(data)
    except Exception as err:
        log.debug(CAT_GUI, f"解析 Apple Event 失败: {err}")
        return ""


def make_dispatcher(on_url: Callable[[str], None]) -> Callable[[str], None]:
    """造一个「原始 URL → 目标地址」的回调。

    **为什么必须在这里解析**：Apple Event 里送过来的是完整的
    `superdownloadmanager://add?url=http://example.com/a.zip`，
    不是里面的目标地址。不剥掉这一层就直接入队，引擎会把
    `superdownloadmanager://...` 当成要下载的地址——
    快捷指令那条路（规格 §4.2 的路径 A）就整个是死的。

    单独拆成模块级函数是为了能直接测它——`install()` 那条路要
    真 PyObjC，Linux runner 上根本跑不到。
    """

    def _dispatch(raw: str) -> None:
        target = parse_scheme_url(raw)
        if not target:
            log.debug(CAT_GUI, f"URL scheme 串解析不出目标地址，丢弃: {raw[:120]}")
            return
        on_url(target)

    return _dispatch


def install(on_url: Callable[[str], None]) -> bool:
    """注册 URL scheme handler。

    `on_url` 收到的是**目标地址**（已经剥掉 scheme 外壳），不是
    原始的 `superdownloadmanager://...` 串。

    返回是否注册成功。
    """
    # **平台检查得在构造 handler 之前。**
    # _URLSchemeHandler 的构造函数要 import objc——非 macOS 上
    # 没装 PyObjC，直接抛 ImportError。类里的 install() 虽然也
    # 有平台检查，但那时候 handler 早建好了，压根轮不到它。
    if sys.platform != "darwin":
        return False

    try:
        handler = _URLSchemeHandler(make_dispatcher(on_url))
    except ImportError as err:
        log.debug(CAT_GUI, f"URL scheme 不可用: {err}")
        return False

    ok = handler.install()
    if ok:
        # handler 必须保持强引用——局部变量被回收后系统回调就找
        # 不到对象了，表现为「双击链接唤起 app 但什么反应都没有」。
        _keepalive.append(handler)
        log.info(CAT_GUI, f"URL scheme 已注册（{URL_SCHEME}://）")
    return ok


#: 持有 handler 强引用，防止被 GC 回收。
_keepalive: list[Any] = []
