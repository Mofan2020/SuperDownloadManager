"""UI 层测试：F052 / F057 / F058 / F059 / F076 / F123 / F135 / F140 / F147 / F148 / F149。

Swift 参照：
- `App/MainWindow/DownloadListView.swift`（F076 列排序）
- `App/MainWindow/ClipboardConfirmView.swift`（F058 弹窗）
- `App/MainWindow/DownloadListStore.swift`（F135 通知、F149 同步）
- `App/SDMApp.swift`（F147 关窗、F148 Dock）
- `App/MainWindow/SettingsView.swift`（F140 带宽）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm import clipboard
from sdm.gui import bridge as bridge_mod
from sdm.gui import macos_status

# -- i18n 一致性 ---------------------------------------------------


def test_i18n_key_sets_match():
    """中英 key 集合必须完全一致。

    漏加一边的话，另一种语言下那条文案会显示成 key 本身
    （`t()` 找不到就返回 key）。
    """
    base = ROOT / "sdm" / "i18n"
    zh = set(json.loads((base / "zh-Hans.json").read_text(encoding="utf-8")))
    en = set(json.loads((base / "en.json").read_text(encoding="utf-8")))
    assert zh == en, f"中英 key 不一致: {zh ^ en}"


def test_new_list_column_keys_exist():
    """列表表头用的 key 都有。"""
    base = ROOT / "sdm" / "i18n"
    for name in ("zh-Hans.json", "en.json"):
        d = json.loads((base / name).read_text(encoding="utf-8"))
        for key in (
            "listColumnName",
            "listColumnState",
            "listColumnSize",
            "listColumnSpeed",
            "clipboardDisableToggle",
        ):
            assert key in d, f"{name} 缺 key: {key}"


# -- F135 系统通知 -------------------------------------------------


class _FakeEngine:
    """假引擎。任务列表由测试自己控制。"""

    def __init__(self, tasks=None):
        self._tasks = tasks or []

    def snapshot(self):
        return list(self._tasks)

    def get(self, task_id):
        for t in self._tasks:
            if t.id == task_id:
                return t
        return None


def _make_bridge(monkeypatch, tasks=None, notify_on_complete=True, capture_clipboard=True):
    """造一个 GuiBridge，依赖全部替换掉。"""

    summaries = tasks or []

    class _FakeSettings:
        def __init__(self):
            self.locale = "zh-Hans"
            self.notify_on_complete = notify_on_complete
            self.capture_clipboard = capture_clipboard
            self.classify_enabled = True
            self.open_at_login = False
            self.threads_default = 32

    class _FakeStore:
        settings = _FakeSettings()

        def update(self, changes):
            for k, v in changes.items():
                setattr(self.settings, k, v)

        def set(self, key, value):
            setattr(self.settings, key, value)

    monkeypatch.setattr(
        bridge_mod.SettingsStore, "shared", staticmethod(lambda: _FakeStore())
    )

    sent: list[tuple] = []
    monkeypatch.setattr(
        bridge_mod, "notify", lambda title, subtitle="", body="": sent.append((title, subtitle, body)) or True
    )

    eng = _FakeEngine(summaries)
    b = bridge_mod.GuiBridge(eng)
    b._test_notify_log = sent
    return b, sent


def _task(state="downloading", task_id="t1", filename="a.zip", total=1000, error=None):
    from sdm.engine.download_engine import TaskSummary

    return TaskSummary(
        id=task_id,
        url=f"http://example.com/{task_id}",
        state=state,
        filename=filename,
        total_bytes=total,
        error_message=error,
    )


def test_notification_sent_when_task_completes(monkeypatch):
    """任务转 completed 时发通知。"""
    b, sent = _make_bridge(monkeypatch, tasks=[_task(state="completed")])

    b.get_tasks()

    assert sent, "任务已经是 completed 了，应该发通知"
    title, subtitle, body = sent[0]
    assert title == "下载完成"
    assert subtitle == "a.zip"
    assert body  # 总大小格式化


def test_notification_sent_once_per_task(monkeypatch):
    """同一个任务只通知一次。"""
    b, sent = _make_bridge(monkeypatch, tasks=[_task(state="completed")])

    b.get_tasks()
    b.get_tasks()
    b.get_tasks()

    assert len(sent) == 1, f"通知了 {len(sent)} 次，应该只一次"


def test_no_notification_when_switch_off(monkeypatch):
    """开关关掉时完成和失败都不发（一个开关管两类）。"""
    b, sent = _make_bridge(
        monkeypatch,
        tasks=[_task(state="completed", task_id="t1"), _task(state="failed", task_id="t2")],
        notify_on_complete=False,
    )

    b.get_tasks()

    assert sent == [], f"开关关了还发了通知: {sent}"


def test_failure_notification_payload(monkeypatch):
    """失败通知的标题和正文。"""
    b, sent = _make_bridge(
        monkeypatch, tasks=[_task(state="failed", error="连接被重置")]
    )

    b.get_tasks()

    title, _subtitle, body = sent[0]
    assert title == "下载失败"
    assert body == "连接被重置"


def test_failure_notification_unknown_error(monkeypatch):
    """没有错误信息时用「未知错误」。"""
    b, sent = _make_bridge(monkeypatch, tasks=[_task(state="failed", error=None)])

    b.get_tasks()

    assert sent[0][2] == "未知错误"


def test_completion_and_failure_both_notified(monkeypatch):
    """完成和失败各发一次。"""
    b, sent = _make_bridge(
        monkeypatch,
        tasks=[
            _task(state="completed", task_id="t1"),
            _task(state="failed", task_id="t2", error="boom"),
        ],
    )

    b.get_tasks()

    titles = {t for t, _, _ in sent}
    assert titles == {"下载完成", "下载失败"}


# -- F052 关闭自动分类 ---------------------------------------------


def test_classify_disabled_uses_base_dir(monkeypatch, tmp_path):
    """关掉自动分类后落基础目录，不进分类子目录。

    Swift 对照：App/MainWindow/DownloadListStore.swift:367
    `if !classifyEnabled { return downloads }`。
    """
    import sdm.gui.bridge as bm

    monkeypatch.setattr(bm.paths, "download_dir", lambda: tmp_path)

    b, _ = _make_bridge(monkeypatch)
    b.settings_store.settings.classify_enabled = False

    started = []

    async def _fake_start(specs):
        started.extend(specs)
        return ["id-1"]

    b.engine.start = _fake_start

    b.add_url("http://example.com/a.zip", category="video")

    assert started
    assert started[0].destination_directory == tmp_path


def test_classify_enabled_uses_category_dir(monkeypatch, tmp_path):
    """开着分类时落分类子目录。"""
    import sdm.gui.bridge as bm

    monkeypatch.setattr(bm.paths, "download_dir", lambda: tmp_path)

    b, _ = _make_bridge(monkeypatch)

    started = []

    async def _fake_start(specs):
        started.extend(specs)
        return ["id-1"]

    b.engine.start = _fake_start

    b.add_url("http://example.com/a.zip", category="video")

    assert started[0].destination_directory.name.lower() == "video"


def test_classify_disabled_ignores_explicit_save_dir(monkeypatch, tmp_path):
    """显式指定目录时照用户给的走（分类开关不管这个）。"""
    import sdm.gui.bridge as bm

    monkeypatch.setattr(bm.paths, "download_dir", lambda: tmp_path)

    b, _ = _make_bridge(monkeypatch)
    b.settings_store.settings.classify_enabled = False

    started = []

    async def _fake_start(specs):
        started.extend(specs)
        return ["id-1"]

    b.engine.start = _fake_start

    b.add_url("http://example.com/a.zip", save_dir=str(tmp_path / "custom"))

    assert started[0].destination_directory == tmp_path / "custom"


# -- F057 同 URL 去重 ----------------------------------------------


def test_clipboard_candidate_suppressed_for_downloading_url(monkeypatch):
    """已经在下载的 URL 不再弹候选。"""
    b, _ = _make_bridge(monkeypatch, tasks=[_task(state="downloading")])

    called: list[str] = []
    b._call_js = lambda func, payload=None: called.append(func)

    b.set_clipboard_candidate("http://example.com/t1")

    assert "onClipboardCandidate" not in called


def test_clipboard_candidate_shown_for_new_url(monkeypatch):
    """新 URL 正常弹候选。"""
    b, _ = _make_bridge(monkeypatch, tasks=[_task(state="downloading", task_id="other")])

    called: list[str] = []
    b._call_js = lambda func, payload=None: called.append(func)

    b.set_clipboard_candidate("http://example.com/t1")

    assert "onClipboardCandidate" in called


def test_clipboard_candidate_shown_for_finished_url(monkeypatch):
    """已完成的 URL 可以重新下载，候选照弹。"""
    b, _ = _make_bridge(monkeypatch, tasks=[_task(state="completed")])

    called: list[str] = []
    b._call_js = lambda func, payload=None: called.append(func)

    b.set_clipboard_candidate("http://example.com/t1")

    assert "onClipboardCandidate" in called


# -- F059 剪贴板 kill switch --------------------------------------


def test_clipboard_monitor_stops_when_disabled(monkeypatch):
    """关掉捕获后下一轮就不再检测。"""
    checked: list[int] = []

    monkeypatch.setattr(
        clipboard.ClipboardMonitor, "_capture_enabled", staticmethod(lambda: False)
    )
    monkeypatch.setattr(
        clipboard.ClipboardMonitor,
        "_check",
        lambda self: checked.append(1),
    )

    mon = clipboard.ClipboardMonitor(on_candidate=lambda u: None, poll_interval=0.01)
    mon.start()
    import time

    time.sleep(0.15)
    mon.stop()

    assert checked == [], f"关掉之后还在检测: {len(checked)} 次"


def test_clipboard_monitor_checks_when_enabled(monkeypatch):
    """开着的时候照常检测。"""
    checked: list[int] = []

    monkeypatch.setattr(
        clipboard.ClipboardMonitor, "_capture_enabled", staticmethod(lambda: True)
    )
    monkeypatch.setattr(
        clipboard.ClipboardMonitor,
        "_check",
        lambda self: checked.append(1),
    )

    mon = clipboard.ClipboardMonitor(on_candidate=lambda u: None, poll_interval=0.01)
    mon.start()
    import time

    time.sleep(0.15)
    mon.stop()

    assert checked, "开着的时候没检测"


def test_capture_enabled_reads_shared_settings(monkeypatch):
    """读的是共享的设置单例。"""
    from sdm.settings import SettingsStore

    class _FakeStore:
        class settings:
            capture_clipboard = False

    monkeypatch.setattr(
        SettingsStore, "shared", staticmethod(lambda: _FakeStore())
    )
    assert clipboard.ClipboardMonitor._capture_enabled() is False


# -- F148 Dock 图标 ------------------------------------------------


def test_dock_policy_constants_match_appkit():
    """常量跟 AppKit 一致（Regular=0, Accessory=1）。"""
    AppKit = pytest.importorskip("AppKit")
    assert AppKit.NSApplicationActivationPolicyRegular == macos_status.POLICY_REGULAR
    assert AppKit.NSApplicationActivationPolicyAccessory == macos_status.POLICY_ACCESSORY


def test_dock_grace_is_150ms():
    """宽限是 0.15 秒。Swift 对照 App/SDMApp.swift:444-541。"""
    assert macos_status.DOCK_HIDE_GRACE_SECONDS == 0.15


def test_set_dock_visible_non_darwin_returns_false(monkeypatch):
    """非 macOS 上不切。"""
    monkeypatch.setattr(macos_status.sys, "platform", "linux")
    assert macos_status.set_dock_visible(False) is False


def test_current_activation_policy_non_darwin(monkeypatch):
    """非 macOS 返回 -1。"""
    monkeypatch.setattr(macos_status.sys, "platform", "linux")
    assert macos_status.current_activation_policy() == -1


def test_has_visible_key_window_non_darwin(monkeypatch):
    """非 macOS 返回 False。"""
    monkeypatch.setattr(macos_status.sys, "platform", "linux")
    assert macos_status.has_visible_key_window() is False


# -- F149 开机自启同步 ---------------------------------------------


def test_sync_open_at_login_is_idempotent(monkeypatch):
    """两边一致时不调用 set_open_at_login。"""
    b, _ = _make_bridge(monkeypatch)
    b.settings_store.settings.open_at_login = True

    calls: list[bool] = []
    monkeypatch.setattr(
        bridge_mod, "is_open_at_login_enabled", lambda: True
    )
    monkeypatch.setattr(
        bridge_mod, "set_open_at_login", lambda enabled: calls.append(enabled) or True
    )

    result = b.sync_open_at_login()

    assert result["changed"] is False
    assert calls == [], f"已经一致还去改: {calls}"


def test_sync_open_at_login_fixes_mismatch(monkeypatch):
    """设置说开了、系统里没开 → 补上。"""
    b, _ = _make_bridge(monkeypatch)
    b.settings_store.settings.open_at_login = True

    calls: list[bool] = []
    monkeypatch.setattr(bridge_mod, "is_open_at_login_enabled", lambda: False)
    monkeypatch.setattr(
        bridge_mod, "set_open_at_login", lambda enabled: calls.append(enabled) or True
    )

    result = b.sync_open_at_login()

    assert result["changed"] is True
    assert calls == [True]


def test_sync_open_at_login_disables_when_setting_off(monkeypatch):
    """设置说关了、系统里还开着 → 注销。"""
    b, _ = _make_bridge(monkeypatch)
    b.settings_store.settings.open_at_login = False

    calls: list[bool] = []
    monkeypatch.setattr(bridge_mod, "is_open_at_login_enabled", lambda: True)
    monkeypatch.setattr(
        bridge_mod, "set_open_at_login", lambda enabled: calls.append(enabled) or True
    )

    result = b.sync_open_at_login()

    assert result["changed"] is True
    assert calls == [False]


# -- F123 在 Console.app 中打开 ------------------------------------


def test_open_log_in_console_uses_open_command(monkeypatch, tmp_path):
    """命令是 open -a Console <路径>。"""
    log_file = tmp_path / "sdm.log"
    log_file.write_text("x")

    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "sdm.system_integration.subprocess.run",
        lambda cmd, **kwargs: calls.append(cmd) or _Result(),
    )
    monkeypatch.setattr("sdm.system_integration.platform.system", lambda: "Darwin")

    from sdm.system_integration import open_log_in_console

    assert open_log_in_console(log_file) is True
    assert calls[0][:3] == ["open", "-a", "Console"]
    assert str(log_file) in calls[0]


def test_open_log_in_console_missing_file(monkeypatch, tmp_path):
    """文件不存在返回 False。"""
    monkeypatch.setattr("sdm.system_integration.platform.system", lambda: "Darwin")

    from sdm.system_integration import open_log_in_console

    assert open_log_in_console(tmp_path / "nope.log") is False


def test_open_log_in_console_non_macos(monkeypatch, tmp_path):
    """非 macOS 返回 False。"""
    log_file = tmp_path / "sdm.log"
    log_file.write_text("x")
    monkeypatch.setattr("sdm.system_integration.platform.system", lambda: "Windows")

    from sdm.system_integration import open_log_in_console

    assert open_log_in_console(log_file) is False


def test_resolve_category_accepts_extension():
    """扩展名也能解析成分类。"""
    from sdm.engine.categories import DownloadCategory

    assert bridge_mod._resolve_category("mp4") is DownloadCategory.VIDEO
    assert bridge_mod._resolve_category(".mp3") is DownloadCategory.MUSIC


def test_resolve_category_accepts_name():
    """分类名能解析。"""
    from sdm.engine.categories import DownloadCategory

    assert bridge_mod._resolve_category("video") is DownloadCategory.VIDEO


def test_resolve_category_unknown_returns_none():
    """认不出来返回 None。"""
    assert bridge_mod._resolve_category("zzz-not-a-thing") is None
    assert bridge_mod._resolve_category("") is None


def test_notification_not_repeated_after_retry(monkeypatch):
    """同一个任务重试后再次完成，只通知一次。

    `_notified` 这层去重：任务 id 相同、完成过之后又变成
    downloading 再变 completed，不该再弹一次完成通知。
    """
    b, sent = _make_bridge(monkeypatch, tasks=[_task(state="completed")])

    b.get_tasks()
    assert len(sent) == 1

    # 任务重试：状态回到 downloading
    b.engine._tasks[0].state = "downloading"
    b.get_tasks()
    assert len(sent) == 1, "重试过程中不该发通知"

    # 再次完成
    b.engine._tasks[0].state = "completed"
    b.get_tasks()
    assert len(sent) == 1, f"重试后又通知了，总共 {len(sent)} 条"


def test_startup_notifies_terminal_tasks_like_swift(monkeypatch):
    """启动时已在终态的历史任务，会发通知——**跟 Swift 一样**。

    Swift 对照：App/MainWindow/DownloadListStore.swift:145-160。
    `notifiedCompletionIDs` 启动时是空集，任务列表首次加载
    （`mapped != tasks`）时就会对终态任务发一遍通知。

    Python 版用 `_last_states`（首次 previous=None）达到同样效果。
    这条是**行为对齐**，不是 bug——别在这里发明需求。
    """
    b, sent = _make_bridge(
        monkeypatch,
        tasks=[
            _task(state="completed", task_id="old1"),
            _task(state="failed", task_id="old2", error="x"),
        ],
    )

    b.get_tasks()

    titles = {t for t, _, _ in sent}
    assert titles == {"下载完成", "下载失败"}
