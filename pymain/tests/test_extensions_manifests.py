"""浏览器扩展的静态检查。

扩展不像 Python 代码那样有测试跑——它跑在别人的浏览器里，装坏了
只有用户会发现。这里的检查把「装不上 / 装上了不干活」的那几类问题
挡在提交之前。

验收要点：
- 每个 `Extensions/*/manifest.json` 都是合法 JSON
- 都声明了 `nativeMessaging`（主通道）
- 清单里引用的文件**都存在**（路径写错浏览器会直接拒绝加载）
- Chrome / Edge 用 `service_worker`，Firefox 用 `scripts`
- 没有残留的坏模式 `url: safeName`
  （那把**文件名**当 URL 发出去，Python 侧必然拒绝）
- 每个后台脚本都有 `blob:` / `data:` 的放行判断
- 每个后台脚本都有 Alt 逃生舱
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

#: 仓库根目录。
REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSIONS_DIR = REPO_ROOT / "Extensions"

#: 要检查的扩展目录。
BROWSERS = ("Chrome", "Edge", "Firefox", "Safari")

#: 老的坏模式：把文件名当 URL 发出去。
#:
#: `safeName` 是**文件名**，不是下载地址。Python 侧只收 http(s) 的
#: URL，这个值过去一律被拒，扩展还顺手把浏览器的下载取消了——
#: 用户的文件直接没了。
BROKEN_URL_PATTERN = "url: safeName"


def _manifest_path(browser: str) -> Path:
    return EXTENSIONS_DIR / browser / "manifest.json"


def _background_path(browser: str) -> Path:
    return EXTENSIONS_DIR / browser / "background.js"


def _load_manifest(browser: str) -> dict:
    return json.loads(_manifest_path(browser).read_text(encoding="utf-8"))


def _referenced_files(manifest: dict) -> list[str]:
    """把清单里引用的文件路径都收集出来。"""
    refs: list[str] = []
    for value in (manifest.get("icons") or {}).values():
        refs.append(value)
    action = manifest.get("action") or manifest.get("browser_action") or {}
    for value in (action.get("default_icon") or {}).values():
        refs.append(value)
    if action.get("default_popup"):
        refs.append(action["default_popup"])
    background = manifest.get("background") or {}
    if background.get("service_worker"):
        refs.append(background["service_worker"])
    for script in background.get("scripts") or []:
        refs.append(script)
    options = manifest.get("options_ui") or {}
    if options.get("page"):
        refs.append(options["page"])
    for entry in manifest.get("content_scripts") or []:
        for script in entry.get("js") or []:
            refs.append(script)
        for sheet in entry.get("css") or []:
            refs.append(sheet)
    for entry in manifest.get("web_accessible_resources") or []:
        for resource in entry.get("resources") or []:
            refs.append(resource)
    return refs


# -- 清单本身 -------------------------------------------------------


@pytest.mark.parametrize("browser", BROWSERS)
def test_manifest_is_valid_json(browser: str) -> None:
    """清单必须是合法 JSON——语法错了浏览器直接不认。"""
    path = _manifest_path(browser)
    assert path.exists(), f"{browser} 扩展缺少 manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data.get("manifest_version") == 3


@pytest.mark.parametrize("browser", BROWSERS)
def test_manifest_declares_native_messaging(browser: str) -> None:
    """主通道是 Native Messaging（契约 §4.1），权限必须声明。"""
    manifest = _load_manifest(browser)
    assert "nativeMessaging" in manifest.get("permissions", [])


@pytest.mark.parametrize("browser", BROWSERS)
def test_manifest_has_name_version_description(browser: str) -> None:
    """商店和「关于」页面要显示这些。"""
    manifest = _load_manifest(browser)
    assert manifest.get("name")
    assert manifest.get("description")
    # 变量别叫 `version`——静态检查会把 `version.split` 当成引用
    # `sdm.version` 模块的属性。
    version_text = manifest.get("version")
    assert version_text and all(part.isdigit() for part in version_text.split("."))


def test_all_extensions_share_one_version() -> None:
    """四个扩展的版本号要一致——不然没法判断谁是最新的。"""
    versions = {browser: _load_manifest(browser)["version"] for browser in BROWSERS}
    assert len(set(versions.values())) == 1, versions


@pytest.mark.parametrize("browser", BROWSERS)
def test_manifest_referenced_files_exist(browser: str) -> None:
    """清单引用的每个文件都得在。

    路径写错的话浏览器的表现是「加载扩展失败」加一句含糊的
    错误信息，排查起来很费时间。
    """
    manifest = _load_manifest(browser)
    for ref in _referenced_files(manifest):
        path = EXTENSIONS_DIR / browser / ref
        assert path.exists(), f"{browser} 扩展引用了不存在的文件: {ref}"


@pytest.mark.parametrize("browser", ("Chrome", "Edge"))
def test_chromium_uses_service_worker(browser: str) -> None:
    """Chromium 系用 `background.service_worker`（契约 §4.4.4）。"""
    background = _load_manifest(browser).get("background") or {}
    assert background.get("service_worker") == "background.js"
    assert "scripts" not in background


def test_firefox_uses_background_scripts() -> None:
    """Firefox 用 `background.scripts`——Firefox 的 MV3 还没有 service worker。"""
    background = _load_manifest(browser="Firefox").get("background") or {}
    assert background.get("scripts") == ["background.js"]
    assert "service_worker" not in background


def test_safari_uses_background_scripts() -> None:
    """Safari 走事件页（`scripts` + 非持久）。"""
    background = _load_manifest(browser="Safari").get("background") or {}
    assert background.get("scripts") == ["background.js"]
    assert background.get("persistent") is False


@pytest.mark.parametrize("browser", BROWSERS)
def test_icons_exist_and_are_png(browser: str) -> None:
    """图标得是真的 PNG 文件，不是空占位。"""
    icons = _load_manifest(browser).get("icons") or {}
    assert icons
    for size, ref in icons.items():
        path = EXTENSIONS_DIR / browser / ref
        assert path.exists()
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", f"{browser} 的 {size} 图标不是 PNG"


# -- 后台脚本的行为 -------------------------------------------------


@pytest.mark.parametrize("browser", BROWSERS)
def test_background_script_has_no_broken_url_pattern(browser: str) -> None:
    """不能再有 `url: safeName`。

    那把文件名当 URL 发出去，Python 侧必然拒绝，扩展还顺手把
    浏览器的下载取消了——用户的文件直接没了。
    """
    text = _background_path(browser).read_text(encoding="utf-8")
    assert BROKEN_URL_PATTERN not in text


@pytest.mark.parametrize("browser", BROWSERS)
def test_background_script_filters_blob_and_data(browser: str) -> None:
    """每个扩展都要放过 `blob:` / `data:`。

    这两种协议浏览器自己能下，下载器接不了。**不能取消浏览器的
    下载**——取消了用户的文件就没了。
    """
    text = _background_path(browser).read_text(encoding="utf-8")
    assert "blob:" in text
    assert "data:" in text


@pytest.mark.parametrize("browser", BROWSERS)
def test_background_script_has_alt_escape_hatch(browser: str) -> None:
    """每个扩展都要有 Alt（macOS 是 Option）逃生舱。"""
    text = _background_path(browser).read_text(encoding="utf-8")
    assert "altKey" in text


@pytest.mark.parametrize("browser", BROWSERS)
def test_background_script_references_native_app_id(browser: str) -> None:
    """原生消息的应用 ID 要跟宿主清单里的 `name` 一致。"""
    text = _background_path(browser).read_text(encoding="utf-8")
    assert "com.skyc8266.superdownloadmanager" in text


@pytest.mark.parametrize("browser", ("Chrome", "Edge", "Firefox"))
def test_background_script_checks_before_cancelling(browser: str) -> None:
    """取消浏览器的下载只能发生在「确认要接管」之后。

    协议判断和 Alt 逃生舱必须排在 `cancel: true` 前面——顺序反了
    就会把 `blob:` 下载也取消掉，用户的文件直接没了。
    """
    text = _background_path(browser).read_text(encoding="utf-8")
    guard_scheme = "if (isUnsupportedScheme(url)) return undefined;"
    guard_alt = "if (isEscapeHatch(url, info)) return undefined;"
    cancel = "return { cancel: true };"
    assert guard_scheme in text
    assert guard_alt in text
    assert cancel in text
    assert text.index(guard_scheme) < text.index(guard_alt) < text.index(cancel)


def test_safari_never_cancels_a_download() -> None:
    """Safari 一律不碰浏览器的下载队列。"""
    text = _background_path("Safari").read_text(encoding="utf-8")
    assert "cancel: true" not in text


def test_safari_does_not_auto_capture_downloads() -> None:
    """Safari **不做自动接管**——它的 API 不允许。

    硬做的话只能等下载开始后再取消，那样会丢文件。契约 §4.4.5
    要求这条如实标注。
    """
    text = _background_path("Safari").read_text(encoding="utf-8")
    assert ".onDeterminingFilename.addListener" not in text


def test_safari_has_context_menu_and_toolbar_button() -> None:
    """Safari 的手动派单要有右键菜单**和**工具栏按钮两条路。"""
    background = _background_path("Safari").read_text(encoding="utf-8")
    assert "contextMenus" in background
    manifest = _load_manifest("Safari")
    action = manifest.get("action") or manifest.get("browser_action") or {}
    assert action.get("default_popup")
    assert (EXTENSIONS_DIR / "Safari" / action["default_popup"]).exists()


def test_safari_documents_the_limitation() -> None:
    """Safari 的限制要写在扩展的说明里（契约 §4.4.5：标注试验性）。"""
    manifest = _load_manifest("Safari")
    assert "试验性" in manifest["description"]
    assert "Safari" in manifest["description"]


# -- 宿主清单模板 ---------------------------------------------------


def test_chromium_manifests_pin_the_extension_key() -> None:
    """Chrome / Edge 的 manifest 必须带 `key` 字段。

    **本地加载的扩展，ID 是由 `key` 算出来的**（没有 `key` 就按
    目录路径算哈希）。ID 变了，宿主清单里的 `allowed_origins` 就对
    不上，Chrome 直接拒绝拉起原生宿主——症状只是「扩展说应用没
    响应」，很难往清单上想。
    """
    for browser in ("Chrome", "Edge"):
        data = _load_manifest(browser)
        assert data.get("key"), f"{browser} 的 manifest 缺 key 字段，扩展 ID 会飘"


def test_extension_id_matches_the_manifest_key() -> None:
    """manifest 里的公钥算出来的扩展 ID 必须跟代码里写死的一致。

    算法是 Chromium 的 `GenerateId`：公钥 DER 字节做 SHA256，
    取前 32 个十六进制位，每个位 `0`-`f` 映射成 `a`-`p`。

    两边对不上 = Chrome 拒绝启动宿主 = 原生消息通道静默失效。
    """
    import base64
    import hashlib

    from sdm.ipc.native_host import CHROMIUM_EXTENSION_ID

    for browser in ("Chrome", "Edge"):
        key = _load_manifest(browser)["key"]
        der = base64.b64decode(key)
        digest = hashlib.sha256(der).hexdigest()
        computed = "".join(chr(ord("a") + int(c, 16)) for c in digest[:32])
        assert computed == CHROMIUM_EXTENSION_ID, (
            f"{browser} 的 manifest key 算出的扩展 ID 是 {computed}，"
            f"代码里写的是 {CHROMIUM_EXTENSION_ID}"
        )


def test_host_manifest_template_lists_the_real_extension_id() -> None:
    """仓库里的宿主清单模板要写真 ID，不能留占位符。"""
    from sdm.ipc.native_host import CHROMIUM_EXTENSION_ID

    path = EXTENSIONS_DIR / "native-messaging-host" / "com.skyc8266.superdownloadmanager.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert f"chrome-extension://{CHROMIUM_EXTENSION_ID}/" in data["allowed_origins"]
    assert not any("REPLACE_WITH_EXTENSION_ID" in o for o in data["allowed_origins"])


def test_native_messaging_host_template_is_valid() -> None:
    """仓库里的宿主清单模板要跟 `host_manifest()` 生成的形状一致。"""
    path = EXTENSIONS_DIR / "native-messaging-host" / "com.skyc8266.superdownloadmanager.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("name", "description", "path", "type", "allowed_origins"):
        assert key in data
    assert data["name"] == "com.skyc8266.superdownloadmanager"
    assert data["type"] == "stdio"


def test_launcher_scripts_exist() -> None:
    """启动脚本模板要在——清单里的 `path` 指向它。"""
    host_dir = EXTENSIONS_DIR / "native-messaging-host"
    assert (host_dir / "sdmd-native-host.sh").exists()
    assert (host_dir / "sdmd-native-host.cmd").exists()
