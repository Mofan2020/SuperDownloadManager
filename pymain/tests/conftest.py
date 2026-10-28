"""pytest 共享配置。

**不引入 pytest-asyncio。** 少一个依赖就少一处 CI 出问题的点，
异步测试用 `run_async()` 包装器自己驱动就够了。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

# 让测试能 import sdm 包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -- 用户数据隔离 -------------------------------------------------
#
# **必须在模块加载时就把路径改掉**，不能靠 fixture。
#
# 原因：`SDMLogger` 是全局单例，写盘在后台线程里跑。日志 entry
# 先进队列，写盘线程随后才落盘。如果只在 fixture 期间改路径，
# fixture 一撤销（monkeypatch 回滚），写盘线程再处理队列里剩下的
# entry 时，路径已经变回用户真实目录——那几条日志就写进用户的
# 日志文件里了。
#
# `HistoryStore` 同理：它在 `__init__` 里就把路径取走了
# （`path or paths.history_db_path()`），单例在测试早期构造，
# 后面再 patch 也不生效。
#
# 所以这里用**进程级**的环境变量覆盖，一改就是整个 pytest 进程
# 的事，不随 fixture 生命周期回滚。
#
# 覆盖的环境变量（见 sdm/paths.py）：
# - `SDM_CONFIG_DIR`   → 配置、设置、历史数据库、socket
# - `SDM_LOG_DIR`      → 日志目录
# - `SDM_DOWNLOAD_DIR` → 下载目录（测试里下东西不该落到 ~/Downloads）

_SANDBOX = Path(tempfile.mkdtemp(prefix="sdm-pytest-"))
for _name, _sub in (
    ("SDM_CONFIG_DIR", "config"),
    ("SDM_LOG_DIR", "logs"),
    ("SDM_DOWNLOAD_DIR", "downloads"),
):
    _dir = _SANDBOX / _sub
    _dir.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_dir)


@atexit.register
def _cleanup_sandbox() -> None:
    """退出时删掉沙箱目录。

    写盘线程是 daemon，进程退出时可能还在写——删目录失败就
    失败，别抛（atexit 里抛异常会盖掉真正的退出原因）。
    """
    shutil.rmtree(_SANDBOX, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_work_dir(tmp_path_factory, monkeypatch):
    """每个测试一个独立的工作目录。

    **工作目录放的是下载辅助文件**（sidecar / 会话文件 / 分片临时
    文件）。它按目标文件的绝对路径哈希分目录，理论上不会互相撞，
    但测试之间共享一个目录会让「这个测试留下的东西」污染下一个
    测试的断言（比如「下载目录里不该有残留」这类检查）。所以每个
    测试单独给一个。

    `SDM_WORK_DIR` 是每次调用现读的（不是模块加载时缓存的），
    所以 monkeypatch 对运行中的代码立即生效。
    """
    work = tmp_path_factory.mktemp("work")
    monkeypatch.setenv("SDM_WORK_DIR", str(work))
    return work


def run_async(coro_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在新的事件循环里跑一个异步函数。"""
    return asyncio.run(coro_fn(*args, **kwargs))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
