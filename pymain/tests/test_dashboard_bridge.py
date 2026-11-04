"""仪表盘和扩展安装的**后端接线**。

前端（app.js）调了两个后端方法：
- `get_dashboard_stats()` —— 仪表盘的统计数据
- `install_browser_extension(browser_id)` —— 装/更新扩展到持久目录

**前端先上、后端后补是常态，接缝最容易断。** 这两个方法要是
不存在，前端不会报错——仪表盘只是永远显示「暂无数据」，扩展安装
按钮点了没反应。很难被发现，所以要在这里钉住。

**这个测试必须能抓住回归**：把方法删掉，它就得红。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.gui import bridge as bridge_mod
from sdm.gui import dashboard as dashboard_mod


class _StubTask:
    def __init__(self, **fields):
        self._fields = fields

    def to_dict(self) -> dict:
        return dict(self._fields)


class _StubEngine:
    def __init__(self, tasks=None):
        self._tasks = tasks or []

    def snapshot(self):
        return list(self._tasks)


class _StubHistory:
    def __init__(self, entries=None):
        self._entries = entries or []

    def all(self, limit: int = 500):
        return list(self._entries)


class _StubHistoryEntry:
    def __init__(self, **fields):
        self._fields = fields

    def to_dict(self) -> dict:
        return dict(self._fields)


def _bridge(engine=None, history=None) -> bridge_mod.GuiBridge:
    instance = bridge_mod.GuiBridge.__new__(bridge_mod.GuiBridge)
    instance.engine = engine or _StubEngine()  # type: ignore[assignment]
    instance.history = history or _StubHistory()  # type: ignore[assignment]
    return instance


# -- 方法存在性 ------------------------------------------------------


def test_bridge_exposes_dashboard_method():
    """仪表盘的后端方法必须在。"""
    assert hasattr(bridge_mod.GuiBridge, "get_dashboard_stats")
    assert callable(bridge_mod.GuiBridge.get_dashboard_stats)


def test_bridge_exposes_install_extension_method():
    """扩展安装的后端方法必须在。"""
    assert hasattr(bridge_mod.GuiBridge, "install_browser_extension")
    assert callable(bridge_mod.GuiBridge.install_browser_extension)


# -- 仪表盘 ----------------------------------------------------------


def test_get_dashboard_stats_returns_real_numbers():
    """有任务有历史时，返回的统计必须真有东西。"""
    engine = _StubEngine(
        [
            _StubTask(
                id="t1",
                state="downloading",
                bytes_done=1000,
                total_bytes=5000,
                speed_bytes_per_sec=250.0,
                category="video",
                filename="a.mp4",
            ),
        ]
    )
    history = _StubHistory(
        [
            _StubHistoryEntry(
                id="h1",
                filename="b.zip",
                total_bytes=2048,
                bytes_done=2048,
                state="completed",
                finished_at=9e9,  # 未来 = 今天
            ),
        ]
    )
    instance = _bridge(engine, history)

    result = instance.get_dashboard_stats()

    assert result["ok"] is True
    assert result["active"]["count"] == 1
    assert result["active"]["speed"] == 250.0
    # 累计下载数 = 任务 + 历史（按 id 合并，id 不同就是两个）
    assert result["totals"]["downloads"] == 2
    # 分类桶至少有视频和 zip 的分类
    ids = {item["id"] for item in result["totals"]["by_category"]}
    assert "video" in ids


def test_get_dashboard_stats_survives_broken_engine():
    """引擎炸了不能把 GUI 带下去——返回 ok=False，前端显示「暂无数据」。"""

    class _BrokenEngine:
        def snapshot(self):
            raise RuntimeError("引擎坏了")

    instance = _bridge(_BrokenEngine(), _StubHistory())

    result = instance.get_dashboard_stats()

    assert result["ok"] is False
    assert "error" in result


def test_get_dashboard_stats_empty_is_still_ok():
    """没有任务没有历史时也要返回 ok=True（前端会画一个空表）。"""
    instance = _bridge(_StubEngine(), _StubHistory())

    result = instance.get_dashboard_stats()

    assert result["ok"] is True
    assert result["active"]["count"] == 0
    assert result["totals"]["downloads"] == 0
    assert result["recent"] == []


def test_dashboard_module_is_importable_and_pure():
    """统计逻辑在独立模块里（能单测，不依赖 GUI）。"""
    assert hasattr(dashboard_mod, "build_dashboard")
    stats = dashboard_mod.build_dashboard([], [])
    assert stats["ok"] is True


# -- 扩展安装 --------------------------------------------------------


def test_install_browser_extension_delegates_to_store(monkeypatch):
    """装扩展要真的调到 extension_store.ensure_persisted。"""
    from sdm.ipc import extension_store

    calls: list[str] = []

    def fake_ensure(browser_id: str) -> dict:
        calls.append(browser_id)
        return {
            "ok": True,
            "path": "/tmp/extensions/Chrome",
            "updated": True,
            "fingerprint": "abc",
            "message": "扩展目录已更新到最新版本",
        }

    monkeypatch.setattr(extension_store, "ensure_persisted", fake_ensure)

    instance = _bridge()
    result = instance.install_browser_extension("chrome")

    assert calls == ["chrome"]
    assert result["ok"] is True
    assert result["path"] == "/tmp/extensions/Chrome"
    assert result["updated"] is True


def test_install_browser_extension_reports_failure(monkeypatch):
    """装不上要把原因带回去——前端要显示出来。"""
    from sdm.ipc import extension_store

    def fake_ensure(browser_id: str) -> dict:
        return {
            "ok": False,
            "path": "",
            "updated": False,
            "fingerprint": "",
            "message": "应用包里没有这个浏览器的扩展",
        }

    monkeypatch.setattr(extension_store, "ensure_persisted", fake_ensure)

    instance = _bridge()
    result = instance.install_browser_extension("safari")

    assert result["ok"] is False
    assert result["message"]


def test_install_browser_extension_never_raises(monkeypatch):
    """装扩展炸了不能把 GUI 带下去。"""
    from sdm.ipc import extension_store

    def boom(browser_id: str) -> dict:
        raise OSError("磁盘满了")

    monkeypatch.setattr(extension_store, "ensure_persisted", boom)

    instance = _bridge()
    result = instance.install_browser_extension("chrome")

    assert result["ok"] is False
    assert result["message"]
