"""内存回归：推送不能靠 `evaluate_js`。

**背景**：pywebview 的 `Window.evaluate_js` 每次调用都往
`window._callbacks` 里塞一条 `uuid → None`，而**同步路径永远不删它**
——只有异步 promise 的回调回来时才会 `del`（util.py:318）。

SDM 的推送循环每 0.7 秒推一次，跑两天就是 24 万多条，只增不减。
实测每次调用净增约 340 字节（uuid 字符串 + dict 条目 + cocoa 那边的
局部 class 和 Semaphore），两天下来几十 MB 白扔——这就是「常驻两天
内存就涨上去」的一部分。

修法：推送走 `run_js`（同一条 cocoa 通路，但不碰 `_callbacks`）。

**这个测试必须能抓住回归**：把 `_run_js` 改回 `evaluate_js`，
它就得红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.gui import bridge as bridge_mod


class _FakeWindow:
    """只记录调用的假窗口。

    **故意模拟 pywebview 的行为**：`evaluate_js` 会往 `_callbacks`
    里塞一条永不回收的记录，`run_js` 不会。这样测试才能真的抓住
    「改回 evaluate_js」这种回归。
    """

    def __init__(self) -> None:
        self._callbacks: dict[str, object] = {}
        self.scripts: list[str] = []
        self.eval_calls = 0
        self.run_calls = 0

    def evaluate_js(self, script: str, callback=None):
        import uuid

        self.eval_calls += 1
        self.scripts.append(script)
        # pywebview window.py:449-450 —— 同步路径永不删
        self._callbacks[uuid.uuid1().hex] = callback
        return None

    def run_js(self, script: str):
        self.run_calls += 1
        self.scripts.append(script)
        return None


def _bridge_with_window(window) -> bridge_mod.GuiBridge:
    """造一个只带推送能力的 bridge 实例（不碰引擎和设置）。"""
    instance = bridge_mod.GuiBridge.__new__(bridge_mod.GuiBridge)
    instance._window = window
    return instance


def test_run_js_does_not_touch_callbacks_table():
    """`_run_js` 走 `run_js`，`_callbacks` 一个字节都不涨。"""
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    for _ in range(1000):
        instance._run_js("sdmEvent('global_speed_updated',{})")

    assert window.run_calls == 1000
    assert window.eval_calls == 0
    # 关键断言：pywebview 的回调表没被撑大。
    assert len(window._callbacks) == 0


def test_emit_event_uses_run_js():
    """推送事件必须走 `run_js`。"""
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    instance._emit_event("global_speed_updated", {"bytes_per_sec": 1.0})

    assert window.run_calls == 1
    assert window.eval_calls == 0
    assert len(window._callbacks) == 0
    assert "sdmEvent(" in window.scripts[0]


def test_call_js_uses_run_js():
    """调前端全局函数也必须走 `run_js`。"""
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    instance._call_js("onScheduleFired", {"url": "https://example.com/x"})

    assert window.run_calls == 1
    assert window.eval_calls == 0
    assert len(window._callbacks) == 0
    assert "onScheduleFired(" in window.scripts[0]


def test_run_js_falls_back_when_missing():
    """老版本 pywebview 没有 `run_js` 时退回 `evaluate_js`，别炸。"""

    class _OldWindow:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate_js(self, script: str, callback=None):
            self.calls += 1

    window = _OldWindow()
    instance = _bridge_with_window(window)

    instance._run_js("x()")

    assert window.calls == 1


def test_push_loop_does_not_grow_callbacks():
    """跑 200 轮推送循环，回调表必须是空的。

    这一条是「改回 evaluate_js 就会红」的那条——前面几条只查
    调用次数，绕过去也能过；这条查的是**真实的泄漏后果**。
    """
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    for _ in range(200):
        instance._emit_event("global_speed_updated", {"bytes_per_sec": 1024.0})
        instance._emit_event("task_updated", {"id": "t1", "state": "downloading"})

    assert len(window._callbacks) == 0, (
        f"推送泄漏了 {len(window._callbacks)} 条回调记录——"
        "推送必须走 run_js，不能用 evaluate_js"
    )


def test_prune_removes_only_sync_records():
    """清理只删 value 为 None 的那些。

    异步路径存的是真正的函数——删了会打断还没回来的 promise
    回调，那些一根手指都不能碰。
    """
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    window.evaluate_js("sync-1")
    window.evaluate_js("sync-2")
    window.evaluate_js("async-1", callback=lambda value: None)

    assert len(window._callbacks) == 3

    removed = instance._prune_pywebview_callbacks()

    assert removed == 2
    assert len(window._callbacks) == 1
    # 异步那条还在，而且值还是原来那个函数。
    remaining = list(window._callbacks.values())
    assert len(remaining) == 1 and callable(remaining[0])


def test_prune_handles_missing_window_and_attribute():
    """窗口没挂、或 pywebview 换了实现，都不能炸。"""
    instance = _bridge_with_window(None)
    assert instance._prune_pywebview_callbacks() == 0

    class _NoCallbacks:
        pass

    instance = _bridge_with_window(_NoCallbacks())
    assert instance._prune_pywebview_callbacks() == 0


def test_prune_is_idempotent():
    """清理完再清一次，不该报错也不该再删。"""
    window = _FakeWindow()
    instance = _bridge_with_window(window)

    window.evaluate_js("sync-1")
    assert instance._prune_pywebview_callbacks() == 1
    assert instance._prune_pywebview_callbacks() == 0
    assert len(window._callbacks) == 0
