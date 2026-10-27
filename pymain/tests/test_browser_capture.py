"""浏览器接管的体检逻辑测试。

这一块的功能是「**告诉用户该干什么**」：扩展在哪、装没装、
配对 token 是多少。判断依据全在用户机器上（扫扩展目录、查
pluginkit），逻辑一错，用户照着提示做就会失败。

验收要点：
- 三个浏览器的静态信息对：Safari 必须手动配对、不能自动接管
- 「装没装」靠 manifest 里的**名字**认，不是靠扩展 ID
  （Chrome 的扩展 ID 每个开发者都不一样，靠 ID 认必然认错）
- `.xpi`（Firefox 装好的包）是 zip，manifest 在里面
- 重置 token 之后**旧 token 立刻作废**——不然「重新配对」是假的
- 设置页调得到状态；Safari 没有扩展管理页时老实说没有
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.ipc import browser_capture as bc
from sdm.ipc import native_host as nh

# -- 浏览器清单 -----------------------------------------------------


def test_three_browsers_are_supported() -> None:
    """Chrome / Firefox / Safari 三个都在。"""
    ids = [spec.id for spec in bc.BROWSERS]
    assert ids == ["chrome", "firefox", "safari"]


def test_safari_needs_manual_pairing_and_cannot_auto_capture() -> None:
    """Safari 两条限制都得如实标出来。

    - 手动配对：Safari 没有 Chrome 那种原生消息宿主，只能粘 token。
    - 不能自动接管：Safari 没有「下载开始前拦一道」的钩子，
      硬做只能事后取消，用户会丢文件。
    """
    safari = bc.browser_spec("safari")
    assert safari is not None
    assert safari.needs_manual_pairing is True
    assert safari.auto_capture is False


@pytest.mark.parametrize("browser_id", ("chrome", "firefox"))
def test_chromium_and_firefox_pair_automatically(browser_id: str) -> None:
    """装了原生消息宿主就能自动配对，不需要手动粘 token。"""
    spec = bc.browser_spec(browser_id)
    assert spec is not None
    assert spec.needs_manual_pairing is False
    assert spec.auto_capture is True
    assert spec.extensions_url, "得有扩展管理页给用户点"


def test_safari_has_no_extensions_page() -> None:
    """Safari 没有 Chrome 式的扩展管理页。

    给一个不存在的 URL 只会让用户看到一个空白页。
    """
    assert bc.browser_spec("safari").extensions_url == ""


def test_unknown_browser_id_returns_none() -> None:
    assert bc.browser_spec("netscape") is None
    assert bc.extension_dir("netscape") is None


# -- 扩展目录 -------------------------------------------------------


def test_extension_root_points_at_the_repository_extensions_folder() -> None:
    """源码运行时扩展目录是仓库根的 `Extensions/`。"""
    root = bc.extension_root()
    assert root is not None
    assert root.name == "Extensions"
    assert (root / "Chrome" / "manifest.json").is_file()


@pytest.mark.parametrize("browser_id", ("chrome", "firefox", "safari"))
def test_extension_dir_exists_for_every_browser(browser_id: str) -> None:
    directory = bc.extension_dir(browser_id)
    assert directory is not None
    assert (directory / "manifest.json").is_file()


def test_extension_root_resolves_inside_a_mac_app_bundle(tmp_path: Path, monkeypatch) -> None:
    """打包版（macOS）：扩展在 `.app` 的 `Contents/Resources/extensions`。

    `sys.executable` 在 .app 里指向 `Contents/MacOS/` 下的二进制，
    Resources 在它**上两级**——找错一级就整个功能静默失效。
    """
    app = tmp_path / "SuperDownloadManager.app"
    resources = app / "Contents" / "Resources"
    (resources / "extensions" / "Chrome").mkdir(parents=True)
    (resources / "extensions" / "Chrome" / "manifest.json").write_text("{}", encoding="utf-8")
    exe = app / "Contents" / "MacOS" / "SuperDownloadManager"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    assert bc.extension_root() == resources / "extensions"
    assert bc.extension_dir("chrome") == resources / "extensions" / "Chrome"


def test_extension_root_resolves_in_windows_onefile(tmp_path: Path, monkeypatch) -> None:
    """打包版（Windows onefile）：资源解到 `sys._MEIPASS`。

    onefile 每次启动把资源解到临时目录，`sys.executable` 旁边
    什么都没有——只能认 `_MEIPASS`。
    """
    meipass = tmp_path / "_MEI12345"
    (meipass / "extensions" / "Firefox").mkdir(parents=True)
    (meipass / "extensions" / "Firefox" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert bc.extension_root() == meipass / "extensions"
    assert bc.extension_dir("firefox") == meipass / "extensions" / "Firefox"


# -- 装没装 ---------------------------------------------------------


def _make_chromium_profile(root: Path, manifest: dict) -> Path:
    """造一个 Chromium 系浏览器的 profile 目录。

    结构是 `<profile>/Extensions/<扩展 id>/<版本>/manifest.json`。
    """
    target = root / "Default" / "Extensions" / "abcdefghijklmnop" / "1.0.0"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_detects_installed_extension_by_manifest_name(tmp_path: Path, monkeypatch) -> None:
    """靠 manifest 里的名字认扩展。

    **不能靠扩展 ID**——Chrome 的扩展 ID 由开发者账号或清单里的
    `key` 决定，每个打包版本都可能不一样。靠名字认才稳。
    """
    profile = _make_chromium_profile(
        tmp_path, {"name": bc.EXTENSION_NAME, "manifest_version": 3}
    )
    monkeypatch.setattr(bc, "_profile_roots", lambda _browser_id: [profile])
    assert bc.is_extension_installed("chrome") is True


def test_ignores_other_extensions(tmp_path: Path, monkeypatch) -> None:
    """别人装的扩展不能被认成我们的。"""
    profile = _make_chromium_profile(
        tmp_path, {"name": "Some Other Extension", "manifest_version": 3}
    )
    monkeypatch.setattr(bc, "_profile_roots", lambda _browser_id: [profile])
    assert bc.is_extension_installed("chrome") is False


def test_firefox_xpi_is_read_as_a_zip(tmp_path: Path, monkeypatch) -> None:
    """Firefox 装好的扩展是 `.xpi`（zip），manifest 在里面。"""
    xpi = tmp_path / "sdm@example.xpi"
    with zipfile.ZipFile(xpi, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"name": bc.EXTENSION_NAME}))
    assert bc._manifest_name(xpi) == bc.EXTENSION_NAME


def test_broken_manifest_does_not_raise(tmp_path: Path) -> None:
    """manifest 坏了只能当「没装」，不能把整个体检搞崩。"""
    broken = tmp_path / "manifest.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    assert bc._manifest_name(broken) == ""


# -- 原生消息宿主 ---------------------------------------------------


def test_native_host_installed_lists_browsers(tmp_path: Path, monkeypatch) -> None:
    """装了宿主清单的浏览器才会自动配对。"""
    chrome_dir = tmp_path / "chrome"
    chrome_dir.mkdir()
    (chrome_dir / f"{nh.APP_ID}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        nh, "host_manifest_dirs", lambda: [("chrome", chrome_dir), ("firefox", tmp_path / "ff")]
    )
    assert bc.native_host_installed() == ["chrome"]


# -- 汇总 -----------------------------------------------------------


def test_capture_info_shape(monkeypatch) -> None:
    """设置页拿到的结构要稳定。"""
    monkeypatch.setattr(bc, "native_host_installed", lambda: ["chrome"])
    info = bc.capture_info()
    assert info["ok"] is True
    assert info["token"], "配对 token 不能是空的"
    assert info["extension_root"]
    hosts = info["host_browsers"]
    assert hosts == ["chrome"]

    browsers = info["browsers"]
    assert [b["id"] for b in browsers] == ["chrome", "firefox", "safari"]
    for entry in browsers:
        for key in (
            "id",
            "label",
            "extensions_url",
            "needs_manual_pairing",
            "auto_capture",
            "extension_dir",
            "available",
            "installed",
            "native_host",
        ):
            assert key in entry, f"{entry.get('id')} 少了字段 {key}"
        assert entry["available"] is True
        assert entry["extension_dir"], "扩展目录路径要给用户看"

    safari = next(b for b in browsers if b["id"] == "safari")
    assert safari["needs_manual_pairing"] is True
    assert safari["auto_capture"] is False
    assert safari["native_host"] is False, "Safari 用不到原生消息宿主"


# -- 配对 token -----------------------------------------------------


def test_reset_pair_token_invalidates_the_old_one() -> None:
    """重置之后旧 token 必须立刻作废。

    `load_pair_token()` 是「设置文件优先」——只写一边的话，
    下一次读回来的还是旧 token，界面上的「重新配对」就是假的。
    """
    old = nh.ensure_pair_token()
    new = nh.reset_pair_token()
    assert new != old
    assert nh.load_pair_token() == new
    assert nh.verify_pair_token(new) is True
    assert nh.verify_pair_token(old) is False


def test_reset_pair_token_writes_both_places() -> None:
    """设置文件和 token 存档都要写。"""
    new = nh.reset_pair_token()
    assert nh.token_path().read_text(encoding="utf-8").strip() == new
    settings = json.loads(nh.paths.settings_path().read_text(encoding="utf-8"))
    assert settings[nh.PAIR_TOKEN_KEY] == new


# -- GUI 桥接层 -----------------------------------------------------


@pytest.fixture()
def bridge():
    from sdm.gui.bridge import GuiBridge

    return GuiBridge()


def test_bridge_exposes_capture_info(bridge) -> None:
    info = bridge.get_browser_capture_info()
    assert info["ok"] is True
    assert info["token"]
    assert len(info["browsers"]) == 3


def test_bridge_reset_pair_token(bridge) -> None:
    res = bridge.reset_pair_token()
    assert res["ok"] is True
    assert res["token"]


def test_bridge_refuses_to_open_a_missing_extension_folder(bridge) -> None:
    """不存在的浏览器不能去开目录——那会打开一个奇怪的路径。"""
    res = bridge.open_browser_extension_folder("netscape")
    assert res["ok"] is False


def test_bridge_reports_safari_has_no_extensions_page(bridge) -> None:
    res = bridge.open_browser_extensions_page("safari")
    assert res["ok"] is False
    assert res["error"]


def test_bridge_opens_the_extension_folder(bridge, monkeypatch, tmp_path) -> None:
    """真的去打开目录——但只是把路径交给系统，不自己 popen。"""
    opened: list[str] = []
    monkeypatch.setattr(
        "sdm.gui.bridge.sys_open_file", lambda path: opened.append(str(path)) or True
    )
    res = bridge.open_browser_extension_folder("chrome")
    assert res["ok"] is True
    assert res["path"].endswith("Chrome")
    assert opened and opened[0].endswith("Chrome")


def test_frontend_and_bridge_agree_on_the_method_names() -> None:
    """前端调的方法名，桥接层必须真的定义了这个方法。

    名字对不上时 pywebview 不会报错——它只在**调用时**抛一个
    「没有这个属性」，用户看到的是「点复制没反应」。
    """
    from sdm.gui.bridge import GuiBridge

    app_js = (ROOT / "sdm" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
    for name in (
        "get_browser_capture_info",
        "reset_pair_token",
        "open_browser_extension_folder",
        "open_browser_extensions_page",
    ):
        assert f"api()?.{name}(" in app_js, f"前端没调 {name}"
        assert hasattr(GuiBridge, name), f"桥接层没有 {name}"


def test_settings_section_is_rendered() -> None:
    """设置页得真的有这个板块——不然用户找不到入口。"""
    app_js = (ROOT / "sdm" / "gui" / "assets" / "app.js").read_text(encoding="utf-8")
    assert "renderBrowserCaptureSection()" in app_js
    assert "settingsBrowserCapture" in app_js
