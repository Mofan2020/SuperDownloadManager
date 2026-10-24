"""Windows 特有行为的回归测试。

macOS 上这些路径根本不会触发——POSIX 允许删掉正在被写的文件，
Windows 不行。CI 上 Windows runner 能真跑一遍，但 macOS 上也得
能测，不然本地改完不知道有没有搞坏 Windows。

做法是把 Windows 的失败模式模拟出来：

- ``unlink`` 抛 PermissionError（Windows 上文件被别的句柄打开）
- 删完文件还在（Windows 上 unlink 失败就是文件还在）

真机上行为一致，测试就能在 macOS 上守住这条路径。
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import pytest

from sdm.engine.file_assembler import FileAssembler, _unlink_with_retry

#: 原始的 Path.unlink。monkeypatch 之后还得能调到真的那个，
#: 否则 fake 里再调 Path.unlink 就是自己，递归到爆栈。
_REAL_UNLINK = Path.unlink


class _WindowsLikeUnlink:
    """模拟 Windows 的 unlink：前 N 次抛 PermissionError。

    真实 Windows 上文件被锁住时，unlink 抛 PermissionError
    (errno.EACCES)，文件原封不动还在。
    """

    def __init__(self, target: Path, failures: int) -> None:
        self.target = target
        self.failures = failures
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError(errno.EACCES, "文件被另一个进程占用", str(self.target))
        _REAL_UNLINK(self.target, *args, **kwargs)


def test_unlink_with_retry_succeeds_after_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件锁松开后重试能删掉。"""
    target = tmp_path / "chunk.bin"
    target.write_bytes(b"x" * 1024)

    fake = _WindowsLikeUnlink(target, failures=2)
    monkeypatch.setattr(Path, "unlink", fake)

    assert _unlink_with_retry(target, attempts=10, delay=0.001) is True
    assert fake.calls == 3
    assert not target.exists()


def test_unlink_with_retry_gives_up_persistently_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一直锁着就放弃，但不能让调用方炸。"""
    target = tmp_path / "chunk.bin"
    target.write_bytes(b"x")

    fake = _WindowsLikeUnlink(target, failures=999)
    monkeypatch.setattr(Path, "unlink", fake)

    assert _unlink_with_retry(target, attempts=3, delay=0.001) is False
    assert fake.calls == 3
    assert target.exists()


def test_unlink_with_retry_missing_file_is_success(tmp_path: Path) -> None:
    """文件本来就不在，不算失败。"""
    assert _unlink_with_retry(tmp_path / "nope.bin", attempts=3, delay=0.001) is True


def test_close_all_releases_handles(tmp_path: Path) -> None:
    """close_all 关掉句柄，文件才能删。"""
    assembler = FileAssembler(tmp_path / "out.bin")
    assembler.open_chunk(0, 0)
    assembler.open_chunk(1, 100)
    assembler.append(b"hello", 0)
    assembler.append(b"world", 1)

    assembler.close_all()

    # 句柄关了之后文件可以正常删掉
    for path in (tmp_path / "out.bin.sdm.partial-0", tmp_path / "out.bin.sdm.partial-1"):
        assert path.exists()
        path.unlink()
    assert not list(tmp_path.glob("*.sdm.partial-*"))


def test_cleanup_partials_survives_locked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分片文件被锁住时，cleanup_partials 不能抛异常。"""
    assembler = FileAssembler(tmp_path / "out.bin")
    assembler.open_chunk(0, 0)
    assembler.append(b"data", 0)

    fake = _WindowsLikeUnlink(tmp_path / "out.bin.sdm.partial-0", failures=999)
    monkeypatch.setattr(Path, "unlink", fake)

    # 不抛就算过——清理是尽力而为，不能因为删不掉就崩掉取消流程
    assembler.cleanup_partials()
    assert fake.calls >= 1


@pytest.mark.parametrize("attempts", [1, 3, 20])
def test_retry_budget_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, attempts: int
) -> None:
    """重试次数上限就是给的次数，不多试。"""
    target = tmp_path / "chunk.bin"
    target.write_bytes(b"x")
    fake = _WindowsLikeUnlink(target, failures=999)
    monkeypatch.setattr(Path, "unlink", fake)

    _unlink_with_retry(target, attempts=attempts, delay=0.001)
    assert fake.calls == attempts
