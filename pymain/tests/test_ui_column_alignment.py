"""下载列表的列对齐（无头浏览器量像素）。

**这个 bug 比想象的大。**

下载列表的每一行和表头用的是同一套 grid 模板（`--row-cols`，
name 列是 `1fr`）。模板变量是 JS 写在容器 `.list-wrap` 上的，
值是「var(--w-name) var(--w-category) …」，而 `--w-*` 只定义在
`.row` / `.sort-header` 上。

CSS 自定义属性里的 `var()` 是在**声明它的那个元素**上做替换的——
写在 `.list-wrap` 上时，替换时 `.list-wrap` 上根本没有 `--w-name`，
整个 `--row-cols` 变成「无效值」，`grid-template-columns` 退回
`none`，**所有列竖着叠起来**。Chromium 和 WebKit（app 实际用的
内核）都实测过：一行 281px 高，八个格子竖着排。

这个测试用真的浏览器量像素——CSS 断言看不出「整张表塌了」这种
问题，量出来的坐标才是证据。

要 node + playwright（还要装了 Chromium）。缺了就跳过——CI 上
没有浏览器，不能因为这个把整个测试套挂掉。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
MJS = HERE / "test_column_alignment_js.mjs"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="没装 node，跳过列对齐测试")


def test_download_list_columns_are_aligned() -> None:
    """表头和数据行必须是同一张表。"""
    result = subprocess.run(
        [node, str(MJS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        cwd=str(HERE.parent),
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if stdout.startswith("SKIP") or "SKIP:" in stdout:
        pytest.skip(stdout.strip())
    assert result.returncode == 0, (
        f"列对齐测试失败：\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "个用例通过" in stdout, stdout
