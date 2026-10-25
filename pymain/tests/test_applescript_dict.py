"""AppleScript 字典（`.sdef`）和 handler 的结构断言。

**这里没有 `sdef` 编译器可用**，所以用两条腿走路：

1. XML 层面——`xml.etree` 解析（顺带用 `xmllint` 对着系统自带的
   `sdef.dtd` 校验过，见 `docs` 里的记录）。
2. 结构层面——自己断言：恰好四个命令、参数和返回值的类型对、
   字典里的四字符码跟 handler 里用的**是同一批**。

**字典和 handler 必须对得上。** 字典改了（换了 command code、换了
property code）而 handler 没改，AppleScript 发来的事件就没人接——
表现为「AppleScript 一点没反应」，而且不报任何错。
所以这里有一条测试专门比对两边的常量。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from sdm.gui.applescript import applescript_handler

#: 契约 §5.3 要求的四个命令。
EXPECTED_COMMANDS = {
    "add download",
    "pause all downloads",
    "resume all downloads",
    "get download list",
}


@pytest.fixture(scope="module")
def sdef_path() -> Path:
    """`.sdef` 文件的位置。"""
    return applescript_handler.sdef_path()


@pytest.fixture(scope="module")
def sdef_tree(sdef_path: Path) -> ET.Element:
    """解析好的字典。"""
    return ET.parse(sdef_path).getroot()


# -- XML 层面 -------------------------------------------------------


def test_sdef_file_exists(sdef_path: Path) -> None:
    assert sdef_path.is_file(), f"找不到字典文件：{sdef_path}"
    assert sdef_path.name == "SuperDownloadManager.sdef"


def test_sdef_is_well_formed_xml(sdef_path: Path) -> None:
    """良构的 XML。解析不过就说明文件写坏了。"""
    root = ET.parse(sdef_path).getroot()
    assert root.tag == "dictionary"


def test_sdef_has_exactly_one_suite(sdef_tree: ET.Element) -> None:
    """命令得在一个 suite 里——事件类就是 suite 的 code。

    `<dictionary>` 直接挂命令的话，AppleScript 那边没有事件类可用。
    """
    suites = sdef_tree.findall("suite")
    assert len(suites) == 1
    assert suites[0].get("code"), "suite 必须有 code——它是事件类"


def test_sdef_declares_exactly_the_four_required_commands(sdef_tree: ET.Element) -> None:
    """恰好四个命令，不多不少。"""
    commands = sdef_tree.findall(".//command")
    names = [command.get("name") for command in commands]
    assert set(names) == EXPECTED_COMMANDS
    assert len(names) == 4, f"命令重复了：{names}"


def test_every_command_has_a_code(sdef_tree: ET.Element) -> None:
    """每个命令都要有 code（事件 ID），否则 AppleScript 发不出来。"""
    for command in sdef_tree.findall(".//command"):
        assert command.get("code"), f"{command.get('name')} 缺 code"


def test_add_download_takes_a_url_and_returns_a_task_id(sdef_tree: ET.Element) -> None:
    """`add download "URL"` → 返回任务 id。"""
    command = _command(sdef_tree, "add download")
    direct = command.find("direct-parameter")
    assert direct is not None
    assert direct.get("type") == "text", "直接参数应该是 URL 字符串"

    result = command.find("result")
    assert result is not None
    assert result.get("type") == "text", "返回值应该是任务 id（文本）"


@pytest.mark.parametrize("name", ["pause all downloads", "resume all downloads"])
def test_pause_and_resume_return_true(sdef_tree: ET.Element, name: str) -> None:
    """暂停 / 继续返回布尔。"""
    command = _command(sdef_tree, name)
    result = command.find("result")
    assert result is not None
    assert result.get("type") == "boolean"


def test_get_download_list_returns_a_list_of_records(sdef_tree: ET.Element) -> None:
    """`get download list` → 记录列表（name + state）。"""
    command = _command(sdef_tree, "get download list")
    result = command.find("result")
    assert result is not None
    types = result.findall("type")
    assert len(types) == 1
    assert types[0].get("list") == "yes", "返回的应该是列表"
    assert types[0].get("type") == "download info", "元素类型是 download info"


def test_record_type_has_name_and_state_text_properties(sdef_tree: ET.Element) -> None:
    """记录类型有 name / state 两个文本属性。"""
    records = sdef_tree.findall(".//record-type")
    assert len(records) == 1
    record = records[0]
    assert record.get("name") == "download info"

    properties = {prop.get("name"): prop for prop in record.findall("property")}
    assert set(properties) == {"name", "state"}
    for name, prop in properties.items():
        assert prop.get("type") == "text", f"{name} 应该是文本"
        assert prop.get("code"), f"{name} 缺 code（AE 四字符码）"


# -- 字典 ↔ handler 的一致性 ----------------------------------------


def test_suite_code_matches_the_handler_event_class(sdef_tree: ET.Element) -> None:
    """字典里 suite 的 code 跟 handler 的事件类必须是同一个。

    对不上 → AppleScript 发来的事件没人接。
    """
    suite_code = sdef_tree.find("suite").get("code")
    assert suite_code == "SDMD"
    assert _fourcc(suite_code) == applescript_handler.EVENT_CLASS


def test_command_codes_match_the_handler_event_ids(sdef_tree: ET.Element) -> None:
    """四个命令的事件 ID 跟 handler 里注册的一致。"""
    expected = {
        "add download": applescript_handler.EVENT_ID_ADD_DOWNLOAD,
        "pause all downloads": applescript_handler.EVENT_ID_PAUSE_ALL,
        "resume all downloads": applescript_handler.EVENT_ID_RESUME_ALL,
        "get download list": applescript_handler.EVENT_ID_GET_LIST,
    }
    for command in sdef_tree.findall(".//command"):
        name = command.get("name")
        assert expected[name] == _fourcc(command.get("code")), f"{name} 的 code 对不上"


def test_property_codes_match_the_handler_constants(sdef_tree: ET.Element) -> None:
    """record 的属性 code 跟 handler 拼字节时用的一致。

    对不上 → AppleScript 取不到 `name of item 1` / `state of item 1`。
    """
    record = sdef_tree.find(".//record-type")
    codes = {prop.get("name"): prop.get("code") for prop in record.findall("property")}
    assert codes["name"] == applescript_handler.PROPERTY_NAME_CODE
    assert codes["state"] == applescript_handler.PROPERTY_STATE_CODE


# -- 描述符构造 -----------------------------------------------------


def test_record_blob_is_byte_identical_to_an_applescript_built_record() -> None:
    """**handler 拼的 record 字节必须跟 AppleScript 自己造的一模一样。**

    格式是逆向出来的（`-[NSAppleEventDescriptor data]` 逐字节拆开），
    这条断言就是那个逆向过程的护栏——格式理解错了，字节就会对不上，
    AppleScript 那边看到的就是空记录。

    只有装了 PyObjC 的 macOS 上能验。
    """
    pytest.importorskip("Foundation")
    if applescript_handler.sys.platform != "darwin":
        pytest.skip("Apple Event 描述符只有 macOS 上有")

    from Foundation import NSAppleScript

    reference, error = (
        NSAppleScript.alloc()
        .initWithSource_('{name: "a.zip", url: "http://x"}')
        .executeAndReturnError_(None)
    )
    assert error is None, "参考用的 AppleScript 片段自己就编译失败了"

    # AppleScript 造的那条 record 的序列化字节（去掉最外层 16 字节的
    # event 头，就是 handler 拼的那个 inner）。
    reference_bytes = bytes(reference.data().bytes())

    ours = applescript_handler.record_blob(
        [("pnam", "a.zip"), ("url ", "http://x")]
    )
    assert ours == reference_bytes, "拼出来的 record 字节跟 AppleScript 造的不一样"


def test_record_descriptor_is_recognised_as_a_record() -> None:
    """拼出来的描述符得真的被当成 record（`isRecordDescriptor`）。"""
    pytest.importorskip("Foundation")
    if applescript_handler.sys.platform != "darwin":
        pytest.skip("Apple Event 描述符只有 macOS 上有")

    descriptor = applescript_handler.record_descriptor(
        [
            (applescript_handler.PROPERTY_NAME_CODE, "a.zip"),
            (applescript_handler.PROPERTY_STATE_CODE, "downloading"),
        ]
    )
    assert descriptor is not None
    assert descriptor.isRecordDescriptor() is True
    # 两个属性都在
    text = str(descriptor)
    assert "a.zip" in text
    assert "downloading" in text


def test_list_descriptor_keeps_the_order() -> None:
    """`get download list` 返回的列表要保持任务顺序。"""
    pytest.importorskip("Foundation")
    if applescript_handler.sys.platform != "darwin":
        pytest.skip("Apple Event 描述符只有 macOS 上有")

    records = [
        applescript_handler.record_descriptor(
            [
                (applescript_handler.PROPERTY_NAME_CODE, name),
                (applescript_handler.PROPERTY_STATE_CODE, state),
            ]
        )
        for name, state in (
            ("first.zip", "downloading"),
            ("second.zip", "completed"),
            ("third.zip", "paused"),
        )
    ]
    descriptor = applescript_handler.list_descriptor(records)
    assert descriptor is not None
    text = str(descriptor)
    assert text.index("first.zip") < text.index("second.zip") < text.index("third.zip")


def test_text_and_boolean_descriptors_are_available() -> None:
    """文本 / 布尔描述符能造出来。"""
    pytest.importorskip("Foundation")
    if applescript_handler.sys.platform != "darwin":
        pytest.skip("Apple Event 描述符只有 macOS 上有")

    assert applescript_handler.text_descriptor("abc") is not None
    assert applescript_handler.boolean_descriptor(True) is not None


def test_install_is_a_noop_off_macos() -> None:
    """非 macOS 上不注册（也不该炸）。"""
    if applescript_handler.sys.platform == "darwin":
        pytest.skip("macOS 上真的会注册")
    assert applescript_handler.install(object()) is False


def _command(tree: ET.Element, name: str) -> ET.Element:
    """按名字找一个命令。"""
    for command in tree.findall(".//command"):
        if command.get("name") == name:
            return command
    raise AssertionError(f"字典里没有命令：{name}")


def _fourcc(code: str) -> int:
    """四字符码 → 整数（跟 handler 里同一套算法）。"""
    return (
        (ord(code[0]) << 24)
        | (ord(code[1]) << 16)
        | (ord(code[2]) << 8)
        | ord(code[3])
    )
