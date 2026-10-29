"""「继续下载」必须真的从断点接着下。

对应用户 2026-10-29 报的问题：

> 如果原来下载失败了，继续下载时好像不能从中断的地方继续，
> 而是重新开始了。

**判据只有一个：服务器又发了多少字节。** 进度条涨满不说明问题
——从头重下也能涨满。测试盯的是「新链接/重试之后，服务器有没有
再发一遍已经下过的区间」。

这组测试还钉住了两个容易漏的坑：

- **弱 ETag / 轮换 ETag 不能把续传打死。** 很多 CDN 每次请求都
  给不同的 ETag，拿它判「资源变了」，用户的每一次「继续下载」
  都会变成「从头重下」。
- **「资源真的变了」不能死循环。** 会话文件跟服务器对不上时，
  必须清掉它并重新开始——不然用户点一次重试失败一次，永远
  恢复不了。
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_engine import DownloadEngine, StartSpec
from sdm.engine.session_state import SessionState

from .async_guard import run_with_timeout
from .link_server import LinkServer

# 256KB 的文件、2 个初始分片 → 每个分片 128KB。
CHUNK_BOUNDARY = 128 * 1024


@pytest.fixture
def payload():
    block = bytes(range(256)) * 8
    out = bytearray()
    while len(out) < 256 * 1024:
        out.extend(block[: min(len(block), 256 * 1024 - len(out))])
    return bytes(out)


def _downloaded_regions(destination: Path) -> list[tuple[int, int]]:
    """盘上已经下好的字节区间（按会话文件里的布局算）。"""
    from sdm.engine import work_dir

    session = SessionState.read(destination)
    if session is None:
        return []
    regions: list[tuple[int, int]] = []
    for chunk in session.chunks:
        path = work_dir.adopt_legacy_partial(destination, chunk.id)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size <= 0:
            continue
        regions.append((chunk.start, chunk.start + size - 1))
    return regions


def _overlaps(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> bool:
    for a_start, a_end in a:
        for b_start, b_end in b:
            if a_start <= b_end and b_start <= a_end:
                return True
    return False


async def _wait_for_state(engine, task_id, states, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        summary = engine.get(task_id)
        if summary is not None and summary.state in states:
            return summary
        await asyncio.sleep(0.02)
    return engine.get(task_id)


def _fast_fail_engine() -> DownloadEngine:
    """引擎 + 短重试预算。

    服务器稳定地返回 404 时，分片会按指数退避重试到 30 秒预算
    才放弃——测试里等不起。把它调到 1 秒。
    """
    engine = DownloadEngine()
    engine.downloader.max_retries = 1
    engine.downloader.chunk_retry_budget = 1.0
    return engine


# -- 1. 普通失败之后，继续下载要接着下 ----------------------------


def test_resume_continues_from_breakpoint(tmp_path, payload):
    """**失败之后点「继续下载」，不能重下已经下过的字节。**

    修好之前的现象：点一下继续，进度条从 0 开始重新涨。
    """
    server = LinkServer(payload, serve_limit=CHUNK_BOUNDARY, fail_status=404)
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = _fast_fail_engine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=server.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            failed = await _wait_for_state(engine, task_id, {"failed"})
            assert failed is not None
            assert failed.state == "failed", failed.state
            assert failed.error_message, "失败必须带原因"

            already = _downloaded_regions(Path(failed.destination))
            assert already, "失败之前一个字节都没下，测不出续传"
            downloaded = sum(b - a + 1 for a, b in already)
            assert downloaded == CHUNK_BOUNDARY, (
                f"下好的字节数不对：{downloaded}"
            )

            # 服务器恢复正常。
            server.serve_limit = None
            server.reset()

            await engine.resume(task_id)
            done = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert done is not None
            assert done.state == "completed", (
                f"继续下载没跑完：state={done.state} error={done.error_message}"
            )
            assert dest.read_bytes() == payload, "合并出来的文件跟源不一致"

            # **关键断言：重试时一个旧字节都没重下。**
            assert not _overlaps(already, server.ranges()), (
                "继续下载把已经下过的字节又下了一遍"
            )
            assert server.total_sent == len(payload) - downloaded, (
                f"服务器又发了 {server.total_sent} 字节，"
                f"实际只需要 {len(payload) - downloaded} 字节"
            )

        run_with_timeout(_inner)
    finally:
        server.stop()


# -- 2. 服务器轮换 ETag 不能把续传打死 ----------------------------


def test_rotating_etag_does_not_break_resume(tmp_path, payload):
    """**服务器每次给不同的 ETag，续传照样要成功。**

    CDN 换节点、换签名 URL 都会这样。拿 ETag 判「资源变了」，
    用户的每一次「继续下载」都会变成「从头重下」——正是要避免的。
    字节比对才是硬证据。
    """
    server = LinkServer(
        payload, etag='"v1"', rotate_etag=True, serve_limit=CHUNK_BOUNDARY,
        fail_status=404,
    )
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = _fast_fail_engine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=server.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            failed = await _wait_for_state(engine, task_id, {"failed"})
            assert failed is not None and failed.state == "failed"
            already = _downloaded_regions(Path(failed.destination))
            assert already, "失败之前一个字节都没下"

            server.serve_limit = None
            server.reset()

            await engine.resume(task_id)
            done = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert done is not None
            assert done.state == "completed", (
                f"ETag 轮换把续传打死了：state={done.state} error={done.error_message}"
            )
            assert dest.read_bytes() == payload
            assert not _overlaps(already, server.ranges()), (
                "ETag 轮换导致已经下过的字节被重下"
            )

        run_with_timeout(_inner)
    finally:
        server.stop()


def test_weak_etag_is_not_used_to_judge_staleness():
    """**弱 ETag（W/"..."）不参与「资源变没变」的判断。**

    RFC 7232：弱校验器只保证语义等价，不保证字节一致，而且明确
    禁止用于 Range 请求。很多 CDN 每次请求都换弱 ETag——拿它判
    「变了」，用户的每次续传都会被清零。
    """
    from sdm.engine.content_sniffer import ContentProbe

    session = SessionState(
        url="http://x/a",
        etag='W/"abc"',
        content_length=100,
    )
    probe = ContentProbe(url="http://x/a", content_length=100, etag='W/"xyz"')
    assert session.staleness(probe) is None, "弱 ETag 变了被判成资源变化"

    # **强 ETag 变了还是要判成变化**——那是真的换了内容。
    strong = SessionState(url="http://x/a", etag='"abc"', content_length=100)
    strong_probe = ContentProbe(url="http://x/a", content_length=100, etag='"xyz"')
    assert strong.staleness(strong_probe) == "etag"

    # 强 ETag 没变 → 没变。
    same = SessionState(url="http://x/a", etag='"abc"', content_length=100)
    same_probe = ContentProbe(url="http://x/a", content_length=100, etag='"abc"')
    assert same.staleness(same_probe) is None


def test_resume_during_task_finalization_still_starts(tmp_path, payload):
    """**任务刚进终态、收尾还没做完时点「继续下载」，必须能启动。**

    状态是在失败分支里写的，`finally`（弹掉 live、清分片句柄）在
    之后。这中间有个窗口——用户在这个窗口里点「继续下载」或者在
    横幅里换链接，早先的实现直接 `return`，等于**什么都没做**：
    任务停在「等待中」永远不动，界面上看着像「点了没反应」。

    链接失效的场景特别容易撞：横幅一出现用户就输新链接，那会儿
    失败分支往往刚把状态写完。

    现场是**造出来的**，不靠时序：任务失败后手动塞一个「还在收尾」
    的 live 进去（task 没 done，状态已是终态）。
    """
    server = LinkServer(payload, serve_limit=CHUNK_BOUNDARY, fail_status=403)
    new = LinkServer(payload, etag='"v2"')
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = _fast_fail_engine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=server.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            waiting = await _wait_for_state(engine, task_id, {"awaiting_new_link"})
            assert waiting is not None
            assert waiting.state == "awaiting_new_link", waiting.state

            # **造出「收尾还没做完」的现场。**
            from sdm.engine.download_engine import LiveTask

            spec = engine._specs[task_id]
            live = LiveTask(task_id, spec)
            live.task = asyncio.create_task(asyncio.sleep(0.4))
            engine.live[task_id] = live
            assert engine.live.get(task_id) is not None

            result = await engine.relink(task_id, new.url)
            assert result["decision"] in ("safeToResume", "unsafeButProceed"), result

            done = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert done is not None
            assert done.state == "completed", (
                f"收尾期间点的换链没启动下载：state={done.state} "
                f"error={done.error_message}"
            )
            assert dest.read_bytes() == payload

        run_with_timeout(_inner)
    finally:
        server.stop()
        new.stop()


def test_stale_partial_file_does_not_block_a_fresh_download(tmp_path, payload):
    """**盘上残留的分片文件不能让新下载把它当成「已经下好的字节」。**

    残留的分片文件很常见：取消过的下载、用户清理目录没清干净、
    **Windows 上文件被句柄占着删不掉**（`unlink` 抛
    PermissionError，重试 20 次也没用）。

    后果有两条，第二条更狠：

    1. 新下载**跳过**那个分片（`start >= chunk.end` 直接返回），
       但规划器里的 `bytes_done` 还是 0 → `all_complete()` 永远不
       成立 → **worker 无限空转，下载永远卡在「下载中」**。
    2. 就算没卡住，合并出来的是**旧内容 + 新内容**的拼接，静默损坏。

    修法：不靠删除，靠**截断**——从头开始的分片把残留清零。
    """
    from sdm.engine import work_dir
    from sdm.engine.download_flow import DownloadFlow

    dest = tmp_path / "payload.bin"
    server = LinkServer(payload)
    try:
        # 造一个残留的分片文件：旧内容、正确的大小（128KB）。
        leftover = work_dir.partial_path(dest, 0)
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(bytes(128 * 1024))

        async def _inner():
            flow = DownloadFlow(url=server.url, destination=dest)
            # **限时**：坏实现下这个调用会永远不返回（worker 空转），
            # 不能让测试挂死——挂死比失败难查得多。
            await asyncio.wait_for(flow.run(), timeout=30)
            assert dest.read_bytes() == payload, (
                "合并出来的文件跟源不一致——残留的分片字节被混进去了"
            )

        run_with_timeout(_inner)
    finally:
        server.stop()


def test_leftover_split_chunks_do_not_corrupt_a_new_download(tmp_path, payload, monkeypatch):
    """**动态分段新切出来的分片，不能把上次的残留当已下好的字节。**

    这个 bug 只在 Windows 上出现过，但它是**真的内容损坏**，不是
    平台差异：

    1. 动态分段会把大分片对半切开，**新切出来的那一片有自己的分片
       文件**（id 更大）。
    2. 这些新分片**不是流程层打开的**——流程层只打开初始那几片。
       新分片是下载器自己打开的，而下载器原来用的是 `resume=True`
       （默认）：盘上同名文件里残留的字节被当成「已经下好的字节」，
       新字节**接在旧字节后面**写。
    3. 合并出来的是「旧内容 + 新内容」的拼接，**长度看着差不多，
       内容全乱**。

    **为什么只有 Windows 中招**：上一轮下载的分片文件在 Windows 上
    删不掉（文件被句柄占着，`unlink` 抛 PermissionError），残留活
    到了下一次下载；macOS 能删掉残留，所以不复现。

    初始分片（id 0、1）是流程层打开的，它按 `chunk.bytes_done > 0`
    判断续传——所以初始分片没问题（见上面那条测试）。**出问题的是
    拆分出来的新分片。**

    这条测试还**把「删不掉」也模拟出来**（`unlink` 对分片文件抛
    PermissionError）——Windows 上就是这个样子。macOS 能删掉残留，
    所以这条路径在 macOS 上永远测不到，**不模拟就没人能提前发现**。
    """
    import pathlib

    from sdm.engine import work_dir
    from sdm.engine.download_flow import DownloadFlow

    dest = tmp_path / "payload.bin"
    server = LinkServer(payload)
    try:
        # **模拟 Windows：分片文件被句柄占着，删不掉。**
        real_unlink = pathlib.Path.unlink

        def _fake_unlink(self, missing_ok=False):
            if ".sdm.partial-" in self.name:
                raise PermissionError(
                    13, "另一个进程正在使用此文件，进程无法访问它"
                )
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(pathlib.Path, "unlink", _fake_unlink)

        # 造出「上一轮下载」留下的分片文件。
        #
        # 初始分片是 0、1，动态分段切出来的往后排——这里多留几个，
        # 不去猜具体切了几刀（切法跟并发调度有关，会变）。
        for chunk_id in range(8):
            leftover = work_dir.partial_path(dest, chunk_id)
            leftover.parent.mkdir(parents=True, exist_ok=True)
            leftover.write_bytes(b"OLD-CONTENT" * 64)

        async def _inner():
            flow = DownloadFlow(url=server.url, destination=dest)
            await asyncio.wait_for(flow.run(), timeout=40)
            got = dest.read_bytes()
            assert len(got) == len(payload), (
                f"长度不对：{len(got)} != {len(payload)}"
            )
            assert _digest(got) == _digest(payload), (
                "内容不对——残留的分片字节被混进去了"
                f"（第一处不同的偏移：{_first_diff(got, payload)}）"
            )

        run_with_timeout(_inner)
    finally:
        server.stop()


def _digest(data: bytes) -> str:
    """内容的短摘要。

    **不要直接断言两个几百 KB 的 bytes 相等**——断言失败时 pytest
    会去算 `difflib` 差异，26 万个元素的序列算差异是平方级的，能把
    CI 作业直接挂死（Windows 上真的挂过一次）。比摘要快得多，
    失败信息也读得懂。
    """
    return hashlib.sha256(data).hexdigest()[:16]


def _first_diff(left: bytes, right: bytes) -> int:
    """第一处不同的偏移（都一样时返回 -1）。"""
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return index
    return len(left) if len(left) != len(right) else -1


# -- 3. 资源真的变了：不能死循环 --------------------------------


def test_changed_resource_clears_session_and_restarts(tmp_path, payload):
    """**服务器上的文件真的换了：清掉会话，重新开始，不能死循环。**

    修好之前：会话文件永远过期 → 每次「重试」都撞同一个
    stale 会话 → 用户永远恢复不了，只能删文件重下。
    """
    server = LinkServer(payload, etag='"v1"', serve_limit=CHUNK_BOUNDARY,
                        fail_status=404)
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = _fast_fail_engine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=server.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            failed = await _wait_for_state(engine, task_id, {"failed"})
            assert failed is not None and failed.state == "failed"
            destination = Path(failed.destination)
            assert SessionState.read(destination) is not None, "会话文件应该还在"

            # **服务器上的文件被换掉了**——长度一样，内容不同，
            # ETag 也变了。
            changed = bytes(reversed(payload))
            server.payload = changed
            server.etag = '"v2"'
            server.serve_limit = None
            server.reset()

            await engine.resume(task_id)
            done = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert done is not None
            # **必须能自己恢复**，不能停在 failed 里等人来救。
            assert done.state == "completed", (
                f"资源变了之后没能自己恢复：state={done.state} "
                f"error={done.error_message}"
            )
            assert dest.read_bytes() == changed, "应该下到新的内容"
            assert SessionState.read(destination) is None, "下完了不该留会话文件"
            # **界面上要能说清楚「进度为什么归零」。**
            #
            # 资源真的变了，本来就只能重下——但用户看到进度从 90%
            # 掉回 0% 会有疑问。引擎要给一个标志，界面据此显示
            # 「已重新开始下载」。
            assert done.stale_restarted, "重新开始过要留标志，界面才知道怎么解释"
            assert done.to_dict()["stale_restarted"] is True

        run_with_timeout(_inner)
    finally:
        server.stop()


def test_retry_task_clears_an_expired_session(tmp_path, payload):
    """**「重试」要能救活一个已经过期的会话。**

    会话文件在盘上（用户上次退出软件时留下的），但服务器上的
    文件已经变了。这时候点「重试」必须一次点通，而不是失败。
    """
    server = LinkServer(payload, etag='"v1"', serve_limit=CHUNK_BOUNDARY,
                        fail_status=404)
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = _fast_fail_engine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=server.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            failed = await _wait_for_state(engine, task_id, {"failed"})
            assert failed is not None and failed.state == "failed"
            server.payload = bytes(reversed(payload))
            server.etag = '"v2"'
            server.serve_limit = None
            server.reset()

            await engine.retry_task(task_id)
            done = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert done is not None
            assert done.state == "completed", (
                f"重试没救回来：state={done.state} error={done.error_message}"
            )
            assert dest.read_bytes() == server.payload

        run_with_timeout(_inner)
    finally:
        server.stop()
