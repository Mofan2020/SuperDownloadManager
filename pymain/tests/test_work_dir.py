"""辅助文件工作目录的测试。

**要盯住的核心事实：下载目录里只剩用户要的那一个文件。**

    ~/Downloads/movie.mkv          ← 用户要的
    ~/Downloads/movie.mkv.sdm.json ← 不该在这
    ~/Downloads/movie.mkv.sdm.partial-0  ← 不该在这
    ~/Downloads/movie.mkv.sdm.session.json ← 不该在这

这些内部文件统一住在：

    ~/Library/Application Support/SuperDownloadManager/downloads/

（Windows 上是 %LOCALAPPDATA%\\SuperDownloadManager\\downloads\\）
"""

from __future__ import annotations

import json
from pathlib import Path

from sdm.engine import work_dir
from sdm.engine.file_assembler import FileAssembler
from sdm.engine.resume_state import ResumeState
from sdm.engine.session_state import SessionState


class TestLocation:
    def test_all_auxiliary_files_live_in_the_work_dir(self, tmp_path: Path) -> None:
        """三个辅助文件都得在工作目录里。"""
        dest = tmp_path / "movie.mkv"
        sidecar = work_dir.sidecar_path(dest)
        session = work_dir.session_path(dest)
        partial = work_dir.partial_path(dest, 3)

        root = work_dir.work_root()
        for path in (sidecar, session, partial):
            assert path.parent == root, f"{path.name} 不在工作目录里"
        # 都不在下载目录里。
        for path in (sidecar, session, partial):
            assert path.parent != dest.parent, f"{path.name} 跑回下载目录了"

    def test_work_dir_is_under_application_support(self, monkeypatch) -> None:
        """工作目录必须在 SDM 自己的地盘里，不能是下载目录。

        **绕开 `SDM_WORK_DIR` 覆盖看真实结构**——测试里为了隔离给每个
        测试单独的工作目录，所以这里得先把覆盖摘掉。
        """
        monkeypatch.delenv("SDM_WORK_DIR", raising=False)
        root = work_dir.work_root()
        assert root.name == "downloads"
        # 挂在配置目录底下——那是 SDM 专属的应用支持目录。
        from sdm.paths import config_dir, download_dir

        assert root.parent == config_dir()
        # 不能是下载目录本身。
        assert root != download_dir()

    def test_different_destinations_get_different_keys(self, tmp_path: Path) -> None:
        """同名文件放在不同目录，辅助文件不能撞车。

        **sidecar 是用来判断「还是不是同一个文件」的**——比对
        etag / content_length。两个不同来源的同名文件共用一个
        sidecar，会导致把新内容续写进旧文件的空洞里。
        """
        a = tmp_path / "a" / "movie.mkv"
        b = tmp_path / "b" / "movie.mkv"
        assert work_dir.task_key(a) != work_dir.task_key(b)
        assert work_dir.sidecar_path(a) != work_dir.sidecar_path(b)

    def test_same_destination_gets_the_same_key(self, tmp_path: Path) -> None:
        """同一个目标文件，算出来的 key 必须稳定。"""
        dest = tmp_path / "movie.mkv"
        assert work_dir.task_key(dest) == work_dir.task_key(dest)

    def test_key_survives_a_missing_target_file(self, tmp_path: Path) -> None:
        """目标文件还不存在时也能算 key。

        下载开始前目标文件还不存在——算路径的时候不能依赖它存在。
        """
        dest = tmp_path / "not-created-yet.mkv"
        assert work_dir.task_key(dest)


class TestLegacyMigration:
    """升级前下的东西还在老位置，不能让用户的续传凭空消失。"""

    def test_legacy_sidecar_is_adopted(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        legacy = work_dir.legacy_sidecar_path(dest)
        legacy.write_text(json.dumps({"url": "https://x/y"}), encoding="utf-8")

        found = work_dir.adopt_legacy_sidecar(dest)
        assert found is not None
        assert found == work_dir.sidecar_path(dest)
        assert found.exists()
        assert not legacy.exists(), "老文件没搬走"

    def test_legacy_session_is_adopted(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        legacy = work_dir.legacy_session_path(dest)
        legacy.write_text("{}", encoding="utf-8")

        found = work_dir.adopt_legacy_session(dest)
        assert found is not None
        assert found == work_dir.session_path(dest)
        assert not legacy.exists()

    def test_legacy_partial_is_adopted(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        legacy = work_dir.legacy_partial_path(dest, 2)
        legacy.write_bytes(b"payload")

        found = work_dir.adopt_legacy_partial(dest, 2)
        assert found == work_dir.partial_path(dest, 2)
        assert found.read_bytes() == b"payload"
        assert not legacy.exists()

    def test_new_location_wins_over_legacy(self, tmp_path: Path) -> None:
        """两处都有时用新的——正在进行的下载用的就是新位置。"""
        dest = tmp_path / "movie.mkv"
        legacy = work_dir.legacy_sidecar_path(dest)
        legacy.write_text("legacy", encoding="utf-8")
        new = work_dir.sidecar_path(dest)
        new.write_text("new", encoding="utf-8")

        found = work_dir.adopt_legacy_sidecar(dest)
        assert found == new
        assert found.read_text(encoding="utf-8") == "new"

    def test_legacy_partials_can_be_cleaned_up(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        for i in range(3):
            work_dir.legacy_partial_path(dest, i).write_bytes(b"x")
        work_dir.remove_legacy_partials(dest)
        assert not work_dir.legacy_partial_paths(dest)


class TestAssemblerWritesToWorkDir:
    def test_partial_files_are_not_in_the_download_dir(self, tmp_path: Path) -> None:
        """分片文件必须写到工作目录里去。"""
        dest = tmp_path / "movie.mkv"
        assembler = FileAssembler(dest)
        assembler.open_chunk(0, 0)
        assembler.append(b"hello", 0)

        assert not list(tmp_path.glob("*.sdm.*")), "下载目录被分片文件污染了"
        partial = work_dir.partial_path(dest, 0)
        assert partial.exists()
        assert partial.read_bytes() == b"hello"

    def test_finalize_merges_from_the_work_dir(self, tmp_path: Path) -> None:
        """合并要从工作目录读分片，结果写回下载目录。"""
        dest = tmp_path / "movie.mkv"
        assembler = FileAssembler(dest)
        assembler.open_chunk(0, 0)
        assembler.open_chunk(1, 4)
        assembler.append(b"abcd", 0)
        assembler.append(b"efgh", 1)
        assembler.finalize([0, 1])

        assert dest.read_bytes() == b"abcdefgh"
        assert not list(tmp_path.glob("*.sdm.*")), "下载目录有残留"
        # 分片文件用完就删。
        assert not work_dir.partial_path(dest, 0).exists()
        assert not work_dir.partial_path(dest, 1).exists()


class TestSessionAndSidecarPaths:
    def test_session_state_uses_the_work_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        state = SessionState(url="https://x/y", chunks=[])
        path = SessionState.path_for(dest)
        assert path.parent == work_dir.work_root()

        state.write(dest)
        assert path.exists()
        assert not list(tmp_path.glob("*.sdm.*"))

        SessionState.remove(dest)
        assert not path.exists()

    def test_resume_state_uses_the_work_dir(self, tmp_path: Path) -> None:
        dest = tmp_path / "movie.mkv"
        state = ResumeState(url="https://x/y", filename="movie.mkv")
        state.write(dest)

        sidecar = ResumeState.sidecar_path(dest)
        assert sidecar.exists()
        assert sidecar.parent == work_dir.work_root()
        assert not list(tmp_path.glob("*.sdm.*"))
