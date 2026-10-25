"""把 Node 侧的 JS 测试拉进 pytest。

`tests/test_sort_js.mjs` 测的是 `app.js` 里 F076 的排序实现。
app.js 是浏览器脚本，pytest 跑不了，所以用 Node 跑独立的
.mjs 脚本，这里只负责调用 + 转发输出。

Node 不在就跳过——不因为缺个运行时把整个测试套挂掉。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MJS = HERE / "test_sort_js.mjs"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="没装 node，跳过 JS 侧测试")


def test_sort_js_suite_passes():
    """跑完整的 JS 排序测试套。

    失败时把 Node 的输出带进 pytest 报告——不然只看到
    "exit code 1"，看不到哪个用例坏了。
    """
    result = subprocess.run(
        [node, str(MJS)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(HERE.parent),
    )
    assert result.returncode == 0, (
        "JS 排序测试失败：\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    # 顺带确认用例数，别让套件空跑也绿。
    assert "个用例通过" in result.stdout, result.stdout
