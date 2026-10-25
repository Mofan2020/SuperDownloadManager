"""跨平台属性引用的静态检查。

`paths.SOCKET_PORT_WINDOWS` 这类引用，编译期查不出来——只有真正
走到那行代码才会 AttributeError。macOS 走 Unix socket，压根不碰
这个常量，所以本机测试全绿，CI 的 Windows runner 才炸。

这类「引用了别的模块的属性，但写错了模块名」的错，靠人检查不可靠。
这里静态扫一遍：所有 `模块.属性` 形式的引用，验证目标模块真的有
那个属性。

新增平台专属常量时，这个测试会挡住「忘了同步到另一个模块」。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 要检查的「点号引用」目标：属性名 -> 模块路径
CHECKED_MODULES = {
    "paths": "sdm.paths",
    "version": "sdm.version",
    "logging_setup": "sdm.logging_setup",
    "categories": "sdm.engine.categories",
    "range_planner": "sdm.engine.range_planner",
}


def _source_files() -> list[Path]:
    files = []
    for path in (ROOT / "sdm").rglob("*.py"):
        files.append(path)
    for path in (ROOT / "tests").rglob("*.py"):
        files.append(path)
    return files


def _attr_refs(tree: ast.AST) -> list[tuple[str, str, int]]:
    """找出所有 `名字.属性` 形式的引用。

    只看 Name 作为 value 的 Attribute——也就是 `paths.foo` 这种
    明确指向模块的。`self.foo`、`obj.bar` 这类不看（对象类型
    静态不可知）。
    """
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            found.append((node.value.id, node.attr, node.lineno))
    return found


@pytest.mark.parametrize("module_name", sorted(CHECKED_MODULES))
def test_module_attributes_exist(module_name: str) -> None:
    """所有 `module.attr` 引用里的 attr，模块上必须真的有。"""
    module = importlib.import_module(CHECKED_MODULES[module_name])

    problems: list[str] = []
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as err:  # pragma: no cover
            problems.append(f"{path.name}:{err.lineno} 语法错误 {err}")
            continue
        for ref_module, attr, lineno in _attr_refs(tree):
            if ref_module != module_name:
                continue
            if not hasattr(module, attr):
                rel = path.relative_to(ROOT)
                problems.append(
                    f"{rel}:{lineno} {module_name}.{attr} 不存在"
                )

    assert not problems, "引用了不存在的属性：\n  " + "\n  ".join(problems)


def test_no_cross_module_constant_confusion() -> None:
    """端口常量只能从一个地方取。

    `paths.SOCKET_PORT_WINDOWS` 和 `version.SOCKET_PORT_WINDOWS`
    只能存在一个来源。历史上写错过一次（paths 里没有，version
    里才有），Windows 上才暴露。
    """
    import sdm.paths as paths_mod
    import sdm.version as version_mod

    for name in dir(version_mod):
        if not name.isupper():
            continue
        if hasattr(paths_mod, name):
            # 两边都有同一个常量名，容易引错模块
            pytest.fail(
                f"{name} 在 paths 和 version 里都有，"
                "容易引错模块。挪到只有一个地方。"
            )
