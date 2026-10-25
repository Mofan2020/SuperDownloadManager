"""UI 层契约测试（规格 §2 + 契约文档 §3 / §6）。

这些用例只断言**契约本身**——桥接层的方法名、设置字段、文案表、
前端结构。不碰引擎的新方法（`probe_url` / `get_speed_history` …），
那些还在并行开发中，等它们落地再补行为测试。

对应契约：`docs/api-contract.md` §3.1（桥接层 API）、§3.2（推送）、
§6（设置新增键）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.gui import bridge as bridge_mod
from sdm.settings import _KEY_MAP, Settings

ASSETS = ROOT / "sdm" / "gui" / "assets"
I18N = ROOT / "sdm" / "i18n"


# -- 桥接层：契约 §3.1 的 9 个必需方法 -------------------------------

#: 契约 §3.1 明确要求的 9 个方法。名字必须一字不差。
REQUIRED_BRIDGE_METHODS = (
    "create_task",
    "pause_task",
    "resume_task",
    "remove_task",
    "get_tasks",
    "get_task_detail",
    "set_global_speed_limit",
    "get_settings",
    "save_settings",
)

#: 契约 §3.1 说「已有的旧名字保留为别名，别删」。
LEGACY_BRIDGE_ALIASES = (
    "add_url",
    "update_settings",
    "get_task",
    "get_task_chunks",
    "cancel_task",
    "retry_task",
    "clear_completed",
    "pause_all",
    "resume_all",
    "set_task_speed_limit",
    "relink_task",
    "set_language",
    "choose_directory",
    "get_clipboard_candidate",
    "set_clipboard_candidate",
)

#: 契约 §3.1 的「新增」表。
NEW_BRIDGE_METHODS = (
    "probe_url",
    "set_global_proxy",
    "set_task_proxy",
    "set_task_options",
    "move_task",
    "retry_task",
    "get_task_error",
    "get_speed_history",
)


@pytest.mark.parametrize("name", REQUIRED_BRIDGE_METHODS)
def test_required_bridge_method_exists(name: str) -> None:
    """契约要求的 9 个方法都在。"""
    assert hasattr(bridge_mod.GuiBridge, name), f"GuiBridge 缺方法 {name}"


@pytest.mark.parametrize("name", LEGACY_BRIDGE_ALIASES)
def test_legacy_alias_still_exists(name: str) -> None:
    """旧名字还在——前端和 CLI 还在用，删了就断。"""
    assert hasattr(bridge_mod.GuiBridge, name), f"别名 {name} 被删了"


@pytest.mark.parametrize("name", NEW_BRIDGE_METHODS)
def test_new_bridge_method_exists(name: str) -> None:
    """契约 §3.1「新增」表里的方法都在。"""
    assert hasattr(bridge_mod.GuiBridge, name), f"GuiBridge 缺方法 {name}"


def test_bridge_methods_are_callable() -> None:
    """方法得是真能调的东西（不是属性、不是 None）。"""
    for name in REQUIRED_BRIDGE_METHODS + NEW_BRIDGE_METHODS:
        attr = getattr(bridge_mod.GuiBridge, name, None)
        assert callable(attr), f"{name} 不可调用"


def test_poll_interval_lowered_to_1500ms() -> None:
    """轮询降到 1500ms——推送成了主通道（契约 §3.2）。

    700ms 那个间隔是写在 app.js 里的字面量，改它得改前端，
    所以这里直接查前端源码。
    """
    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "1500" in app_js, "前端没有 1500ms 的轮询间隔"
    assert "}, 700);" not in app_js, "前端还在按 700ms 轮询"


def test_frontend_has_sdm_event_entry_point() -> None:
    """前端注册了唯一的推送入口 window.sdmEvent(kind, payload)。"""
    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "window.sdmEvent" in app_js, "app.js 里没有 window.sdmEvent"
    for kind in ("task_updated", "task_removed", "global_speed_updated", "notice", "settings_updated"):
        assert kind in app_js, f"前端没处理 {kind} 事件"


def test_bridge_emits_events_through_sdm_event() -> None:
    """Python 侧按契约推事件：调 window.sdmEvent(kind, payload)。"""
    import inspect

    source = inspect.getsource(bridge_mod.GuiBridge._emit_event)
    assert "sdmEvent" in source, "桥接层没有通过 sdmEvent 推事件"


# -- 设置：契约 §6 的新增键 ------------------------------------------

NEW_SETTING_FIELDS = {
    "max_concurrent_tasks": 5,
    "theme": "system",
    "proxy_url": "",
    "proxy_scheme": "http",
    "browser_pair_token": "",
    "initial_connections": 2,
    "max_chunk_count": 32,
}


@pytest.mark.parametrize("field,default", sorted(NEW_SETTING_FIELDS.items()))
def test_new_setting_field_default(field: str, default) -> None:
    """契约 §6 的新字段都在，默认值一样。"""
    settings = Settings()
    assert hasattr(settings, field), f"Settings 缺字段 {field}"
    assert getattr(settings, field) == default, f"{field} 的默认值不对"


@pytest.mark.parametrize("field", sorted(NEW_SETTING_FIELDS))
def test_new_setting_field_in_key_map(field: str) -> None:
    """新字段都进了 _KEY_MAP（Swift 版键名映射）。"""
    assert field in _KEY_MAP.values(), f"{field} 没进 _KEY_MAP"


def test_hidden_columns_setting_exists() -> None:
    """列的显隐要能存下来（规格 §2.1 的列选择器）。"""
    settings = Settings()
    assert hasattr(settings, "hidden_columns")
    assert settings.hidden_columns == []
    assert "hidden_columns" in _KEY_MAP.values()


def test_settings_round_trip_keeps_list_values(tmp_path) -> None:
    """列表型设置要能读回来。

    **这是真踩过的坑**：Settings.load() 原来按类型分支填值，
    list 一个分支都不匹配 → 列表型设置永远读不回来。
    """
    from sdm.settings import SettingsStore

    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.update({"hidden_columns": ["speed", "eta"]})
    store.save()

    again = SettingsStore(path)
    assert again.settings.hidden_columns == ["speed", "eta"]


# -- i18n：两份文案表的键必须一样 ------------------------------------


def _catalog(name: str) -> dict:
    return json.loads((I18N / name).read_text(encoding="utf-8"))


def test_i18n_key_sets_identical() -> None:
    """中英 key 集合必须一致——漏一边的话另一种语言下显示成 key。"""
    zh = set(_catalog("zh-Hans.json"))
    en = set(_catalog("en.json"))
    assert zh == en, f"中英 key 不一致: {sorted(zh ^ en)}"


NEW_I18N_KEYS = (
    # 状态栏
    "statusBarSpeed",
    "statusBarActive",
    "statusBarLimit",
    "statusBarNoLimit",
    # 列
    "listColumnCategory",
    "listColumnProgress",
    "listColumnEta",
    "columnPickerTitle",
    # 主题
    "themeToggle",
    "themeSystem",
    "themeLight",
    "themeDark",
    # 分类名
    "categoryVideo",
    "categoryMusic",
    "categoryArchives",
    "categoryDocuments",
    "categorySoftware",
    "categoryOther",
    # 新建任务对话框
    "addSheetSaveDirField",
    "addSheetCategoryField",
    "addSheetProxyField",
    "addSheetHeadersField",
    "addSheetChunkCountField",
    "addSheetChunkAuto",
    "addSheetChunkManual",
    "addSheetStartWhenField",
    "addSheetStartNow",
    "addSheetStartQueue",
    "addSheetStartScheduled",
    "addSheetParsing",
    "addSheetProbeFailed",
    "addSheetReprobe",
    # 详情面板
    "detailDownloaded",
    "detailUploaded",
    "detailConnections",
    "detailLogSummary",
    "detailSpeedCurve",
)


@pytest.mark.parametrize("key", NEW_I18N_KEYS)
def test_new_i18n_key_in_both_catalogs(key: str) -> None:
    """新增的文案键中英都有。"""
    for name in ("zh-Hans.json", "en.json"):
        assert key in _catalog(name), f"{name} 缺 key: {key}"


def test_no_hardcoded_cn_in_frontend() -> None:
    """app.js 里不该再有硬编码的中文文案。

    只查字符串字面量里的中文——注释里的中文是代码说明的一部分，
    留着（这套代码的注释本来就是中文的）。
    """
    import re

    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    # 去掉注释再看
    stripped = re.sub(r"/\*.*?\*/", "", app_js, flags=re.S)
    stripped = re.sub(r"//[^\n]*", "", stripped)
    offenders = re.findall(r'"[^"\n]*[一-鿿][^"\n]*"', stripped)
    assert not offenders, f"app.js 里还有硬编码中文: {offenders[:5]}"


# -- 前端结构 -------------------------------------------------------


def test_index_html_has_status_bar() -> None:
    """状态栏在 .main 下面（规格 §2.1）。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="statusBar"' in html, "index.html 里没有状态栏"
    assert 'id="statusSpeed"' in html
    assert 'id="statusActive"' in html
    assert 'id="statusLimit"' in html
    # 状态栏在主区里面、内容的后面
    main_at = html.index('<main class="main">')
    status_at = html.index('id="statusBar"')
    content_at = html.index('id="content"')
    assert main_at < content_at < status_at, "状态栏的位置不对"


ADD_DIALOG_FIELDS = (
    "addUrl",           # URL（多行，批量粘贴）
    "addSaveDir",       # 保存目录
    "addFilename",      # 文件名
    "addCategory",      # 分类
    "addProxy",         # 代理覆盖
    "addHeaders",       # 自定义请求头
    "addChunkCount",    # 分块数
    "addStartWhen",     # 开始时机
)


@pytest.mark.parametrize("field", ADD_DIALOG_FIELDS)
def test_add_dialog_has_field(field: str) -> None:
    """新建任务对话框有规格 §2.2 要求的那些字段。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert f'id="{field}"' in html, f"新建任务对话框缺字段 {field}"


def test_add_dialog_url_is_multiline_textarea() -> None:
    """URL 框是多行文本域——一行一个地址，支持批量粘贴。"""
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    at = html.index('id="addUrl"')
    tag = html[max(0, at - 200):at]
    assert "<textarea" in tag, "URL 框不是 textarea"


def test_add_dialog_has_probe_state_and_reprobe() -> None:
    """探测状态和「重新解析」按钮都在。

    **解析失败不是终态**——所以必须有个重新解析的入口，
    而且失败时对话框不能关。
    """
    html = (ASSETS / "index.html").read_text(encoding="utf-8")
    assert 'id="addProbeState"' in html, "没有探测状态元素"
    assert 'id="addReprobe"' in html, "没有重新解析按钮"
    assert 'id="addProbeError"' in html, "没有行内错误元素"

    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    # 探测失败时不能关窗
    assert "closeAddDialog" not in app_js.split("async function probeAddUrl")[1].split("function urlLines")[0], (
        "探测失败时关了对话框——失败必须是可恢复的"
    )


def test_frontend_theme_toggle_present() -> None:
    """主题切换：system → light → dark。"""
    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert "nextTheme" in app_js
    assert 'THEME_CYCLE = ["system", "light", "dark"]' in app_js
    # system 时删掉 data-theme 属性，让 prefers-color-scheme 说话
    assert 'delete document.documentElement.dataset.theme' in app_js


def test_style_has_both_dark_contexts() -> None:
    """CSS 里暗色板有两份：系统暗色 + 用户明确选暗色。

    少一份的话，用户在浅色系统里选「深色」就看不到变化。
    """
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    assert ':root[data-theme="dark"]' in css, "CSS 里没有显式暗色主题"
    assert "prefers-color-scheme: dark" in css, "CSS 里没有跟随系统的暗色"


def test_frontend_has_column_picker() -> None:
    """列选择器（规格 §2.1 的列显隐）。"""
    css = (ASSETS / "style.css").read_text(encoding="utf-8")
    app_js = (ASSETS / "app.js").read_text(encoding="utf-8")
    assert ".col-picker" in css, "CSS 里没有列选择器"
    assert "col-picker-btn" in app_js, "app.js 里没有列选择器按钮"
    assert "hidden_columns" in app_js, "app.js 没把列显隐存进设置"
