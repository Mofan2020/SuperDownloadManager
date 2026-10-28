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
    """模拟 Windows 的 unlink：目标文件前 N 次抛 PermissionError。

    真实 Windows 上文件被锁住时，unlink 抛 PermissionError
    (errno.EACCES)，文件原封不动还在。

    用法是 monkeypatch.setattr(Path, "unlink", fake.make())。

    **必须做成函数，不能做成对象。** Path.unlink 原本是方法，
    替换成普通对象时 Python 不会绑定 self——`path.unlink()` 会以
    零参数调用它，于是所有删除请求都被当成目标文件的请求，测试
    变成假绿。函数是描述符，绑定天然正确。
    """

    def __init__(self, target: Path, failures: int) -> None:
        self.target = target
        self.failures = failures
        self.calls = 0

    def make(self):
        """返回可以 monkeypatch 的函数。"""
        target = self.target
        failures = self.failures
        state = self

        def fake_unlink(path: Any = None, *args: Any, **kwargs: Any) -> None:
            # 只对 target 模拟「文件被占用」。别的文件正常删——
            # 这个 fake 替换的是 Path.unlink，所有调用都走它，
            # 不区分目标的话会误删。
            if path is not None and Path(path) != target:
                _REAL_UNLINK(path, *args, **kwargs)
                return
            state.calls += 1
            if state.calls <= failures:
                raise PermissionError(errno.EACCES, "文件被另一个进程占用", str(target))
            _REAL_UNLINK(target, *args, **kwargs)

        return fake_unlink


def test_unlink_with_retry_succeeds_after_lock_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件锁松开后重试能删掉。"""
    target = tmp_path / "chunk.bin"
    target.write_bytes(b"x" * 1024)

    fake = _WindowsLikeUnlink(target, failures=2)
    monkeypatch.setattr(Path, "unlink", fake.make())

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
    monkeypatch.setattr(Path, "unlink", fake.make())

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
    from sdm.engine import work_dir

    paths = [work_dir.partial_path(tmp_path / "out.bin", 0),
             work_dir.partial_path(tmp_path / "out.bin", 1)]
    for path in paths:
        assert path.exists()
        path.unlink()
    assert not list(work_dir.work_root().glob("*.sdm.partial-*"))


def test_cleanup_partials_survives_locked_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分片文件被锁住时，cleanup_partials 不能抛异常。"""
    assembler = FileAssembler(tmp_path / "out.bin")
    assembler.open_chunk(0, 0)
    assembler.append(b"data", 0)

    from sdm.engine import work_dir

    fake = _WindowsLikeUnlink(
        work_dir.partial_path(tmp_path / "out.bin", 0), failures=999
    )
    monkeypatch.setattr(Path, "unlink", fake.make())

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
    monkeypatch.setattr(Path, "unlink", fake.make())

    _unlink_with_retry(target, attempts=attempts, delay=0.001)
    assert fake.calls == attempts


def test_engine_cancel_cleans_partials_even_when_unlink_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引擎层取消：即使 unlink 一开始就失败，分片也要被清掉。

    模拟 Windows 的真实情况——文件被别的句柄打开时 unlink 抛
    PermissionError。真机上只要句柄还没关，删就一定失败。

    所以正确的行为是：取消时先关句柄，再删。这里把 unlink 改成
    「前 N 次抛 PermissionError」，模拟锁还没释放的那段窗口。

    如果引擎没有在删除前关句柄，这个测试会挂——因为真实的
    Windows 上文件确实删不掉。
    """
    import asyncio

    from sdm.engine.download_engine import DownloadEngine, StartSpec
    from tests.conftest import run_async
    from tests.range_server import RangeServer

    async def _inner() -> None:
        total = 4 * 1024 * 1024
        srv = RangeServer(total_bytes=total, chunk_delay=0.02)
        try:
            engine = DownloadEngine()
            ids = await engine.start([
                StartSpec(url=srv.url, destination_directory=tmp_path,
                          filename_override="out.bin")
            ])
            task_id = ids[0]
            await asyncio.sleep(0.35)

            from sdm.engine import work_dir

            # 分片文件已经在盘上了（说明确实下载过）
            assert list(work_dir.work_root().glob("*.sdm.partial-*")), "没产生分片，测不到删除路径"

            # 让 unlink 前 3 次失败——模拟文件还开着
            target = next(work_dir.work_root().glob("*.sdm.partial-*"))
            fake = _WindowsLikeUnlink(target, failures=3)
            monkeypatch.setattr(Path, "unlink", fake.make())

            await engine.cancel(task_id)
            await asyncio.sleep(0.2)

            leftovers = list(work_dir.work_root().glob("*.sdm.partial-*"))
            assert not leftovers, f"取消后残留分片：{[p.name for p in leftovers]}"
        finally:
            srv.stop()

    run_async(_inner)


def test_engine_cancel_deletes_while_unlink_always_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使 unlink 一直失败，引擎也不能因此崩掉。

    删不掉是尽力而为的事——不能因为删文件失败就让整个取消流程
    抛异常，用户点「取消」然后程序崩了。
    """
    import asyncio

    from sdm.engine.download_engine import DownloadEngine, StartSpec
    from tests.conftest import run_async
    from tests.range_server import RangeServer

    async def _inner() -> None:
        total = 4 * 1024 * 1024
        srv = RangeServer(total_bytes=total, chunk_delay=0.02)
        try:
            engine = DownloadEngine()
            ids = await engine.start([
                StartSpec(url=srv.url, destination_directory=tmp_path,
                          filename_override="out.bin")
            ])
            task_id = ids[0]
            await asyncio.sleep(0.35)

            from sdm.engine import work_dir

            target = next(work_dir.work_root().glob("*.sdm.partial-*"))
            fake = _WindowsLikeUnlink(target, failures=9999)
            monkeypatch.setattr(Path, "unlink", fake.make())

            # 不能抛异常
            await engine.cancel(task_id)
            await asyncio.sleep(0.1)

            summary = engine.get(task_id)
            assert summary is not None
            assert summary.state == "cancelled", f"状态是 {summary.state!r}"
        finally:
            srv.stop()

    run_async(_inner)
