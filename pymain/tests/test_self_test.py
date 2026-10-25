"""打包自检的测试。

`--self-test` 是 CI 和打包流程的守门人——「打包出来才发现起不来」
这件事由它挡住。

所以自检本身也得被测：改了自检逻辑、或者哪个模块 import 挂了，
这里要红。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SNIPPET = (
    "import sys; sys.path.insert(0,'.'); "
    "from sdm.gui.main import run_self_test; sys.exit(run_self_test())"
)


def _run_self_test() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        # Windows 控制台默认 GBK，解码 UTF-8 输出会 UnicodeDecodeError。
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def test_self_test_passes():
    """自检全过。"""
    result = _run_self_test()
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    assert result.returncode == 0, f"自检失败：\n{stdout}\n{stderr}"


def test_self_test_covers_new_modules():
    """自检里必须有这几项。

    这些模块是后加的，PyInstaller 静态分析认不认得它们，只有
    真打包才知道。自检里 import 一次，进包了就说明没问题。

    少一项 = 少一层保护，所以这里把名字钉死。
    """
    result = _run_self_test()
    output = result.stdout or ""

    for name in (
        "通知通道",
        "URL scheme",
        "Dock 策略",
        "分类解析",
        "文案键一致",
    ):
        assert name in output, f"自检少了「{name}」这项：\n{output}"


def test_self_test_fails_when_a_check_fails(monkeypatch, capsys):
    """有检查失败时，退出码必须非零。

    这条最要紧——CI 靠退出码判断。自检要是永远返回 0，
    它就是个摆设，「打包出来才发现起不来」照样会发生。
    """
    # 让「分类解析」这项必然失败：把 bridge 里的解析函数
    # 换成一个恒返回 None 的假货。
    import sdm.gui.bridge as bridge_mod
    from sdm.gui import main as gui_main

    monkeypatch.setattr(bridge_mod, "_resolve_category", lambda name: None)

    code = gui_main.run_self_test()
    captured = capsys.readouterr()

    assert code != 0, "检查失败了但退出码还是 0，自检形同虚设"
    assert "分类解析" in captured.out
    assert "FAIL" in captured.out


def test_self_test_output_marks_failures(monkeypatch, capsys):
    """失败项在输出里标成 FAIL，不是 OK。"""
    import sdm.gui.bridge as bridge_mod
    from sdm.gui import main as gui_main

    monkeypatch.setattr(bridge_mod, "_resolve_category", lambda name: None)

    gui_main.run_self_test()
    captured = capsys.readouterr()

    for line in captured.out.splitlines():
        if "分类解析" in line:
            assert "FAIL" in line, f"失败项没标 FAIL：{line}"
            break
    else:
        pytest.fail("输出里没有「分类解析」这行")


def test_key_consistency_check_detects_missing_key(monkeypatch, capsys):
    """中英 key 不一致时，「文案键一致」这项必须报 FAIL。

    **这条不能省。** 中英 key 集合本来就一致，所以「检查恒真」
    和「检查真的在查」在正常数据下结果一样——只有造出不一致
    的数据才能区分。

    做法：往英文表里塞一个中文表没有的 key，模拟「改了中文
    忘了改英文」这个真实场景。
    """
    from sdm.gui import main as gui_main
    from sdm.i18n import CATALOGS, _load_catalogs

    _load_catalogs()
    CATALOGS["en"]["_test_only_key_zzz"] = "临时"
    try:
        gui_main.run_self_test()
    finally:
        CATALOGS["en"].pop("_test_only_key_zzz", None)

    captured = capsys.readouterr()

    for line in captured.out.splitlines():
        if "文案键一致" in line:
            assert "FAIL" in line, f"key 不一致却没报 FAIL：{line}"
            assert "_test_only_key_zzz" in line, f"没报出缺哪个 key：{line}"
            break
    else:
        pytest.fail("输出里没有「文案键一致」这行")


def test_applescript_dict_check_is_skipped_off_macos(monkeypatch):
    """非 macOS 上不查词典（Windows CI 抓到的）。

    `.sdef` 只存在于 macOS 的 .app 里（`Contents/Resources/`），
    Windows 包里没有它、也不需要它——Apple Events 和 Script Editor
    都是 macOS 的东西。自检在 Windows 上查它必然红。

    旧写法是把这一项直接写在 `run_self_test` 里，读 `sys.platform`
    自己决定——那样没法测：源码跑的时候词典就在模块旁边，
    模拟不出「Windows 冻结包里没有词典」那个情形。
    """
    from sdm.gui.applescript import applescript_handler
    from sdm.gui.main import _check_applescript_dict

    # **把词典路径指到一个不存在的地方。** 这样「查了」和「跳过了」
    # 两种实现的结果才不一样——否则在源码树上词典就在模块旁边，
    # 查不查都过，测试等于没写。
    monkeypatch.setattr(
        applescript_handler, "sdef_path", lambda: Path("/不存在的/SuperDownloadManager.sdef")
    )

    results: list[tuple[str, bool, str]] = []
    _check_applescript_dict(
        lambda name, ok, detail="": results.append((name, ok, detail)),
        is_macos=False,
    )
    assert results, "检查项没产出结果"
    name, ok, detail = results[0]
    assert name == "AppleScript 字典"
    assert ok is True, f"非 macOS 不该判失败：{detail}"
    assert "非 macOS" in detail, f"非 macOS 上这一项应该被跳过，实际是：{detail}"


def test_applescript_dict_check_fails_when_missing_on_macos(monkeypatch):
    """macOS 上词典不在必须判失败。

    **这条不能松**：词典没进包的话，Script Editor 里一个命令都看不到，
    而应用启动时不会报错——只有这一条检查挡得住。
    """
    from sdm.gui.applescript import applescript_handler
    from sdm.gui.main import _check_applescript_dict

    monkeypatch.setattr(
        applescript_handler, "sdef_path", lambda: Path("/不存在的/SuperDownloadManager.sdef")
    )

    results: list[tuple[str, bool, str]] = []
    _check_applescript_dict(
        lambda name, ok, detail="": results.append((name, ok, detail)),
        is_macos=True,
    )
    assert results
    name, ok, _detail = results[0]
    assert name == "AppleScript 字典"
    assert ok is False, "macOS 上词典缺失必须判失败"


def test_self_test_passes_on_non_macos_platform():
    """把平台当成非 macOS 再跑一遍自检，它照样全过。"""
    snippet = (
        "import sys; sys.path.insert(0,'.'); "
        "from sdm.gui.main import run_self_test; "
        "sys.platform='win32'; "
        "sys.exit(run_self_test())"
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    assert "FAIL" not in stdout, f"模拟 Windows 时自检不该有失败项：\n{stdout}"
    assert result.returncode == 0, f"自检退出码 {result.returncode}：\n{stdout}\n{stderr}"
