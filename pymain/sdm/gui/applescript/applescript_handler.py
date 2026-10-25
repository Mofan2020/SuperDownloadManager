"""AppleScript 命令处理（契约 §5.3 / 规格 §4.3）。

**怎么接住 AppleScript 发来的命令：**

AppleScript 编译之后是一个 Apple Event，发给「应用本身」——
事件类是这个应用字典里 `<suite code=...>` 的 code，事件 ID 是对应
`<command code=...>` 的 code。在 Python 侧用 `NSAppleEventManager`
注册 handler 就能接住，**跟 `url_scheme.py` 接 `kInternetEventClass` /
`kAEGetURL` 是同一套机制**。

**为什么不走 NSScriptCommand：** 正经的可脚本化应用要用
`NSScriptCommand` 子类 + 在 NSApplication 委托里实现
`application:commandForEvent:userData:`。但 NSApplication 是
pywebview 建的、委托也是它的——去改别人的委托风险太大。
`NSAppleEventManager` 挂在系统这一层，**不碰 pywebview 的任何东西**。

**三个坑（跟 url_scheme.py 一样，也是从 url_scheme.py 学来的）：**

1. 事件类 / 事件 ID 必须是**整数**，不是字符串。传字符串会报
   `ValueError: depythonifying 'unsigned int', got 'str'`。
2. ObjC 的 selector 有两个参数时，Python 方法要写 `self` + 两个参数。
   少一个会抛 `BadPrototypeError`。
3. handler 必须**保持强引用**——局部变量被回收后系统回调就找不到
   对象了，表现为「AppleScript 点了没反应」。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any

from ...logging_setup import CAT_GUI, get_logger

log = get_logger()


def _fourcc(code: str) -> int:
    """四字符码转成 Apple Event 用的整数。

    `'SDMD'` → 0x53444d44。`NSAppleEventManager` 的
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


#: 事件类 = `.sdef` 里 `<suite code="SDMD">` 的 code。
#:
#: **改 `.sdef` 的 suite code 必须同步改这里**，否则 AppleScript 发来的
#: 事件没人接。
EVENT_CLASS = _fourcc("SDMD")

#: 各命令的事件 ID，跟 `.sdef` 里 `<command code=...>` 一一对应。
EVENT_ID_ADD_DOWNLOAD = _fourcc("ADdl")
EVENT_ID_PAUSE_ALL = _fourcc("APau")
EVENT_ID_RESUME_ALL = _fourcc("ARes")
EVENT_ID_GET_LIST = _fourcc("AGet")

#: record 里两个属性的四字符码，跟 `.sdef` 里 `<property code=...>` 一致。
#:
#: 'pnam' 是标准 suite 里 `name` 属性的代码；'cSta' 是我们自己给
#: `state` 定的代码。**必须跟 `.sdef` 一致**，否则 AppleScript 取不到
#: `state of item 1`。
PROPERTY_NAME_CODE = "pnam"
PROPERTY_STATE_CODE = "cSta"

#: keyDirectObject（Apple Event 描述符里的 '----'）
_KEY_DIRECT_OBJECT = _fourcc("----")

#: 字典文件的位置。打包时它要进 `Contents/Resources/`，
#: `Info.plist` 里写 `OSAScriptingDefinition` 指向它。
SDEF_NAME = "SuperDownloadManager.sdef"


def sdef_path() -> Path:
    """`.sdef` 文件的位置。

    **源码运行时**是模块旁边的那个文件。

    **打包后**模块在 `_internal/` 里，而字典被 build.py 复制到了
    `Contents/Resources/`——两者不是同一个地方。所以这里要按
    `sys.frozen` 分开找，不然打包后这个函数会指向一个不存在的
    路径（诊断信息会说「词典不在」，其实是找错地方了）。

    运行时真正读词典的是系统（按 Info.plist 的
    `OSAScriptingDefinition` 找），这个函数只用于诊断和测试。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate = Path(meipass) / SDEF_NAME
            if candidate.is_file():
                return candidate
        # onedir 的 .app：Contents/Resources/ 在 Contents/MacOS/ 的上一级
        exe = Path(sys.executable).resolve()
        for parent in exe.parents:
            candidate = parent / "Resources" / SDEF_NAME
            if candidate.is_file():
                return candidate
    return Path(__file__).parent / SDEF_NAME


# -- 描述符构造 -----------------------------------------------------


def _u32(value: int) -> bytes:
    """4 字节大端整数。Apple Event 的长度字段都是这个格式。"""
    return struct.pack(">I", value)


def _text_bytes(value: str) -> bytes:
    """'utxt' 文本描述符：UTF-16BE + 字节长度。

    Apple Event 的文本是 **UTF-16BE**，不是 UTF-8。
    """
    raw = value.encode("utf-16-be")
    return b"utxt" + _u32(len(raw)) + raw


def record_blob(properties: list[tuple[str, str]]) -> bytes:
    """拼出一条 record 的序列化字节。

    **这个格式是逆向出来的，不是猜的。** 用 NSAppleScript 造一条同样
    的 record（`{name: "a.zip", url: "http://x"}`），把
    `-[NSAppleEventDescriptor data]` 的字节逐个拆开，再照着拼——
    拼出来的结果跟 AppleScript 自己造的**逐字节相同**
    （`tests/test_applescript_dict.py` 里有这条断言）。

    结构（从外往里）：

        'dle2' | u32(0) | 'reco' | u32(len(inner)) | inner

    其中 inner 是：

        u32(0) | u32(0) | u32(24) | 'reco' | u32(属性个数) | u32(0) | 属性们

    每个属性是：四字符码（属性名）| 'utxt' | u32(字节数) | UTF-16BE 文本。

    **属性名用的是四字符码，不是字符串。** 这是最容易搞错的地方：
    record 里根本没有 "name" 这个字符串，存的是 'pnam'（标准 suite 里
    name 属性的代码）。所以 `state` 属性的代码必须跟 `.sdef` 里
    `<property code="cSta">` 对上，否则 AppleScript 取不到
    `state of item 1`。

    那个 u32(24) 是「从内层 'reco' 到第一个变长数据的偏移」，
    跟属性个数无关（1 个属性和 2 个属性都是 24）。
    """
    body = b"".join(
        code.encode("ascii") + _text_bytes(value) for code, value in properties
    )
    inner = (
        _u32(0)
        + _u32(0)
        + _u32(24)
        + b"reco"
        + _u32(len(properties))
        + _u32(0)
        + body
    )
    return b"dle2" + _u32(0) + b"reco" + _u32(len(inner)) + inner


def record_descriptor(properties: list[tuple[str, str]]) -> Any:
    """造一条 record 描述符（name / state 那种）。"""
    from Foundation import NSAppleEventDescriptor

    blob = record_blob(properties)
    return NSAppleEventDescriptor.alloc().initWithDescriptorType_bytes_length_(
        _fourcc("reco"), blob, len(blob)
    )


def list_descriptor(elements: list[Any]) -> Any:
    """造一条 list 描述符。

    **`insertDescriptor_atIndex_(desc, 0)` 是追加到末尾**，不是插到
    头——插 0 会把新元素放到最后（实测插 1 会顶掉第 0 个，
    所以一律插 0）。
    """
    from Foundation import NSAppleEventDescriptor

    result = NSAppleEventDescriptor.alloc().initListDescriptor()
    for element in elements:
        result.insertDescriptor_atIndex_(element, 0)
    return result


def text_descriptor(value: str) -> Any:
    """文本描述符。"""
    from Foundation import NSAppleEventDescriptor

    return NSAppleEventDescriptor.descriptorWithString_(value)


def boolean_descriptor(value: bool) -> Any:
    """布尔描述符。"""
    from Foundation import NSAppleEventDescriptor

    return NSAppleEventDescriptor.descriptorWithBoolean_(bool(value))


# -- 事件处理 -------------------------------------------------------


def _direct_text(event: Any) -> str:
    """取命令的直接参数（`add download "URL"` 里那个 URL）。"""
    try:
        descriptor = event.paramDescriptorForKeyword_(_KEY_DIRECT_OBJECT)
        if descriptor is None:
            return ""
        try:
            value = descriptor.stringValue()
        except Exception:
            data = descriptor.data()
            if data is None:
                return ""
            if isinstance(data, bytes):
                value = data.decode("utf-16-be", errors="replace")
            else:
                value = str(data)
        return str(value or "").strip()
    except Exception as err:
        log.debug(CAT_GUI, f"解析 AppleScript 直接参数失败: {err}")
        return ""


def _reply(reply: Any, descriptor: Any) -> None:
    """把结果写回 Apple Event 的 reply。

    **不回写的话 AppleScript 那边拿到的就是空值**——命令看起来
    「执行了但什么都没返回」。
    """
    setter = getattr(reply, "setResult_", None)
    if setter is None:
        log.debug(CAT_GUI, "Apple Event reply 不支持 setResult:")
        return
    try:
        setter(descriptor)
    except Exception as err:
        log.warning(CAT_GUI, f"AppleScript 结果回写失败: {err}")


class _AppleScriptDelegate:
    """接住四个命令的 ObjC handler。

    **类名必须唯一**——PyObjC 按类名往 Objective-C 运行时注册，
    同名类第二次定义会抛
    `is overriding existing Objective-C class`。所以名字里带一个
    自增序号。

    **每个命令一个 selector**，而不是一个 selector 靠事件 ID 分发：
    selector 名字是固定写在类里的，不用在运行时读 `eventID()`
    （`NSAppleEvent` 这个类 PyObjC 没导出，只能靠 duck typing，
    能不依赖就不依赖）。
    """

    _seq = 0

    def __init__(self, service: Any) -> None:
        type(self)._seq += 1
        cls_name = f"_SDMAppleScriptDelegate{type(self)._seq}"

        import objc  # noqa: F401
        from Foundation import NSObject

        # 方法体放进类字典的闭包——self 要显式接住。
        def _add(self_, event, reply) -> None:
            url = _direct_text(event)
            if not url:
                log.warning(CAT_GUI, "AppleScript add download 少了 URL")
                _reply(reply, text_descriptor(""))
                return
            # **必须走 bridge。** bridge.add_url 内部才走引擎的状态机
            # （排队、并发上限、限速、目录分类）。直接调引擎就是开了
            # 个后门，GUI 里看到的和实际下载的会对不上。
            result = service.add_url(url) or {}
            task_id = str(result.get("task_id") or "")
            if not result.get("ok"):
                log.warning(CAT_GUI, f"AppleScript 添加下载失败: {result.get('error')}")
            _reply(reply, text_descriptor(task_id))

        def _pause(self_, event, reply) -> None:
            result = service.pause_all() or {}
            _reply(reply, boolean_descriptor(bool(result.get("ok"))))

        def _resume(self_, event, reply) -> None:
            result = service.resume_all() or {}
            _reply(reply, boolean_descriptor(bool(result.get("ok"))))

        def _list(self_, event, reply) -> None:
            records = []
            for task in service.get_tasks() or []:
                name = str(task.get("filename") or task.get("url") or "")
                state = str(task.get("state") or "")
                records.append(
                    record_descriptor(
                        [(PROPERTY_NAME_CODE, name), (PROPERTY_STATE_CODE, state)]
                    )
                )
            _reply(reply, list_descriptor(records))

        cls = type(
            cls_name,
            (NSObject,),
            {
                "handleAddDownload_withReplyEvent_": _add,
                "handlePauseAll_withReplyEvent_": _pause,
                "handleResumeAll_withReplyEvent_": _resume,
                "handleGetDownloadList_withReplyEvent_": _list,
            },
        )
        self._delegate = cls.alloc().init()

    def install(self) -> bool:
        """把 handler 注册到 NSAppleEventManager。返回是否成功。"""
        if sys.platform != "darwin":
            return False
        try:
            from AppKit import NSAppleEventManager
        except ImportError:
            log.debug(CAT_GUI, "AppKit 不可用，AppleScript 不注册")
            return False

        try:
            manager = NSAppleEventManager.sharedAppleEventManager()
            for selector, event_id in (
                ("handleAddDownload:withReplyEvent:", EVENT_ID_ADD_DOWNLOAD),
                ("handlePauseAll:withReplyEvent:", EVENT_ID_PAUSE_ALL),
                ("handleResumeAll:withReplyEvent:", EVENT_ID_RESUME_ALL),
                ("handleGetDownloadList:withReplyEvent:", EVENT_ID_GET_LIST),
            ):
                manager.setEventHandler_andSelector_forEventClass_andEventID_(
                    self._delegate, selector, EVENT_CLASS, event_id
                )
            return True
        except Exception as err:
            log.warning(CAT_GUI, f"AppleScript 注册失败: {err}")
            return False


def install(service: Any) -> bool:
    """注册 AppleScript handler。

    `service` 是 GUI 桥接层（`GuiBridge`）——**命令必须走它**，不能
    直接调引擎。`bridge.add_url` / `pause_all` / `resume_all` 内部
    才走引擎的状态机（排队、并发上限、限速），绕过去就等于开了个
    后门。

    返回是否注册成功。非 macOS 返回 False。
    """
    # **平台检查得在构造 handler 之前。**
    # _AppleScriptDelegate 的构造函数要 import objc——非 macOS 上
    # 没装 PyObjC 会直接抛 ImportError。
    if sys.platform != "darwin":
        return False

    try:
        delegate = _AppleScriptDelegate(service)
    except ImportError as err:
        log.debug(CAT_GUI, f"AppleScript 支持不可用: {err}")
        return False

    ok = delegate.install()
    if ok:
        # handler 必须保持强引用——被 GC 回收后系统回调就找不到对象了。
        _keepalive.append(delegate)
        log.info(CAT_GUI, "AppleScript 命令已注册")
    return ok


#: 持有 handler 强引用，防止被 GC 回收。
_keepalive: list[Any] = []
