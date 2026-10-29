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
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_engine import DownloadEngine, StartSpec
from sdm.engine.session_state import SessionState

from .conftest import run_async
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

        run_async(_inner)
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

        run_async(_inner)
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

        run_async(_inner)
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

        run_async(_inner)
    finally:
        server.stop()
