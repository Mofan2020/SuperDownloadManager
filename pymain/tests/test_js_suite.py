"""把 Node 侧的 JS 测试拉进 pytest。

- `tests/test_sort_js.mjs` 测的是 `app.js` 里 F076 的排序实现。
- `tests/test_settings_scroll_js.mjs` 测的是设置页重画时保持滚动位置。
- `tests/test_connecting_js.mjs` 测的是「连接中」时不能显示 0 KB/s。

app.js 是浏览器脚本，pytest 跑不了，所以用 Node 跑独立的
.mjs 脚本，这里只负责调用 + 转发输出。

Node 不在就跳过——不因为缺个运行时把整个测试套挂掉。

列对齐那套（`test_column_alignment_js.mjs`）要真的浏览器，
单独放在 `test_ui_column_alignment.py` 里，缺浏览器就跳过。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MJS_SUITES = [
    HERE / "test_sort_js.mjs",
    HERE / "test_settings_scroll_js.mjs",
    HERE / "test_connecting_js.mjs",
]

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="没装 node，跳过 JS 侧测试")


@pytest.mark.parametrize("mjs", MJS_SUITES, ids=lambda p: p.stem)
def test_js_suite_passes(mjs: Path):
    """跑完整的 JS 测试套。

    失败时把 Node 的输出带进 pytest 报告——不然只看到
    "exit code 1"，看不到哪个用例坏了。
    """
    result = subprocess.run(
        [node, str(mjs)],
        capture_output=True,
        text=True,
        # Windows 控制台默认 GBK，解码 UTF-8 输出会 UnicodeDecodeError。
        # 显式指定编码，别让平台默认值决定。
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(HERE.parent),
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    assert result.returncode == 0, (
        f"JS 测试失败（{mjs.name}）：\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )
    # 顺带确认用例数，别让套件空跑也绿。
    assert "个用例通过" in stdout, stdout
