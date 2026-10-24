"""pytest 共享配置。

**不引入 pytest-asyncio。** 少一个依赖就少一处 CI 出问题的点，
异步测试用 `run_async()` 包装器自己驱动就够了。
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

# 让测试能 import sdm 包
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run_async(coro_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """在新的事件循环里跑一个异步函数。"""
    return asyncio.run(coro_fn(*args, **kwargs))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
