"""文件名清洗的跨平台测试。

这些用例的判定标准是「Windows 上能不能创建这个文件」。
macOS 上几乎什么都能建，所以光在 mac 上测等于没测。
"""

from __future__ import annotations

import pytest

from sdm.engine.filenames import sanitize_filename, unique_path


class TestWindowsIllegalChars:
    """Windows 禁止的字符。"""

    @pytest.mark.parametrize("raw", [
        "a:b.zip",        # 冒号——NTFS 的流分隔符
        "a/b.zip",        # 正斜杠
        "a\\b.zip",       # 反斜杠
        "a?b.zip",        # 问号
        "a*b.zip",        # 星号
        'a"b.zip',        # 双引号
        "a<b.zip",        # 小于号
        "a>b.zip",        # 大于号
        "a|b.zip",        # 竖线
    ])
    def test_illegal_chars_replaced(self, raw: str) -> None:
        out = sanitize_filename(raw)
        for ch in '\\/:*?"<>|':
            assert ch not in out, f"{ch!r} 还在 {out!r} 里"

    @pytest.mark.parametrize("raw", [
        "a\x00b.zip",
        "a\x01b.zip",
        "a\x1fb.zip",
    ])
    def test_control_chars_replaced(self, raw: str) -> None:
        out = sanitize_filename(raw)
        assert not any(ord(c) < 32 for c in out), f"控制字符还在 {out!r} 里"


class TestWindowsReservedNames:
    """Windows 保留设备名。带不带扩展名都算保留。"""

    @pytest.mark.parametrize("raw", [
        "CON", "con", "CON.zip", "PRN", "AUX", "NUL",
        "COM1", "com9", "LPT1", "lpt9",
    ])
    def test_reserved_names_escaped(self, raw: str) -> None:
        out = sanitize_filename(raw)
        stem = out.split(".")[0].upper()
        assert stem not in {"CON", "PRN", "AUX", "NUL"} and not (
            len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3].isdigit()
        ), f"保留字没处理：{raw!r} -> {out!r}"


class TestWindowsTrailingJunk:
    """Windows 不允许文件名以点或空格结尾。"""

    @pytest.mark.parametrize("raw", [
        "file.zip.", "file.zip ", "file.zip.  ", "file.",
    ])
    def test_trailing_dot_space_stripped(self, raw: str) -> None:
        out = sanitize_filename(raw)
        assert not out.endswith("."), f"结尾还有点：{out!r}"
        assert not out.endswith(" "), f"结尾还有空格：{out!r}"


class TestLengthLimit:
    def test_long_name_truncated(self) -> None:
        out = sanitize_filename("a" * 500 + ".zip")
        assert len(out) <= 120, f"长度 {len(out)} 超过限制"

    def test_extension_preserved_on_truncate(self) -> None:
        out = sanitize_filename("a" * 500 + ".zip")
        assert out.endswith(".zip"), f"扩展名被切掉了：{out[-20:]!r}"

    def test_extensionless_name_truncated(self) -> None:
        out = sanitize_filename("a" * 500)
        assert len(out) <= 120


class TestEdgeCases:
    def test_empty_uses_fallback(self) -> None:
        assert sanitize_filename("", fallback="x.bin") == "x.bin"

    def test_only_illegal_chars_uses_fallback(self) -> None:
        assert sanitize_filename("///", fallback="x.bin") == "x.bin"

    def test_path_component_stripped(self) -> None:
        # 有的服务器会把完整路径塞进 Content-Disposition
        out = sanitize_filename("/var/www/uploads/file.zip")
        assert out == "file.zip"

    def test_unicode_preserved(self) -> None:
        # 中文文件名必须保留，不能被清成 download.bin
        out = sanitize_filename("中文文件名.zip")
        assert "中文文件名" in out

    def test_emoji_preserved(self) -> None:
        out = sanitize_filename("🎬movie.mkv")
        assert "movie" in out

    def test_fullwidth_normalized(self) -> None:
        # 全角括号 → 半角
        out = sanitize_filename("test（1）.zip")
        assert "（" not in out

    def test_spaces_compressed(self) -> None:
        out = sanitize_filename("a     b.zip")
        assert "     " not in out

    def test_realistic_github_name(self) -> None:
        # GitHub release 上带冒号和斜杠的名字很常见
        out = sanitize_filename("SuperDownloadManager-v2.0.1:beta/1.zip")
        for ch in '\\/:*?"<>|':
            assert ch not in out


class TestUniquePath:
    def test_returns_same_when_absent(self, tmp_path) -> None:
        p = tmp_path / "a.zip"
        assert unique_path(p) == p

    def test_appends_suffix_when_exists(self, tmp_path) -> None:
        p = tmp_path / "a.zip"
        p.write_bytes(b"x")
        out = unique_path(p)
        assert out != p
        assert out.name == "a-1.zip"

    def test_increments(self, tmp_path) -> None:
        (tmp_path / "a.zip").write_bytes(b"x")
        (tmp_path / "a-1.zip").write_bytes(b"x")
        out = unique_path(tmp_path / "a.zip")
        assert out.name == "a-2.zip"
