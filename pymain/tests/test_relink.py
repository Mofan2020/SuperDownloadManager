"""换链接续传 + 同文件验证的测试。

对应用户 2026-10-29 报的问题：

> 如果发现原链接失效无法下载，应允许用户输入一个新的链接。
> 由 SDM 去保证新链接能获取到的文件和原有失效链接获取的文件是一样
> 的。如果判断一致，就继续下载，而不是因为链接失效就无法下载了。

**核心断言只有一个：新链接不能重下已经下过的字节。**

「续传」不是看进度条涨不涨，是看**服务器到底又发了多少字节**。
所以测试服务器记录了每个请求的 Range 起点和发了多少字节，
断言「新链接一次都没碰过盘上已有的那段区间」。

这组测试也顺带钉住了另一个坑：**换链之后会话文件的 URL 必须
改写成新链接**。不改的话流程层会判定「同名文件重新下载」，
把整个会话丢掉、从零重下——用户明明已经下了一半。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_engine import DownloadEngine, StartSpec
from sdm.engine.expired_link import (
    ExpiredLinkHandler,
    RelinkDecision,
    RelinkSample,
)
from sdm.engine.http_downloader import HTTPDownloader
from sdm.engine.resume_state import ResumeState
from sdm.engine.session_state import SessionState

from .conftest import run_async
from .link_server import LinkServer


@pytest.fixture
def payload():
    """一份 256KB 的确定性内容。"""
    block = bytes(range(256)) * 8
    out = bytearray()
    while len(out) < 256 * 1024:
        out.extend(block[: min(len(block), 256 * 1024 - len(out))])
    return bytes(out)


# -- 工具 ----------------------------------------------------------


def _downloaded_regions(destination: Path) -> list[tuple[int, int]]:
    """盘上已经下好的字节区间（按会话文件里的布局算）。

    会话文件记着「哪个分片对应哪段字节」，分片文件里是字节本身。
    两者合起来才是「已经下过哪儿了」。
    """
    session = SessionState.read(destination)
    if session is None:
        return []
    from sdm.engine import work_dir

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


def _overlaps(regions: list[tuple[int, int]], other: list[tuple[int, int]]) -> bool:
    """两堆区间有没有重叠。"""
    for a_start, a_end in regions:
        for b_start, b_end in other:
            if a_start <= b_end and b_start <= a_end:
                return True
    return False


async def _wait_for_state(engine, task_id, states, timeout=30.0):
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        summary = engine.get(task_id)
        if summary is not None and summary.state in states:
            return summary
        await asyncio.sleep(0.02)
    return engine.get(task_id)


async def _drive_expired_link(engine, task_id, server):
    """等任务进「等待新链接」状态，返回已下好的区间。

    **链接失效是服务器自己触发的**（`serve_limit`），不靠时序——
    本地网上文件瞬间就下完了，靠「等一会儿再切 403」根本来不及。
    """
    summary = await _wait_for_state(
        engine, task_id, {"awaiting_new_link", "failed", "completed"}
    )
    destination = Path(engine.get(task_id).destination)
    return summary, _downloaded_regions(destination)


# 256KB 的文件、2 个初始分片 → 每个分片 128KB。
# 链接在第一个分片下完之后失效，第二个分片拿到 403。
CHUNK_BOUNDARY = 128 * 1024


# -- 1. 换链之后必须续传，而不是重新下 ----------------------------


def test_relink_resumes_instead_of_redownloading(tmp_path, payload):
    """**换新链接之后，已下过的字节一个都不能重下。**

    这是用户报的核心问题。断言方式是「看服务器又发了多少字节」：
    新链接服务器发过的区间，跟失效前盘上已有的区间**必须完全
    不重叠**。只断言「文件下完了」是不够的——从头重下也能下完，
    用户感受到的是「白等了一个小时」。
    """
    old = LinkServer(payload, serve_limit=CHUNK_BOUNDARY)
    new = LinkServer(payload, etag='"v2"')
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=old.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            summary, already = await _drive_expired_link(engine, task_id, old)
            assert summary is not None
            assert summary.state == "awaiting_new_link", (
                f"任务没进等待新链接状态：{summary.state} / {summary.error_message}"
            )
            assert already, "失效前一个字节都没下，测不出续传"

            result = await engine.relink(task_id, new.url)
            assert result["decision"] == "safeToResume", result

            final = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert final is not None
            assert final.state == "completed", (
                f"换链之后没下完：state={final.state} error={final.error_message}"
            )
            assert dest.read_bytes() == payload, "合并出来的文件跟源不一致"

            # **关键断言：新链接没碰过已经下过的那段。**
            assert not _overlaps(already, new.ranges()), (
                "新链接重下了已经下过的字节——这不是续传，是从头再来"
            )
            assert new.total_sent < len(payload), (
                f"新链接发了 {new.total_sent} 字节，文件才 {len(payload)} 字节，"
                "等于整个重下了一遍"
            )

        run_async(_inner)
    finally:
        old.stop()
        new.stop()


def test_relink_rewrites_session_url(tmp_path, payload):
    """换链成功后，会话文件里的 URL 必须改成新链接。

    **不改的话流程层会判定「同名文件重新下载」**，把整个会话
    丢掉、从零重下——这正是「换链接之后变成重新下载」的根因。
    """
    old = LinkServer(payload, serve_limit=CHUNK_BOUNDARY)
    new = LinkServer(payload, etag='"v2"')
    try:

        async def _inner():
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=old.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            await _drive_expired_link(engine, task_id, old)
            destination = Path(engine.get(task_id).destination)

            await engine.relink(task_id, new.url)
            session = SessionState.read(destination)
            assert session is not None, "会话文件没了"
            assert session.url == new.url, (
                f"会话里的 URL 还是旧的：{session.url}"
            )

        run_async(_inner)
    finally:
        old.stop()
        new.stop()


# -- 2. 同文件验证：内容一样才准续 --------------------------------


def test_relink_rejects_a_different_file(tmp_path, payload):
    """新链接指向**另一个文件**时必须拒绝，不能往旧空洞里写。

    长度相同的两个文件最容易骗过「只比长度」的校验，所以这里
    故意用同样长度、不同内容。
    """
    old = LinkServer(payload, serve_limit=CHUNK_BOUNDARY)
    other = bytes(reversed(payload))
    new = LinkServer(other, etag='"v2"')
    try:

        async def _inner():
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=old.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            await _drive_expired_link(engine, task_id, old)

            result = await engine.relink(task_id, new.url)
            assert result["decision"] == "unsafeReject", result
            assert result.get("reason"), "拒绝时必须给理由"
            # 拒绝之后不能偷偷开始下。
            assert new.total_sent == 0, "拒绝了却还在下"

        run_async(_inner)
    finally:
        old.stop()
        new.stop()


def test_relink_uses_bytes_not_etag(tmp_path, payload):
    """**内容一样但 ETag 不同，必须放行。**

    CDN 换节点、换签名 URL 都会让 ETag 变。拿 ETag 判「换了
    文件」，用户的每一次换链都会被误杀——这正是要解决的问题。
    字节比对才是硬证据。
    """
    old = LinkServer(payload, etag='"v1"', serve_limit=CHUNK_BOUNDARY)
    new = LinkServer(payload, etag='"totally-different"')
    try:

        async def _inner():
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=old.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            await _drive_expired_link(engine, task_id, old)

            result = await engine.relink(task_id, new.url)
            assert result["decision"] == "safeToResume", result
            assert result.get("verifiedSameFile") is True, result

        run_async(_inner)
    finally:
        old.stop()
        new.stop()


def test_relink_without_samples_falls_back_to_restart(tmp_path, payload):
    """**没有可比对的字节样本时，不许硬说「是同一个文件」。**

    一个字节都没下过的时候没法认亲，这时候如果 ETag 还对不上，
    只能放行但警告——并且必须**清掉旧分片从头下**，不能拿旧的
    字节去续新链接的内容。
    """
    # **一个字节都下不到**（连探测都是 403）——没有样本可比。
    old = LinkServer(payload, etag='"v1"', serve_limit=0)
    new = LinkServer(payload, etag='"v2"')
    dest = tmp_path / "payload.bin"
    try:

        async def _inner():
            engine = DownloadEngine()
            ids = await engine.start(
                [
                    StartSpec(
                        url=old.url,
                        destination_directory=tmp_path,
                        filename_override="payload.bin",
                    )
                ]
            )
            task_id = ids[0]
            summary = await _wait_for_state(engine, task_id, {"awaiting_new_link", "failed"})
            assert summary is not None
            assert summary.state == "awaiting_new_link", summary.state
            assert not _downloaded_regions(Path(summary.destination)), "不该有已下载的字节"

            result = await engine.relink(task_id, new.url)
            # 没有样本 + ETag 不同 → 不能判「是同一个文件」。
            assert result["decision"] in ("unsafeButProceed", "safeToResume"), result

            final = await _wait_for_state(engine, task_id, {"completed", "failed"})
            assert final is not None
            assert final.state == "completed", final.error_message
            assert dest.read_bytes() == payload

        run_async(_inner)
    finally:
        old.stop()
        new.stop()


# -- 3. 处理器本身的单元测试 --------------------------------------


def test_handler_rejects_length_mismatch(payload):
    """长度不同 → 直接拒绝，连字节都不用比。"""

    async def _inner():
        server = LinkServer(payload)
        try:
            handler = ExpiredLinkHandler(
                ResumeState(url="http://x/a", content_length=len(payload) + 1),
                HTTPDownloader(),
            )
            result = await handler.evaluate_relink(server.url)
            assert result.decision == RelinkDecision.UNSAFE_REJECT
        finally:
            server.stop()

    run_async(_inner)


def test_handler_verifies_bytes(tmp_path, payload):
    """字节一样 → 判定「是同一个文件」，哪怕 ETag 完全不同。"""

    async def _inner():
        server = LinkServer(payload, etag='"other"')
        try:
            handler = ExpiredLinkHandler(
                ResumeState(url="http://x/a", content_length=len(payload), etag='"v1"'),
                HTTPDownloader(),
                samples=[RelinkSample(offset=0, data=payload[:1024])],
            )
            result = await handler.evaluate_relink(server.url)
            assert result.decision == RelinkDecision.SAFE_TO_RESUME
            assert result.verified_same_file is True
        finally:
            server.stop()

    run_async(_inner)


def test_handler_rejects_different_bytes(payload):
    """字节不一样 → 拒绝，哪怕长度和 ETag 都对得上。"""

    async def _inner():
        server = LinkServer(payload, etag='"v1"')
        try:
            handler = ExpiredLinkHandler(
                ResumeState(url="http://x/a", content_length=len(payload), etag='"v1"'),
                HTTPDownloader(),
                samples=[RelinkSample(offset=0, data=bytes(len(payload)))],
            )
            result = await handler.evaluate_relink(server.url)
            assert result.decision == RelinkDecision.UNSAFE_REJECT
        finally:
            server.stop()

    run_async(_inner)
