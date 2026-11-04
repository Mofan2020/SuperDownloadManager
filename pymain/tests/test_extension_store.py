"""扩展持久化安装的测试。

这一段决定用户能不能真的把扩展装上：

- **定位**：包内那份在哪儿（打包版 / 源码运行）必须能找对。macOS 的
  `.app` 里 `_MEIPASS` 指向 `Contents/Frameworks`，扩展其实在
  `Contents/Resources/extensions`——认死 `_MEIPASS` 的话功能静默失效。
- **持久化**：装到用户目录的那份必须**活得比应用久**（Windows 的
  onefile 把资源解到临时目录，退出就没了）。
- **指纹**：SHA-256 指纹相同才算同一份；内容变了指纹必须变，否则
  「永远是最新版本」就是假的。
- **幂等**：重复装不能反复覆盖（用户正在浏览器里加载这个目录，
  反复换内容会让浏览器的扩展状态乱掉）。

验收要点：
- 同一目录两次算指纹结果相同
- 文件内容变了指纹就变
- `ensure_persisted` 第二次 `updated=False`
- 包内换了版本后 `updated=True` 且持久目录内容变成新的
- 包内没有扩展时返回 `ok=False`，**不抛异常**
- 持久目录落在 `config_dir()` 底下（用 `SDM_CONFIG_DIR` 隔离，
  不写用户真实目录）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.ipc import extension_store as es

# -- 测试夹具 -------------------------------------------------------


@pytest.fixture()
def bundle(tmp_path: Path, monkeypatch) -> Path:
    """造一个「包内」扩展目录，冻结运行时的 `_MEIPASS` 指过去。

    `sys.frozen` + `sys._MEIPASS` 是打包版的真实形状——候选探测
    的第一优先级就是它。
    """
    meipass = tmp_path / "_MEI12345"
    chrome = meipass / "extensions" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "manifest.json").write_text('{"name": "Super Download Manager"}', encoding="utf-8")
    (chrome / "background.js").write_text("// v1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    return chrome


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch) -> Path:
    """把持久目录指到临时目录，不碰用户真实数据。"""
    target = tmp_path / "config"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SDM_CONFIG_DIR", str(target))
    return target


def _write_version(chrome: Path, version: str) -> None:
    """改掉包内扩展的内容——模拟发了新版本。"""
    (chrome / "background.js").write_text(f"// {version}\n", encoding="utf-8")


# -- 指纹 -----------------------------------------------------------


def test_fingerprint_is_stable_for_the_same_directory(tmp_path: Path) -> None:
    """同一目录算两次，指纹必须一样。

    不一样的话「是不是最新版本」就没法判断了——每次打开设置页都会
    提示要更新，用户会疯。
    """
    directory = tmp_path / "ext"
    directory.mkdir()
    (directory / "manifest.json").write_text("{}", encoding="utf-8")
    (directory / "sub").mkdir()
    (directory / "sub" / "a.js").write_text("a", encoding="utf-8")

    assert es.fingerprint_dir(directory) == es.fingerprint_dir(directory)


def test_fingerprint_changes_when_file_content_changes(tmp_path: Path) -> None:
    """内容变了，指纹必须跟着变。"""
    directory = tmp_path / "ext"
    directory.mkdir()
    target = directory / "manifest.json"
    target.write_text('{"version": "1"}', encoding="utf-8")

    before = es.fingerprint_dir(directory)
    target.write_text('{"version": "2"}', encoding="utf-8")
    after = es.fingerprint_dir(directory)

    assert before != after


def test_fingerprint_changes_when_file_is_renamed(tmp_path: Path) -> None:
    """**光比内容不行**——两个文件换个名字就是另一份扩展了。

    指纹里必须带上相对路径，否则改名之后指纹不变，会以为还是
    同一份。
    """
    directory = tmp_path / "ext"
    directory.mkdir()
    (directory / "background.js").write_text("same", encoding="utf-8")
    before = es.fingerprint_dir(directory)

    (directory / "background.js").rename(directory / "background2.js")
    assert es.fingerprint_dir(directory) != before


def test_empty_directory_has_a_fixed_fingerprint(tmp_path: Path) -> None:
    """空目录也得有个稳定的值，不能每次都不一样。"""
    directory = tmp_path / "empty"
    directory.mkdir()
    first = es.fingerprint_dir(directory)
    assert first
    assert first == es.fingerprint_dir(directory)


def test_unreadable_file_is_skipped_not_raised(tmp_path: Path) -> None:
    """读不出来的文件只能跳过，不能把整个体检搞崩。"""
    directory = tmp_path / "ext"
    directory.mkdir()
    (directory / "ok.txt").write_text("ok", encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise PermissionError("nope")

    original = Path.read_bytes
    Path.read_bytes = _boom  # type: ignore[method-assign]
    try:
        value = es.fingerprint_dir(directory)
    finally:
        Path.read_bytes = original  # type: ignore[method-assign]
    assert isinstance(value, str) and value


# -- 位置 -----------------------------------------------------------


def test_persistent_dir_lives_under_the_config_dir(config_dir: Path) -> None:
    """持久目录在 `config_dir()/extensions/<浏览器>` 底下。

    **不能落在应用包旁边**——那儿在 Windows 上是临时目录，进程退出
    就被删掉，用户加载的扩展会凭空消失。
    """
    target = es.persistent_dir("chrome")
    assert target is not None
    assert target == config_dir / "extensions" / "Chrome"
    assert target.parent == config_dir / "extensions"


def test_persistent_dir_uses_the_same_dirnames_as_the_repository(config_dir: Path) -> None:
    """目录名跟 `Extensions/` 下的一致（Chrome / Edge / Firefox / Safari）。"""
    for browser_id, dirname in es.BROWSER_DIRNAMES.items():
        target = es.persistent_dir(browser_id)
        assert target is not None
        assert target.name == dirname


def test_persistent_dir_is_none_for_an_unknown_browser(config_dir: Path) -> None:
    assert es.persistent_dir("netscape") is None


def test_bundle_dir_finds_the_meipass_copy(bundle: Path) -> None:
    """Windows onefile：资源解到 `_MEIPASS`，扩展就在它底下。"""
    assert es.bundle_dir("chrome") == bundle


def test_bundle_dir_finds_the_macos_app_bundle_copy(tmp_path: Path, monkeypatch) -> None:
    """macOS `.app`：扩展在 `Contents/Resources/extensions`。

    **`_MEIPASS` 在 macOS onedir 下指向 `Contents/Frameworks`**——
    那儿没有 extensions。认死 `_MEIPASS` 的话，整个扩展功能在
    macOS 上静默失效。
    """
    app = tmp_path / "SuperDownloadManager.app"
    resources = app / "Contents" / "Resources"
    chrome = resources / "extensions" / "Chrome"
    chrome.mkdir(parents=True)
    (chrome / "manifest.json").write_text("{}", encoding="utf-8")
    exe = app / "Contents" / "MacOS" / "SuperDownloadManager"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(app / "Contents" / "Frameworks"), raising=False)

    assert es.bundle_dir("chrome") == chrome


def test_bundle_dir_falls_back_to_the_repository_when_running_from_source(monkeypatch) -> None:
    """源码运行时扩展在仓库根的 `Extensions/<浏览器>`。"""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)

    target = es.bundle_dir("chrome")
    assert target is not None
    assert target.name == "Chrome"
    assert (target / "manifest.json").is_file()


def test_bundle_dir_is_none_when_nothing_is_there(tmp_path: Path, monkeypatch) -> None:
    """找不到就是 None——不能瞎编一个路径。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "app"))
    assert es.bundle_dir("chrome") is None
    assert es.bundle_fingerprint("chrome") is None


# -- 安装 -----------------------------------------------------------


def test_ensure_persisted_copies_the_bundle(bundle: Path, config_dir: Path) -> None:
    """第一次装：把包内那份复制到持久目录。"""
    result = es.ensure_persisted("chrome")
    assert result["ok"] is True
    assert result["updated"] is True
    assert result["path"] == str(config_dir / "extensions" / "Chrome")
    assert (config_dir / "extensions" / "Chrome" / "manifest.json").is_file()
    assert result["fingerprint"]


def test_ensure_persisted_is_idempotent(bundle: Path, config_dir: Path) -> None:
    """第二次装：什么都不做，`updated=False`。

    反复覆盖会让浏览器里正在加载的扩展目录不停变——扩展状态会被
    浏览器重置，用户得重新点一次「加载」。
    """
    first = es.ensure_persisted("chrome")
    assert first["updated"] is True

    second = es.ensure_persisted("chrome")
    assert second["ok"] is True
    assert second["updated"] is False
    assert second["fingerprint"] == first["fingerprint"]


def test_ensure_persisted_updates_when_the_bundle_changed(bundle: Path, config_dir: Path) -> None:
    """包内发了新版本 → 持久目录要换成新的。"""
    es.ensure_persisted("chrome")
    old_fingerprint = es.persisted_fingerprint("chrome")

    _write_version(bundle, "v2")
    result = es.ensure_persisted("chrome")

    assert result["ok"] is True
    assert result["updated"] is True
    assert result["fingerprint"] != old_fingerprint
    assert es.persisted_fingerprint("chrome") == es.bundle_fingerprint("chrome")
    installed = config_dir / "extensions" / "Chrome" / "background.js"
    assert installed.read_text(encoding="utf-8") == "// v2\n"


def test_ensure_persisted_reports_failure_instead_of_raising(
    tmp_path: Path, monkeypatch, config_dir: Path
) -> None:
    """包内没有这份扩展时返回 `ok=False`，**不抛异常**。

    设置页要照常显示其它浏览器的状态——一个浏览器没有扩展不该
    把整个体检搞崩。
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "app"))

    result = es.ensure_persisted("chrome")
    assert result["ok"] is False
    assert result["updated"] is False
    assert result["message"]
    assert not (config_dir / "extensions" / "Chrome").exists()


def test_ensure_persisted_rejects_an_unknown_browser(config_dir: Path) -> None:
    result = es.ensure_persisted("netscape")
    assert result["ok"] is False
    assert result["message"]


def test_persisted_fingerprint_is_none_before_installing(bundle: Path, config_dir: Path) -> None:
    assert es.persisted_fingerprint("chrome") is None


def test_persisted_and_bundle_fingerprints_match_after_install(
    bundle: Path, config_dir: Path
) -> None:
    """装完之后两边指纹必须一致——否则没法说「已经是最新版本」。"""
    es.ensure_persisted("chrome")
    assert es.persisted_fingerprint("chrome") == es.bundle_fingerprint("chrome")
