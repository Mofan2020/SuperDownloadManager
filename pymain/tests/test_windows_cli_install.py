"""Windows 上「自动装好命令行」这件事，从入口函数开始验。

和 `test_windows_cli.py` 的分工：那边验 shim 的内容和计划任务的幂等，
这里验**它到底会不会被执行**、升级会不会真的换掉旧的那一份、PATH 加得对
不对。

起因是个只靠读代码看不出来的洞：`ensure_installed()` 先调
`bundle_resources()`，而那个函数只认得 macOS `.app` 的 `Contents/Resources`。
Windows 的 onefile 里没有这么个目录，于是它返回 `None`，函数直接 return——
`_ensure_windows()` 从来没被执行过。CLI、PATH、daemon 计划任务、原生消息
宿主一个都没装上，日志里也没有一句错误。原来的用例全都直接调
`_ensure_windows()`，正好绕开了这层。
"""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

import pytest

from sdm.gui import bootstrap

# ------------------------------------------------------------------ 假注册表


class FakeWinreg:
    """够用的 winreg 替身：只实现 PATH 那一小节要用的东西。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    REG_EXPAND_SZ = 2
    REG_SZ = 1

    def __init__(self, path_value: str = "", *, persist: bool = True) -> None:
        self.values: dict[str, tuple[str, int]] = {"Path": (path_value, self.REG_EXPAND_SZ)}
        self.writes: list[tuple[str, int, str]] = []
        self.persist = persist

    def OpenKey(self, root, name, reserved, access):
        return ("key", root, name)

    def CloseKey(self, key) -> None:
        return None

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise OSError("value not found")
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value) -> None:
        self.writes.append((name, kind, value))
        if self.persist:
            self.values[name] = (value, kind)


@pytest.fixture
def fake_winreg(monkeypatch: pytest.MonkeyPatch) -> FakeWinreg:
    module = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", module)
    # 广播要 user32，macOS 上没有——单独关掉，免得每次都走异常分支。
    monkeypatch.setattr(bootstrap, "_broadcast_environment_change", lambda: True)
    return module


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: True)
    monkeypatch.setattr(bootstrap, "_is_macos", lambda: False)


# ------------------------------------------------ 1. 入口函数真的会走 Windows


def test_the_windows_installer_is_actually_reached(monkeypatch: pytest.MonkeyPatch, as_windows) -> None:
    """**这条是这次修复的核心。** 以前 Windows 分支是死代码。"""
    called: list[bool] = []
    monkeypatch.setattr(bootstrap, "_ensure_windows", lambda: called.append(True) or {"platform": "win32"})
    # Windows 打包版里就是这个样子：没有 .app，拿不到 Resources。
    monkeypatch.setattr(bootstrap, "bundle_resources", lambda: None)

    result = bootstrap.ensure_installed()

    assert called == [True], "Windows 上必须走到 _ensure_windows()"
    assert result["platform"] == "win32"


def test_macos_without_a_bundle_still_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """反过来也要成立：源码运行时不该往 /usr/local/bin 里塞东西。"""
    called: list[bool] = []
    monkeypatch.setattr(bootstrap, "_is_windows", lambda: False)
    monkeypatch.setattr(bootstrap, "_is_macos", lambda: True)
    monkeypatch.setattr(bootstrap, "_ensure_windows", lambda: called.append(True) or {})
    monkeypatch.setattr(bootstrap, "bundle_resources", lambda: None)

    result = bootstrap.ensure_installed()

    assert called == []
    assert result["cli"]["reason"] == "not_bundle"


# ------------------------------------------------------ 2. 升级要真的换一份


def _make_zip(path: Path, name: str, content: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)


def test_a_rebuilt_zip_replaces_what_was_extracted_before(tmp_path: Path) -> None:
    """标记文件里存指纹，不是一句 ``ok``。

    以前只写 ``ok``：升级之后解出来的还是上一版，GUI 是新的、``sdm`` 命令
    和后台 daemon 是旧的，而且谁都看不出来。
    """
    archive = tmp_path / "sdm-cli.zip"
    dest = tmp_path / "runtime" / "sdm"
    _make_zip(archive, "sdm.exe", "version-one")

    assert bootstrap._extract_zip_once(archive, dest) is True
    assert (dest / "sdm.exe").read_text(encoding="utf-8") == "version-one"

    # 同一个文件再来一次：不重复解包。
    assert bootstrap._extract_zip_once(archive, dest) is True

    # 换一份（大小和 mtime 都变了）：必须重新解，而且旧文件不能留下。
    (dest / "leftover.txt").write_text("old", encoding="utf-8")
    _make_zip(archive, "sdm.exe", "version-two-with-more-bytes")
    stamp = time.time() + 30
    import os

    os.utime(archive, (stamp, stamp))

    assert bootstrap._extract_zip_once(archive, dest) is True
    assert (dest / "sdm.exe").read_text(encoding="utf-8") == "version-two-with-more-bytes"
    assert not (dest / "leftover.txt").exists(), "旧目录要被整个换掉"


def test_a_missing_zip_is_not_a_success(tmp_path: Path) -> None:
    assert bootstrap._extract_zip_once(tmp_path / "nope.zip", tmp_path / "out") is False


def test_the_old_daemon_is_stopped_only_when_something_is_replaced(tmp_path: Path) -> None:
    """正在跑的 ``sdmd.exe`` 是锁住的，升级前必须先停。

    但**只在真的要重新解的时候停**——每次开 GUI 都停一下正在下载的 daemon
    是另一种毛病。
    """
    archive = tmp_path / "sdmd-daemon.zip"
    dest = tmp_path / "runtime" / "sdmd"
    _make_zip(archive, "sdmd.exe", "one")
    stops: list[str] = []

    bootstrap._extract_zip_once(archive, dest, stop=lambda: stops.append("stop"))
    assert stops == ["stop"], "要解包，先停"

    bootstrap._extract_zip_once(archive, dest, stop=lambda: stops.append("stop"))
    assert stops == ["stop"], "没变化就不该打扰正在跑的 daemon"


# ------------------------------------------------------------- 3. 用户 PATH


def test_a_missing_directory_is_appended_as_an_expandable_string(fake_winreg: FakeWinreg) -> None:
    """必须写 REG_EXPAND_SZ：老 PATH 里的 %USERPROFILE% 不能变成字面量。"""
    fake_winreg.values["Path"] = (r"%USERPROFILE%\bin;%SystemRoot%", FakeWinreg.REG_EXPAND_SZ)

    result = bootstrap._add_to_user_path(Path(r"C:\Users\someone\AppData\Local\SuperDownloadManager\bin"))

    assert result == {"ok": True, "reason": "installed", "in_path": True}
    name, kind, value = fake_winreg.writes[-1]
    assert name == "Path"
    assert kind == FakeWinreg.REG_EXPAND_SZ
    assert "%USERPROFILE%" in value and "%SystemRoot%" in value, "原有变量必须留着"
    assert value.endswith(r"C:\Users\someone\AppData\Local\SuperDownloadManager\bin")


def test_the_same_directory_is_not_added_twice(fake_winreg: FakeWinreg) -> None:
    """尾部分隔符、大小写、正斜杠——注册表里全都不敢保证一致。"""
    # 正斜杠、大小写不同，而且末尾多一个分隔符——三种写法都得认得出来。
    fake_winreg.values["Path"] = (
        r"C:/Users/Someone/AppData/Local/SuperDownloadManager/bin\;C:\Windows",
        FakeWinreg.REG_EXPAND_SZ,
    )

    result = bootstrap._add_to_user_path(
        Path(r"C:\Users\someone\AppData\Local\SuperDownloadManager\bin"))

    assert result["reason"] == "already"
    assert fake_winreg.writes == [], "已经在里面了就不该再写一次"


def test_a_write_that_does_not_stick_is_reported_as_a_failure(fake_winreg: FakeWinreg) -> None:
    """SetValueEx 不保证值真的落下去了，所以写完要读回来核对。"""
    fake_winreg.persist = False

    result = bootstrap._add_to_user_path(Path(r"C:\SDM\bin"))

    assert result["ok"] is False
    assert result["reason"] == "verify_failed"


def test_path_normalisation_covers_the_awkward_spellings() -> None:
    assert bootstrap._normalize_path_entry(r'C:\Tools\bin\ ') == r"c:\tools\bin"
    assert bootstrap._normalize_path_entry('"C:\\Tools\\bin"') == r"c:\tools\bin"
    assert bootstrap._normalize_path_entry("C:/Tools/bin") == r"c:\tools\bin"


# ------------------------------------------------ 4. 装完之后告诉系统一声


def test_a_successful_add_tells_explorer_about_the_new_path(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """不广播的话，新开的终端也看不到 PATH——用户看到的是「不是内部或外部
    命令」，跟没装一模一样。"""
    module = FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", module)
    broadcasts: list[bool] = []
    monkeypatch.setattr(bootstrap, "_broadcast_environment_change",
                        lambda: broadcasts.append(True) or True)

    bootstrap._add_to_user_path(Path(r"C:\SDM\bin"))

    assert broadcasts == [True]


def test_an_already_present_directory_does_not_broadcast(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """什么都没改就别惊动系统——每次启动都广播一遍是白折腾。"""
    module = FakeWinreg(path_value=r"C:\SDM\bin")
    monkeypatch.setitem(sys.modules, "winreg", module)
    broadcasts: list[bool] = []
    monkeypatch.setattr(bootstrap, "_broadcast_environment_change",
                        lambda: broadcasts.append(True) or True)

    bootstrap._add_to_user_path(Path(r"C:\SDM\bin"))

    assert broadcasts == []
