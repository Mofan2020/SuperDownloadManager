"""回归测试：修过的真 bug 不能再犯。

这一批测的都是实际出过问题的路径——不是假设出来的边界情况，
是代码里真的坏过、只是没人调到的地方。

- ResumeState.read() 之前写成 @staticmethod 却用了 cls。
  过期链接换新链接续传要走这条路径，一调就 NameError。
- copy_to_clipboard() 里 else 分支多了一次 `import subprocess`，
  局部变量把模块名遮住了，整个函数里的 subprocess 都变成局部变量，
  一调就 NameError。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdm.engine.resume_state import ResumeState


def test_resume_state_read_roundtrip(tmp_path: Path) -> None:
    """写进去再读出来，字段要一致。"""
    state = ResumeState(
        url="https://example.com/file.bin",
        content_length=123456,
        filename="file.bin",
    )
    final_path = tmp_path / "file.bin"
    state.write(final_path)

    # sidecar 现在住在工作目录里，不在下载目录里。
    sidecar = ResumeState.sidecar_path(final_path)
    assert sidecar.exists()
    loaded = ResumeState.read(sidecar)
    assert loaded.url == state.url
    assert loaded.content_length == state.content_length
    assert loaded.filename == state.filename


def test_resume_state_read_is_not_broken_by_staticmethod(tmp_path: Path) -> None:
    """read() 必须能真的构造出对象。

    @staticmethod + cls 的写法，调用时会 NameError: undefined name 'cls'。
    这条测试直接盯住这个：调 read() 不许抛 NameError。
    """
    sidecar = tmp_path / "x.sdm.json"
    sidecar.write_text(
        json.dumps({
            "url": "https://example.com/a.bin",
            "content_length": 42,
            "filename": "a.bin",
        }),
        encoding="utf-8",
    )

    try:
        loaded = ResumeState.read(sidecar)
    except NameError as err:  # pragma: no cover - 这条就是要抓它
        pytest.fail(f"ResumeState.read() 抛 NameError：{err}")

    assert isinstance(loaded, ResumeState)
    assert loaded.content_length == 42


def test_copy_to_clipboard_does_not_shadow_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """剪贴板复制不能因为局部 import 遮蔽模块名而炸掉。

    else 分支（Linux）里多写了一次 `import subprocess`，Python 会把
    subprocess 认成整个函数里的局部变量，于是 macOS/Windows 分支里
    用到的 subprocess 也会报 NameError。
    """
    from sdm.gui import bridge

    calls: list[tuple[str, ...]] = []

    class _FakeProc:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        return _FakeProc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.paths, "is_macos", lambda: True)
    monkeypatch.setattr(bridge.paths, "is_windows", lambda: False)

    result = bridge.GuiBridge.__new__(bridge.GuiBridge)  # 只调方法，不需要完整初始化
    try:
        out = bridge.GuiBridge.copy_to_clipboard(result, "hello")
    except NameError as err:  # pragma: no cover - 这条就是要抓它
        pytest.fail(f"copy_to_clipboard() 抛 NameError：{err}")

    assert out == {"ok": True}
    assert calls and calls[0][0] == "pbcopy"


def test_copy_to_clipboard_linux_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux 分支也不能因为那次多余的 import 而炸。"""
    from sdm.gui import bridge

    calls: list[tuple[str, ...]] = []

    class _FakeProc:
        returncode = 0

    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        return _FakeProc()

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.paths, "is_macos", lambda: False)
    monkeypatch.setattr(bridge.paths, "is_windows", lambda: False)

    result = bridge.GuiBridge.__new__(bridge.GuiBridge)
    out = bridge.GuiBridge.copy_to_clipboard(result, "hello")
    assert out == {"ok": True}
    assert calls and calls[0][0] == "xclip"
