"""GUI 任务生命周期 + 自动分类 + 全局 UA 的回归测试。

对应用户 2026-10-27 报的三件事：

1. **任务永远卡住、0 字节。** 根因是 `GuiBridge.set_window()` 把
   `_loop` 猜成了 None——引擎的循环引用被抹掉之后，
   `create_task` 退化成 `asyncio.run()` 的一次性循环，下载任务
   在跑第一行之前就被拆循环带走了。这组测试钉住这个根因。
2. **自动分类不生效。** .dmg 的链接应该归到「程序」，不该停在
   「视频」。
3. **UA 要可配置。** 内置默认 + 全局可改 + 站点规则优先。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.categories import (
    DownloadCategory,
    category_for_download,
    category_for_filename,
)
from sdm.engine.content_sniffer import USER_AGENT
from sdm.engine.download_engine import DownloadEngine, TaskSummary
from sdm.engine.http_downloader import HTTPDownloader
from sdm.gui import bridge as bridge_mod
from sdm.gui.bridge import GuiBridge
from sdm.settings import SettingsStore
from sdm.site_rules import SiteRule

from .conftest import run_async
from .range_server import RangeServer

# -- 工具 ----------------------------------------------------------


def _start_loop() -> asyncio.AbstractEventLoop:
    """起一个常驻的事件循环（跟 GUI 里 sdm-engine 线程一样）。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="sdm-test-engine", daemon=True)
    thread.start()
    return loop


def _stop_loop(loop: asyncio.AbstractEventLoop) -> None:
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.05)


def _wait_state(engine: DownloadEngine, task_id: str, timeout: float = 30.0):
    """等任务进终态，返回最后的 summary。"""
    deadline = time.time() + timeout
    summary = engine.get(task_id)
    while time.time() < deadline:
        summary = engine.get(task_id)
        if summary is not None and summary.state in ("completed", "failed", "cancelled"):
            return summary
        time.sleep(0.02)
    return summary


# -- 1. 根因：set_window 不能抹掉引擎循环 ----------------------------


def test_set_window_keeps_the_engine_loop():
    """**`set_window()` 之后引擎循环还得在。**

    窗口是在主线程里建的，主线程上没有 asyncio 循环——
    `asyncio.get_running_loop()` 必然抛 RuntimeError。
    早先的实现把 `_loop` 猜成了 None，于是 `set_loop()` 刚设好的
    引擎循环被抹掉了：任务被扔进一次性循环，`asyncio.run` 一返回
    就被拆掉，用户看到的就是「卡在连接中、0 字节」。
    """
    bridge = GuiBridge()
    loop = _start_loop()
    try:
        bridge.set_loop(loop)
        bridge.set_window(object())
        assert bridge._loop is loop, "set_window 把引擎循环抹掉了"
    finally:
        _stop_loop(loop)


def test_ensure_loop_creates_a_persistent_loop():
    """没人调 set_loop 时，桥接层自己建一个**常驻**循环。

    常驻是刚性的：一次性循环会把 `engine.start()` 里
    create_task 出去的下载任务一起带走。
    """
    bridge = GuiBridge()
    loop = bridge._ensure_loop()
    try:
        assert loop is not None
        assert bridge._ensure_loop() is loop, "第二次调用应该拿同一个循环"
        assert loop.is_running(), "循环没在跑"
    finally:
        _stop_loop(loop)


def test_gui_create_task_actually_downloads(tmp_path):
    """**从 GUI 走一遍：任务必须真的把字节下载下来。**

    这条是整组测试里最关键的一条：它复现的就是用户看到的
    「任务卡住不动、0 字节」。修复前 `create_task` 走的是一次性
    循环，任务在跑第一行之前就被取消，这条会红。
    """
    payload = 256 * 1024
    with RangeServer(total_bytes=payload) as srv:
        engine = DownloadEngine()
        bridge = GuiBridge(engine)
        loop = _start_loop()
        try:
            bridge.set_loop(loop)
            bridge.set_window(object())
            res = bridge.create_task(
                f"{srv.url}/payload.bin",
                {"save_dir": str(tmp_path)},
            )
            assert res.get("ok"), res
            task_id = res.get("task_id")
            assert task_id, "没拿到 task_id"

            # **任务必须挂在那个常驻循环上。**
            # 一次性循环（`asyncio.run`）建的下载任务，
            # `asyncio.run` 一返回就被拆掉——用户看到的是
            # 「任务躺在列表里、0 字节、连 socket 都没建过」。
            live = engine.live.get(task_id)
            assert live is not None and live.task is not None
            assert live.task.get_loop() is loop, "下载任务没挂在引擎的常驻循环上"

            summary = _wait_state(engine, task_id)
            assert summary is not None
            assert summary.state == "completed", (
                f"任务没跑完：state={summary.state} error={summary.error_message}"
            )
            assert summary.bytes_done == payload
            assert (tmp_path / "payload.bin").exists()
            assert (tmp_path / "payload.bin").stat().st_size == payload
        finally:
            _stop_loop(loop)


# -- 2. 自动分类 ----------------------------------------------------


def test_category_for_filename():
    """按文件名的扩展名判分类。"""
    assert category_for_filename("CleanShot X 5.0.2.dmg") == DownloadCategory.SOFTWARE
    assert category_for_filename("movie.mp4") == DownloadCategory.VIDEO
    assert category_for_filename("bundle.zip") == DownloadCategory.ARCHIVES
    assert category_for_filename("song.flac") == DownloadCategory.MUSIC
    # 没有扩展名 / 空 → 判不出来
    assert category_for_filename("download") is None
    assert category_for_filename("") is None
    assert category_for_filename(None) is None  # type: ignore[arg-type]


def test_category_for_download_prefers_real_filename():
    """真实文件名优先于 URL。

    CDN 的 URL 常常没有扩展名，文件名在 Content-Disposition 里。
    """
    # URL 是视频路径、文件名是 dmg → 以文件名为准
    assert (
        category_for_download("http://x/a.mp4", "installer.dmg") == DownloadCategory.SOFTWARE
    )
    # 文件名判不出来 → 退回 URL
    assert category_for_download("http://x/a.mp4", "download") == DownloadCategory.VIDEO
    # 两边都判不出来 → 其他
    assert category_for_download("http://x/download/1", None) == DownloadCategory.OTHER


def test_explicit_category_helper():
    """「自动」和「用户真选了」必须分得清。"""
    assert bridge_mod._explicit_category("auto") is None
    assert bridge_mod._explicit_category("AUTO") is None
    assert bridge_mod._explicit_category("") is None
    assert bridge_mod._explicit_category(None) is None
    assert bridge_mod._explicit_category("video") == "video"
    # 扩展名也认（早期 API 传的是扩展名）
    assert bridge_mod._explicit_category("mp4") == "video"
    # 认不出来的当自动——宁可让引擎猜，也别归到不存在的分类里
    assert bridge_mod._explicit_category("nonsense") is None


def test_apply_category_ignores_auto():
    """**「自动」不能覆盖引擎判出来的分类。**

    用户加一个 .dmg，引擎判成 software；前端如果把下拉框的
    默认值（视频）发过来，就会把它顶掉——用户看到的正是
    「.dmg 却在视频分类里」。
    """
    engine = DownloadEngine()
    bridge = GuiBridge(engine)
    summary = TaskSummary(
        id="t1",
        url="http://x/a.dmg",
        state="queued",
        filename="a.dmg",
        category="software",
    )
    engine.tasks["t1"] = summary

    bridge._apply_category("t1", "auto")
    assert summary.category == "software", "「自动」不该覆盖引擎的分类"

    # 用户真的选了视频 → 以用户为准
    bridge._apply_category("t1", "video")
    assert summary.category == "video"


def test_probe_reports_category_from_content_disposition():
    """探测结果里带上按真实文件名判出来的分类。

    前端拿它自动切换分类下拉（用户没手动改过时）。
    """
    with RangeServer(
        total_bytes=4096,
        content_disposition='attachment; filename="CleanShot X 5.0.2.dmg"',
    ) as srv:
        engine = DownloadEngine()
        res = run_async(engine.probe_url, f"{srv.url}/download/12345")
        assert res.get("ok"), res
        assert res.get("filename") == "CleanShot X 5.0.2.dmg"
        assert res.get("category") == "software"


def test_engine_refines_category_after_probe(tmp_path):
    """引擎探测之后按真实文件名修正分类。

    URL 是无扩展名的 CDN 路径时，探测拿到的文件名才是真相。
    """
    with RangeServer(
        total_bytes=64 * 1024,
        content_disposition='attachment; filename="payload.zip"',
    ) as srv:
        engine = DownloadEngine()
        loop = _start_loop()
        try:
            bridge = GuiBridge(engine)
            bridge.set_loop(loop)
            bridge.set_window(object())
            res = bridge.create_task(
                f"{srv.url}/download/12345",
                {"save_dir": str(tmp_path)},
            )
            assert res.get("ok"), res
            summary = _wait_state(engine, res["task_id"])
            assert summary is not None
            assert summary.category == "archives", summary.category
        finally:
            _stop_loop(loop)


def test_user_chosen_category_is_not_refined(tmp_path):
    """用户手动选过的分类，探测之后也不许改。"""
    with RangeServer(
        total_bytes=64 * 1024,
        content_disposition='attachment; filename="payload.zip"',
    ) as srv:
        engine = DownloadEngine()
        loop = _start_loop()
        try:
            bridge = GuiBridge(engine)
            bridge.set_loop(loop)
            bridge.set_window(object())
            res = bridge.create_task(
                f"{srv.url}/download/12345",
                {"save_dir": str(tmp_path), "category": "video"},
            )
            assert res.get("ok"), res
            summary = _wait_state(engine, res["task_id"])
            assert summary is not None
            assert summary.category == "video", summary.category
        finally:
            _stop_loop(loop)


# -- 3. 全局 UA ----------------------------------------------------


def test_set_user_agent_rebuilds_client():
    """改全局 UA 会重建客户端（UA 写在客户端默认头里）。"""
    dl = HTTPDownloader(user_agent="old-ua")
    old = dl.client
    dl.set_user_agent("new-ua")
    assert dl.user_agent == "new-ua"
    assert dl.client is not old, "换 UA 必须重建客户端"
    assert dl.client.headers["user-agent"] == "new-ua"


def test_empty_user_agent_falls_back_to_builtin():
    """清空输入框 = 恢复内置默认 UA，不是发一个空 UA。"""
    dl = HTTPDownloader(user_agent="something")
    dl.set_user_agent("")
    assert dl.user_agent == USER_AGENT


def test_engine_set_user_agent():
    """引擎能把全局 UA 传到下载器。"""
    engine = DownloadEngine()
    run_async(engine.set_user_agent, "EngineUA/1.0")
    assert engine.downloader.user_agent == "EngineUA/1.0"
    run_async(engine.set_user_agent, "")
    assert engine.downloader.user_agent == USER_AGENT


def test_site_rule_user_agent_beats_global_user_agent(monkeypatch):
    """**站点规则的 UA 优先于全局 UA。**

    规则是用户为某个主机专门配的「跟这个站点说话的方式」，
    全局设置只是兜底。
    """
    dl = HTTPDownloader(user_agent="global-ua", max_retries=0)

    class _FakeStore:
        def match(self, url):
            return SiteRule(id="r1", host="example.com", user_agent="rule-ua")

        def apply_to_headers(self, rule, headers):
            if rule.user_agent:
                headers["User-Agent"] = rule.user_agent
            return headers

        def get_password(self, user, host):
            return None

    monkeypatch.setattr(
        "sdm.site_rules.SiteRuleStore.shared", staticmethod(lambda: _FakeStore())
    )
    got = dl.apply_site_rule("http://example.com/a.zip", {"User-Agent": dl.user_agent})
    assert got["User-Agent"] == "rule-ua"


def test_user_agent_setting_round_trip(tmp_path):
    """全局 UA 能存进设置、也能读回来。"""
    path = tmp_path / "settings.json"
    store = SettingsStore(path=path)
    store.set("network_user_agent", "MyUA/1.0")
    again = SettingsStore(path=path)
    assert again.settings.network_user_agent == "MyUA/1.0"


def test_save_settings_applies_user_agent_to_engine():
    """设置页改 UA → 立刻作用到引擎的下载器上。

    引擎的 setter 是 async（改 UA 等于重建客户端，得在引擎自己的
    事件循环里做），所以这里等一小会儿。
    """
    engine = DownloadEngine()
    bridge = GuiBridge(engine)
    res = bridge.save_settings({"network_user_agent": "GlobalUA/9"})
    assert res.get("ok"), res

    deadline = time.time() + 5.0
    while time.time() < deadline:
        if engine.downloader.user_agent == "GlobalUA/9":
            break
        time.sleep(0.02)
    assert engine.downloader.user_agent == "GlobalUA/9"


def test_client_pool_timeout_is_finite():
    """连接池等待必须有上限。

    `pool=None` 的意思是「等连接池空位时永不超时」——池满了就
    永久挂起，既不报错也不重试。
    """
    dl = HTTPDownloader()
    assert dl.client.timeout.pool is not None
    assert dl.pool_timeout > 0
