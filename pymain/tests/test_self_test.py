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
        timeout=120,
    )


def test_self_test_passes():
    """自检全过。"""
    result = _run_self_test()
    assert result.returncode == 0, f"自检失败：\n{result.stdout}\n{result.stderr}"


def test_self_test_covers_new_modules():
    """自检里必须有这几项。

    这些模块是后加的，PyInstaller 静态分析认不认得它们，只有
    真打包才知道。自检里 import 一次，进包了就说明没问题。

    少一项 = 少一层保护，所以这里把名字钉死。
    """
    result = _run_self_test()
    output = result.stdout

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
