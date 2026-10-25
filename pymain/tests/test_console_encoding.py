"""Windows 控制台编码的回归测试。

Windows 的控制台默认用 locale 编码（简体中文系统是 GBK/cp936）。
程序里往 stdout/stderr 写中文或带非 ASCII 字符的路径时会抛
UnicodeEncodeError。

这个坑在 CI 的 Windows runner 上抓到过：GUI 自检输出
"配置目录: C:\\Users\\runneradmin\\..." 时直接崩。

这里用 GBK 编码的假流模拟 Windows 控制台，验证：
- force_utf8_stdio() 能把流切到 UTF-8
- safe_print() 在编不了的时候降级而不是崩
"""

from __future__ import annotations

import io
import sys

import pytest

from sdm.console import force_utf8_stdio, safe_print


class _GbkStream(io.TextIOWrapper):
    """编码是 GBK 的文本流，模拟 Windows 简体中文控制台。"""

    def __init__(self) -> None:
        raw = io.BytesIO()
        super().__init__(raw, encoding="gbk", errors="strict", newline="")

    @property
    def buffer_content(self) -> bytes:
        return self.buffer.getvalue()

    def getvalue(self) -> str:
        # reconfigure 之后流是 UTF-8 的，按 UTF-8 解
        return self.buffer.getvalue().decode("utf-8", errors="replace")


def test_force_utf8_stdio_reconfigures_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """force_utf8_stdio 要真的把流切成 UTF-8。"""
    out = _GbkStream()
    err = _GbkStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    force_utf8_stdio()

    assert out.encoding.lower().replace("-", "") == "utf8"
    assert err.encoding.lower().replace("-", "") == "utf8"


def test_utf8_output_survives_gbk_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """切到 UTF-8 之后，中文和 emoji 都能输出。"""
    out = _GbkStream()
    monkeypatch.setattr(sys, "stdout", out)

    force_utf8_stdio()

    # 「下载完成」是中文，「🐕‍🦺」是 emoji——GBK 都编不了
    safe_print("下载完成：SuperDownloadManager 🐕‍🦺")
    out.flush()

    assert "下载完成" in out.getvalue()


def test_safe_print_does_not_raise_on_unencodable() -> None:
    """编不了的时候降级，不抛异常。

    有些环境连 reconfigure 都救不了（老控制台、被重定向到奇怪
    编码的管道）。这时输出问号也比程序崩掉强。
    """

    class _BrokenStream(io.StringIO):
        encoding = "ascii"

        def write(self, s: str) -> int:
            # 模拟编不了就抛的流
            s.encode("ascii")
            return super().write(s)

    stream = _BrokenStream()
    # 不抛就算过
    safe_print("中文测试", file=stream)


def test_safe_print_handles_closed_stream() -> None:
    """流已关闭也不能抛。"""
    stream = io.StringIO()
    stream.close()
    safe_print("测试", file=stream)


def test_safe_print_to_real_stdout(capsys: pytest.CaptureFixture) -> None:
    """普通情况下内容原样输出。"""
    safe_print("普通输出")
    captured = capsys.readouterr()
    assert "普通输出" in captured.out
