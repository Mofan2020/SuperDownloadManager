"""F087/F100/F119/F123 的验收测试。

四条功能在 Python 重写版里都缺过接线：

- F087 速度 EWMA：`speed_display.compute()` 写好了但没人调，
  引擎直接把 delta/elapsed 塞进对外速度，Swift v1.0.7 那个
  「进度满了速度还在显示」的 bug 又回来了。
- F100 历史落库：`HistoryStore.insert()` 写好了（幂等），
  但没有生产调用方，历史列表永远是空的。
- F119 每日定时清理：`_run_cleanup()` 只在设置变更时跑一次，
  没有「每天一次」。
- F123 在 Console.app 中打开：i18n 文案备好了，后端没有函数。

**这些测试验证行为，不是验证实现细节。** 断言里写死具体数字
（α=0.4 公式算出来的值），不是 `> 0` 那种恒真断言。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdm.engine.content_sniffer import ProbeError
from sdm.engine.download_engine import DownloadEngine, StartSpec, TaskState, TaskSummary
from sdm.engine.http_downloader import HTTPDownloader
from sdm.engine.speed_display import SpeedInput, compute
from sdm.history_store import HistoryEntry, HistoryStore
from sdm.logging_setup import LogSettings, SDMLogger
from sdm.system_integration import open_log_in_console
from tests.conftest import run_async
from tests.range_server import RangeServer

#: EMA 的 α。Swift v1.0.5 起 UI 一直是这个值。
ALPHA = 0.4


# ====================================================================
# F087 速度 EWMA
# ====================================================================


def test_ema_second_frame_equals_alpha_formula() -> None:
    """第二帧的速度必须**恰好**等于 α 公式算出来的值。

    第一帧 raw=1000（EMA 缓存是 0，直接取 raw）；
    第二帧 raw=2000 → 0.4*2000 + 0.6*1000 = 1400.0。

    写死 1400.0 而不是断言 `> 0`：公式改错了这个测试就红。
    """
    first = compute(
        SpeedInput(
            state="downloading",
            engine_speed_bytes_per_sec=0.0,
            delta_bytes_since_last_tick=1000,
            tick_interval_seconds=1.0,
        ),
        previous_ema=0.0,
        alpha=ALPHA,
    )
    assert first.displayed_speed == 1000.0
    assert first.next_ema == 1000.0

    second = compute(
        SpeedInput(
            state="downloading",
            engine_speed_bytes_per_sec=0.0,
            delta_bytes_since_last_tick=2000,
            tick_interval_seconds=1.0,
        ),
        previous_ema=first.next_ema,
        alpha=ALPHA,
    )
    expected = ALPHA * 2000 + (1 - ALPHA) * 1000
    assert expected == 1400.0
    assert second.displayed_speed == 1400.0
    assert second.next_ema == 1400.0


def test_engine_applies_ema_not_raw_speed() -> None:
    """引擎必须把速度接到 `compute()` 上，不是自己算 EMA。

    引擎是事件驱动的：一次进度推 2000 字节、间隔 1 秒 → raw
    2000；再推 1000 字节、间隔 1 秒 → raw 1000。
    对外显示的必须是 EMA 值 0.4*1000 + 0.6*2000 = 1600，
    不是第二次的 raw 1000。
    """
    engine = DownloadEngine()
    summary = TaskSummary(id="t1", url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks["t1"] = summary

    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_bytes_per_sec == 2000.0
    assert summary.speed_ema == 2000.0

    engine._apply_speed(summary, delta_bytes=1000, elapsed=1.0)
    assert summary.speed_ema == 1600.0, "第二帧没走 EMA 公式"
    assert summary.speed_bytes_per_sec == 1600.0, "对外速度没接 EMA 输出"


def test_terminal_state_zeroes_speed_and_clears_ema_cache() -> None:
    """终态速度恰好 0.0，且 EMA 缓存被清掉。

    Swift v1.0.7 的 bug：进度满了速度还在显示（EMA 以 0.6 的
    系数往 0 衰减，要过一秒才掉下去）。修法是终态硬清零 +
    清缓存——不是让它衰减。
    """
    engine = DownloadEngine()
    summary = TaskSummary(id="t2", url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks["t2"] = summary

    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_ema == 2000.0

    summary.state = TaskState.COMPLETED.value
    engine._zero_speed(summary)

    assert summary.speed_bytes_per_sec == 0.0, "终态速度必须恰好是 0.0"
    assert summary.speed_ema == 0.0, "终态必须清掉 EMA 缓存"


def test_resume_does_not_carry_stale_ema() -> None:
    """恢复下载后第一帧不能带暂停前的旧 EMA。

    如果缓存没清，恢复后第一帧会是 0.4*raw + 0.6*旧值，
    而不是干净的 raw。
    """
    engine = DownloadEngine()
    summary = TaskSummary(id="t3", url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks["t3"] = summary

    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_ema == 2000.0

    # 暂停 → 清零
    summary.state = TaskState.PAUSED.value
    engine._zero_speed(summary)
    assert summary.speed_ema == 0.0

    # 恢复 → 第一帧就是 raw 本身，不掺旧值
    summary.state = TaskState.DOWNLOADING.value
    engine._apply_speed(summary, delta_bytes=500, elapsed=1.0)
    assert summary.speed_ema == 500.0, "恢复后第一帧带了暂停前的旧 EMA"


def test_paused_state_zeroes_speed_and_clears_ema_cache() -> None:
    """暂停态和终态一样：速度 0，EMA 缓存清零。"""
    engine = DownloadEngine()
    summary = TaskSummary(id="t4", url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks["t4"] = summary

    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_ema == 2000.0

    summary.state = TaskState.PAUSED.value
    engine._zero_speed(summary)

    assert summary.speed_bytes_per_sec == 0.0
    assert summary.speed_ema == 0.0


def test_awaiting_new_link_also_zeroes_speed() -> None:
    """`awaiting_new_link` 在 Swift 里也算暂停态——同样归零。"""
    out = compute(
        SpeedInput(
            state="awaiting_new_link",
            engine_speed_bytes_per_sec=0.0,
            delta_bytes_since_last_tick=5000,
            tick_interval_seconds=1.0,
        ),
        previous_ema=3000.0,
        alpha=ALPHA,
    )
    assert out.displayed_speed == 0.0
    assert out.next_ema == 0.0


def test_engine_pause_and_cancel_zero_the_speed(tmp_path) -> None:
    """引擎的 pause()/cancel() 都要清速度，不只是清显示值。

    这两条路径以前只写 `speed_bytes_per_sec = 0.0`，
    EMA 缓存留着——恢复后第一帧被旧值带跑。
    """
    engine = DownloadEngine()
    summary = TaskSummary(id="t5", url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks["t5"] = summary
    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_ema == 2000.0

    run_async(engine.pause, "t5")
    assert summary.state == TaskState.PAUSED.value
    assert summary.speed_bytes_per_sec == 0.0
    assert summary.speed_ema == 0.0, "pause 没清 EMA 缓存"

    # 恢复下载（引擎的 resume() 就是把状态改回 downloading）。
    # 第一帧必须是干净的 raw，不带暂停前的旧 EMA。
    summary.state = TaskState.DOWNLOADING.value
    engine._apply_speed(summary, delta_bytes=2000, elapsed=1.0)
    assert summary.speed_ema == 2000.0, "恢复后第一帧带了暂停前的旧 EMA"

    run_async(engine.cancel, "t5")
    assert summary.speed_bytes_per_sec == 0.0
    assert summary.speed_ema == 0.0, "cancel 没清 EMA 缓存"


def test_completed_download_reports_zero_speed(tmp_path) -> None:
    """端到端：下完之后速度必须是 0.0，不能还挂着最后那帧的值。

    这是用户真正看到的 bug——「进度条满了，速度还在显示」。
    """
    srv = RangeServer(total_bytes=256 * 1024)
    try:

        async def _inner() -> None:
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            task_id = ids[0]
            for _ in range(100):
                await asyncio.sleep(0.05)
                summary = engine.get(task_id)
                if summary is not None and summary.state == TaskState.COMPLETED.value:
                    break
            summary = engine.get(task_id)
            assert summary is not None
            assert summary.state == TaskState.COMPLETED.value
            assert summary.speed_bytes_per_sec == 0.0, (
                f"完成后速度还是 {summary.speed_bytes_per_sec}"
            )
            assert summary.speed_ema == 0.0, "完成后 EMA 缓存没清"

        run_async(_inner)
    finally:
        srv.stop()


# ====================================================================
# F100 历史落库
# ====================================================================


def test_history_row_written_when_task_completes(tmp_path) -> None:
    """任务进终态后 history 表要有一行，字段正确。

    `started_at` 必须是**真实值**——Swift 版写的是
    `Date().addingTimeInterval(-60)`（伪造的「60 秒前开始」）。
    Python 版要用 TaskSummary 里真实的 started_at。
    """
    store = HistoryStore(tmp_path / "history.db")
    srv = RangeServer(total_bytes=256 * 1024)
    try:

        async def _inner() -> None:
            engine = DownloadEngine(history_store=store)
            ids = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            task_id = ids[0]
            for _ in range(100):
                await asyncio.sleep(0.05)
                summary = engine.get(task_id)
                if summary is not None and summary.state == TaskState.COMPLETED.value:
                    break
            await asyncio.sleep(0.1)

        started = time.time()
        run_async(_inner)

        rows = store.all(10)
        assert len(rows) == 1, f"历史表里应有 1 行，实际 {len(rows)} 行"
        entry = rows[0]
        assert entry.state == "completed"
        assert entry.url.startswith("http://127.0.0.1:")
        assert entry.filename == "out.bin"
        assert entry.destination_path.endswith("out.bin")
        assert entry.total_bytes == 256 * 1024
        assert entry.bytes_done == 256 * 1024
        assert entry.error_message is None
        # 真实 started_at：任务开始前后几秒内，不是「60 秒前」
        assert entry.started_at >= started - 5.0, (
            f"started_at 是伪造的（{entry.started_at} < {started - 5.0}）"
        )
        assert entry.started_at <= time.time() + 1.0
        assert entry.finished_at >= entry.started_at
    finally:
        srv.stop()


def test_history_is_idempotent_for_same_task_id(tmp_path) -> None:
    """同一个 task id 写两次，表里仍然只有一行。

    `INSERT OR REPLACE` 保证幂等——Swift 版每次 store 变化都
    重写所有终态行，Python 版只需要写一次。
    """
    store = HistoryStore(tmp_path / "history.db")
    entry = HistoryEntry(
        id="same-id",
        url="https://example.com/a.zip",
        filename="a.zip",
        destination_path=str(tmp_path / "a.zip"),
        total_bytes=100,
        bytes_done=100,
        state="completed",
        error_message=None,
        started_at=time.time(),
        finished_at=time.time(),
    )
    store.insert(entry)
    store.insert(entry)

    rows = store.all(10)
    assert len(rows) == 1, f"同一 id 写两次后应有 1 行，实际 {len(rows)} 行"
    assert rows[0].id == "same-id"


def test_history_write_failure_does_not_break_task(tmp_path) -> None:
    """写库失败绝不能影响下载主流程。

    注入一个一写就抛异常的 store，任务照样要正常完成、
    状态照样是 completed。
    """

    class _BrokenStore(HistoryStore):
        def insert(self, entry: HistoryEntry) -> None:
            raise RuntimeError("磁盘满了")

    srv = RangeServer(total_bytes=256 * 1024)
    try:

        async def _inner() -> None:
            engine = DownloadEngine(history_store=_BrokenStore(tmp_path / "broken.db"))
            ids = await engine.start(
                [
                    StartSpec(
                        url=srv.url,
                        destination_directory=tmp_path,
                        filename_override="out.bin",
                    )
                ]
            )
            task_id = ids[0]
            for _ in range(100):
                await asyncio.sleep(0.05)
                summary = engine.get(task_id)
                if summary is not None and summary.state == TaskState.COMPLETED.value:
                    break
            summary = engine.get(task_id)
            assert summary is not None
            assert summary.state == TaskState.COMPLETED.value, "写库失败把任务状态搞坏了"

        run_async(_inner)
    finally:
        srv.stop()


def test_failed_task_is_written_to_history(tmp_path) -> None:
    """失败的任务也要落历史——用户要能在历史里看到它为什么没成。

    用假的 downloader 让探测直接失败：走真实网络的话 500 会先
    走完 8 秒的探测重试预算，测试又慢又不准。

    注意 403 不算失败——403/410/451 是「链接失效」，任务进
    `awaiting_new_link`，那不是终态（Swift 的 `isTerminal` 也
    只认 completed/failed/cancelled），所以不落历史。
    """

    class _FailingDownloader(HTTPDownloader):
        async def probe(self, url, extra_headers=None):
            raise ProbeError("HTTP 500", status=500)

    store = HistoryStore(tmp_path / "history.db")

    async def _inner() -> None:
        engine = DownloadEngine(history_store=store, downloader=_FailingDownloader())
        ids = await engine.start(
            [
                StartSpec(
                    url="http://127.0.0.1:1/x.bin",
                    destination_directory=tmp_path,
                    filename_override="out.bin",
                )
            ]
        )
        task_id = ids[0]
        for _ in range(100):
            await asyncio.sleep(0.05)
            summary = engine.get(task_id)
            if summary is not None and summary.state == TaskState.FAILED.value:
                break
        await asyncio.sleep(0.1)

    run_async(_inner)
    rows = store.all(10)
    assert len(rows) == 1, f"失败任务也该落历史，实际 {len(rows)} 行"
    assert rows[0].state == "failed"
    assert rows[0].error_message, "失败任务的历史记录里应该有错误信息"


# ====================================================================
# F119 每日定时清理
# ====================================================================


def test_cleanup_deletes_expired_rotated_logs(tmp_path) -> None:
    """清理逻辑：过期���轮转文件被删，没过期的留着。

    直接调清理方法，目录指向 tmp_path——**不碰用户真实的
    日志目录**（~/Library/Logs/SuperDownloadManager/）。
    """
    log_dir = tmp_path / "cleanup_logs"
    log_dir.mkdir()
    expired = log_dir / "sdm-2020-01-01.log"
    expired.write_text("old\n", encoding="utf-8")
    fresh = log_dir / "sdm-2026-01-01.log"
    fresh.write_text("new\n", encoding="utf-8")
    # 把过期文件的 mtime 推到 60 天前
    old = time.time() - 60 * 24 * 3600
    os.utime(expired, (old, old))

    logger = SDMLogger(log_dir=log_dir, cleanup_enabled=True)
    logger.update_settings(LogSettings(retention_days=30))

    assert not expired.exists(), "过期日志没被删"
    assert fresh.exists(), "没过期的日志被误删了"


def test_cleanup_timer_runs_on_injected_interval(tmp_path) -> None:
    """定时器按注入的间隔跑，不是硬编码 24 小时。

    间隔注入 0.05 秒，等它自己触发——**不真等 24 小时**。
    """
    log_dir = tmp_path / "cleanup_logs"
    log_dir.mkdir()
    expired = log_dir / "sdm-2020-01-01.log"
    expired.write_text("old\n", encoding="utf-8")
    old = time.time() - 60 * 24 * 3600
    os.utime(expired, (old, old))

    logger = SDMLogger(
        cleanup_interval=0.05,
        log_dir=log_dir,
        cleanup_enabled=True,
    )
    logger.update_settings(LogSettings(retention_days=30))
    logger.start_cleanup_timer()
    try:
        deadline = time.time() + 5.0
        while expired.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert not expired.exists(), "定时器没在注入的间隔内触发清理"
    finally:
        logger.stop_cleanup_timer()


def test_cleanup_timer_stops_when_asked(tmp_path) -> None:
    """定时器能停掉——stop() 之后不再清理。"""
    log_dir = tmp_path / "cleanup_logs"
    log_dir.mkdir()
    logger = SDMLogger(
        cleanup_interval=0.05,
        log_dir=log_dir,
        cleanup_enabled=True,
    )
    logger.start_cleanup_timer()
    thread = logger._cleanup_thread
    assert thread is not None and thread.is_alive()

    logger.stop_cleanup_timer()
    assert not thread.is_alive(), "stop 之后定时线程还活着"
    assert logger._cleanup_thread is None


def test_start_starts_cleanup_timer_and_stop_stops_it(tmp_path) -> None:
    """logger.start() 起定时器，logger.stop() 停掉。"""
    logger = SDMLogger(
        cleanup_interval=0.05,
        log_dir=tmp_path / "cleanup_logs",
        cleanup_enabled=True,
    )
    logger.start()
    try:
        assert logger._cleanup_thread is not None
        assert logger._cleanup_thread.is_alive()
    finally:
        logger.stop()
    assert logger._cleanup_thread is None


def test_default_cleanup_interval_is_24_hours(tmp_path) -> None:
    """生产默认间隔是 24 小时（对应 Swift 的 24*3600）。"""
    logger = SDMLogger(log_dir=tmp_path / "cleanup_logs", cleanup_enabled=True)
    assert logger._cleanup_interval == 24 * 3600


def test_cleanup_disabled_under_pytest(tmp_path) -> None:
    """pytest 进程里定时清理必须关着。

    `get_logger()` 在模块 import 时就起单例，定时器随之启动。
    清理会**删文件**——测试绝不能让它碰用户真实的日志目录。
    """
    logger = SDMLogger()
    assert logger._cleanup_enabled is False, "pytest 下清理没关掉"
    logger.start_cleanup_timer()
    assert logger._cleanup_thread is None, "pytest 下定时器不该启动"


# ====================================================================
# F123 在 Console.app 中打开
# ====================================================================


def test_open_log_in_console_uses_open_command(tmp_path, monkeypatch) -> None:
    """macOS 上执行 `open -a Console <path>`。

    用 mock 拦下 subprocess.run，断言命令和路径都对。
    """
    from sdm import system_integration

    target = tmp_path / "sdm.log"
    target.write_text("hello\n", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return None

    monkeypatch.setattr(system_integration.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(system_integration.subprocess, "run", _fake_run)

    assert open_log_in_console(target) is True
    assert len(calls) == 1
    assert calls[0] == ["open", "-a", "Console", str(target)]


def test_open_log_in_console_returns_false_on_other_platforms(tmp_path, monkeypatch) -> None:
    """非 macOS 返回 False，且不执行任何命令。"""
    from sdm import system_integration

    target = tmp_path / "sdm.log"
    target.write_text("hello\n", encoding="utf-8")

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return None

    monkeypatch.setattr(system_integration.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system_integration.subprocess, "run", _fake_run)

    assert open_log_in_console(target) is False
    assert calls == [], "非 macOS 不该执行 open 命令"


def test_open_log_in_console_returns_false_for_missing_file(tmp_path, monkeypatch) -> None:
    """文件不存在时返回 False，不弹 Console。"""
    from sdm import system_integration

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return None

    monkeypatch.setattr(system_integration.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(system_integration.subprocess, "run", _fake_run)

    assert open_log_in_console(tmp_path / "nope.log") is False
    assert calls == []


def test_open_log_in_console_uses_bundle_id_for_subsystem(tmp_path, monkeypatch) -> None:
    """子系统 ID 来自 version.BUNDLE_ID，不硬编码。"""
    from sdm import system_integration
    from sdm.version import BUNDLE_ID

    target = tmp_path / "sdm.log"
    target.write_text("hello\n", encoding="utf-8")

    logged: list[str] = []

    class _FakeLogger:
        def debug(self, category, message):
            logged.append(message)

        def info(self, category, message):
            logged.append(message)

        def warning(self, category, message):
            logged.append(message)

    monkeypatch.setattr(system_integration.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(system_integration.subprocess, "run", lambda args, **kw: None)
    monkeypatch.setattr(system_integration, "log", _FakeLogger())

    assert open_log_in_console(target) is True
    assert any(BUNDLE_ID in msg for msg in logged), (
        f"日志里没带上子系统 ID {BUNDLE_ID}：{logged}"
    )
