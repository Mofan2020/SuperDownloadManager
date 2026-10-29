"""规格 §1.2 / §1.3 / §1.5 / §1.7 / §5 的引擎行为测试。

这些是**新加的**能力，每一条都要有锁：

- 动态分段（§1.2）：分片布局在下载过程中真的会长。
- 无 Range 回退（§1.2.4）：服务器对 Range 返回 200 → 单流下载。
- 续传校验（§1.3）：资源变了 → `stale_session`，不静默重下。
- 会话文件（§1.3）：分片布局落盘，崩溃后能按原布局接着下。
- 进度增量（§1.1 / §2.1）：文件级进度是所有分片增量的和，
  不会虚高。
- 错误分类（§5.1）：HTTP 状态码 → 类别 + 可重试性。
- 校验和（§1.7）：完成后按选定的算法算出校验值。
- 任务队列（§1.8）：同时活跃的任务数有上限。
- 名字冲突（§5.3）：默认保留两者。
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_engine import (
    NAME_CONFLICT_KEEP_BOTH,
    DownloadEngine,
    StartSpec,
    TaskState,
    TaskSummary,
)
from sdm.engine.download_flow import DownloadFlow, FlowError, FlowFailure
from sdm.engine.errors import ErrorCategory, error_for_status
from sdm.engine.progress_channel import ProgressChannel
from sdm.engine.range_planner import DynamicPlanner, RangeStrategy
from sdm.engine.session_state import SessionState
from tests.conftest import run_async
from tests.range_server import RangeServer


@pytest.fixture
def server():
    """一个本地的 Range 服务器。"""
    srv = RangeServer(total_bytes=256 * 1024)
    yield srv
    srv.stop()


# --------------------------------------------------------------------
# §1.2 动态分段
# --------------------------------------------------------------------


def test_dynamic_planner_splits_largest_incomplete_chunk():
    """空闲连接到来时，最大的未完成分片被对半拆开。"""
    planner = DynamicPlanner(
        RangeStrategy(min_chunk_size=1024, max_chunks=16),
        content_length=8 * 1024,
        initial_chunks=2,
        target_chunks=8,
    )
    assert planner.chunk_count == 2

    before = planner.chunks()
    target = max(before, key=lambda c: c.remaining)
    original_end = target.end

    result = planner.split_largest()
    assert result is not None
    # 拆开后总片数 +1：被拆的那片变短，新片接管后半段。
    assert planner.chunk_count == 3
    assert result.source.id == target.id
    assert result.source.start == target.start
    assert result.source.end == result.split_at == result.chunk.start
    # 新片一直延伸到原来那片的末尾——两片合起来正好盖住原区间。
    assert result.chunk.end == original_end
    # 新片可下载（进了待办队列）
    assert planner.take_pending() is not None


def test_dynamic_planner_never_exceeds_the_hard_cap():
    """分片数不超过**硬上限**（`max_chunks`）。

    **不是「目标分片数」。** 以前拿 `target` 当硬上限，那等于
    「分片数不许超过连接数」——空闲连接想切最大的未完成分片都切
    不动，只能下班。下载末尾的活就没人接了，速度掉到 1/N。

    分片数比连接数多是**故意的**：多出来的分片就是给空闲连接
    准备的活干。
    """
    planner = DynamicPlanner(
        RangeStrategy(min_chunk_size=1024, max_chunks=64),
        content_length=64 * 1024,
        initial_chunks=1,
        target_chunks=4,
    )
    while planner.split_largest() is not None:
        pass
    assert planner.chunk_count == 64
    # 分片布局仍然完整覆盖整个文件——拆得再碎也不能有空洞。
    chunks = sorted(planner.chunks(), key=lambda c: c.start)
    assert chunks[0].start == 0
    assert chunks[-1].end == 64 * 1024
    for previous, current in itertools.pairwise(chunks):
        assert current.start == previous.end


def test_dynamic_planner_splits_beyond_the_target_for_idle_connections():
    """**目标分片数不是上限**——空闲连接必须有活干。

    这是长尾的根源：以前分片数一到 `target` 就不再拆，空闲的
    worker 直接退出。下载末尾只剩几分片时，活跃连接一路往下掉。

    这条测试钉的是「拆到 target 之后还能继续拆」。
    """
    planner = DynamicPlanner(
        RangeStrategy(min_chunk_size=1024, max_chunks=64),
        content_length=64 * 1024,
        initial_chunks=1,
        target_chunks=4,
    )
    # 先拆到超过目标分片数。
    splits = 0
    while planner.chunk_count <= 4 and planner.split_largest() is not None:
        splits += 1
    assert splits >= 4, f"拆到目标分片数就停了，只拆了 {splits} 次"
    assert planner.chunk_count > 4, "分片数必须能超过目标分片数"


def test_dynamic_planner_stops_when_chunks_get_too_small():
    """剩下的不够再拆一半时就不拆——碎片化没有意义。"""
    planner = DynamicPlanner(
        RangeStrategy(min_chunk_size=4096, max_chunks=64),
        content_length=16 * 1024,
        initial_chunks=2,
        target_chunks=8,
    )
    # 初始两片各 8192 = 2 × min_chunk_size，能拆；拆完每片 4096，
    # 再拆就不够了（4096 < 2 × 4096）。
    splits = 0
    while planner.split_largest() is not None:
        splits += 1
        assert splits < 10, "不该无限拆下去"
    assert splits == 2
    assert planner.chunk_count == 4


def test_dynamic_download_splits_while_downloading(tmp_path):
    """下载过程中分片数真的会长，而且文件字节完全正确。

    这是最关键的一条：**动态分段如果算错了偏移，合并出来的文件
    大小看着正常但内容全乱**。所以必须逐字节比对。
    """
    total = 512 * 1024
    srv = RangeServer(total_bytes=total, chunk_delay=0.05, delay_per_block=True)
    try:
        dest = tmp_path / "dyn.bin"

        async def _inner():
            flow = DownloadFlow(
                url=srv.url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=64 * 1024,
                    min_chunk_size=32 * 1024,
                    default_chunks=16,
                    max_chunks=16,
                ),
                parallelism_cap=8,
            )
            await flow.run()
            # 动态分段之后分片数会变多——这才是「不是开始时一次性
            # 切死分片」的证据。
            assert flow.chunk_count_now > 2, (
                f"分片数一直是 {flow.chunk_count_now}，动态分段没生效"
            )

        run_async(_inner)
        assert dest.read_bytes() == srv.expected_bytes()
    finally:
        srv.stop()


def test_progress_total_never_exceeds_file_size(tmp_path):
    """文件级进度不能超过文件大小。

    **这条锁的是一个真实 bug。** 分片上报的原来是「本分片累计量」
    而不是「本次增量」，上层求和之后进度会虚高好几倍——2 MB 的
    文件报成 5 MB。原来的测试没抓到，是因为那个测试的分片大小
    正好等于一个数据块（一个分片只有一个块，累计量 == 增量）。
    """
    total = 2 * 1024 * 1024
    srv = RangeServer(total_bytes=total)
    try:
        dest = tmp_path / "progress.bin"
        channel = ProgressChannel()
        seen: list[int] = []

        async def _inner():
            async def consume():
                async for value in channel.stream():
                    seen.append(value)

            consumer = asyncio.create_task(consume())
            flow = DownloadFlow(
                url=srv.url,
                destination=dest,
                strategy=RangeStrategy(
                    initial_chunk_size=256 * 1024,
                    min_chunk_size=64 * 1024,
                    default_chunks=8,
                    max_chunks=8,
                ),
                parallelism_cap=8,
                progress_channel=channel,
            )
            await flow.run()
            await asyncio.wait_for(consumer, timeout=5)

        run_async(_inner)
        assert seen, "应该至少收到一次进度上报"
        assert max(seen) == total, f"进度报成了 {max(seen)}，文件才 {total} 字节"
        assert seen[-1] == total
    finally:
        srv.stop()


# --------------------------------------------------------------------
# §1.2.4 无 Range 回退
# --------------------------------------------------------------------


def test_server_without_range_falls_back_to_single_stream(tmp_path):
    """服务器无视 Range（对 Range 请求回 200）→ 退化成单流下载。

    规格 §1.2.4：「立即回退为单流下载，不进行分块，并在 UI 中
    标注『服务器不支持分块加速』」。
    """
    srv = RangeServer(total_bytes=128 * 1024, ignore_range=True)
    try:
        dest = tmp_path / "norange.bin"

        async def _inner():
            flow = DownloadFlow(
                url=srv.url,
                destination=dest,
                strategy=RangeStrategy(default_chunks=8, max_chunks=8),
                parallelism_cap=4,
            )
            outcome = await flow.run()
            assert outcome.server_supports_ranges is False
            assert flow.server_supports_ranges is False

        run_async(_inner)
        assert dest.exists()
        assert dest.read_bytes() == srv.expected_bytes()
        # 单流模式下不应该有多个分片临时文件
        partials = list(tmp_path.glob("norange.bin.sdm.partial-*"))
        assert len(partials) <= 1, f"回退后还在分块：{[p.name for p in partials]}"
    finally:
        srv.stop()


# --------------------------------------------------------------------
# §1.3 会话文件与续传校验
# --------------------------------------------------------------------


def test_session_file_records_chunk_layout(tmp_path):
    """分片布局要落盘——动态分段的 id 是运行时长的，不记下来没法续传。"""
    state = SessionState(
        url="https://example.com/a.bin",
        etag='"abc"',
        content_length=1000,
        chunks=[
            {"id": 0, "start": 0, "end": 500, "bytes_done": 120},
            {"id": 1, "start": 500, "end": 1000, "bytes_done": 0},
        ],
    )
    dest = tmp_path / "a.bin"
    state.write(dest)

    path = SessionState.path_for(dest)
    assert path.exists(), "会话文件必须落盘"
    loaded = SessionState.read(dest)
    assert loaded is not None
    assert len(loaded.chunks) == 2
    assert loaded.chunks[0].bytes_done == 120
    assert loaded.etag == '"abc"'


def test_session_staleness_detects_etag_change():
    """ETag 变了 → 判定资源已变化。"""
    from sdm.engine.content_sniffer import ContentProbe

    session = SessionState(url="u", etag='"v1"', content_length=100)
    same = ContentProbe(url="u", etag='"v1"', content_length=100)
    changed = ContentProbe(url="u", etag='"v2"', content_length=100)
    assert session.staleness(same) is None
    assert session.staleness(changed) == "etag"


def test_session_staleness_falls_back_to_length():
    """没有 ETag 就看 Last-Modified，再没有就看文件大小。"""
    from sdm.engine.content_sniffer import ContentProbe

    session = SessionState(url="u", content_length=100)
    assert session.staleness(ContentProbe(url="u", content_length=100)) is None
    assert session.staleness(ContentProbe(url="u", content_length=200)) == "content_length"


def test_stale_session_restarts_instead_of_failing(tmp_path, server):
    """资源变了 → **自己从头再下一次**，不把失败甩给用户。

    旧契约是「抛 `stale_session` 给调用方，让用户决定」。用户反馈
    「继续下载失效」之后改了：资源真的变了，那个会话**永久**不可用，
    抛出去的结果就是用户点一次重试失败一次，永远恢复不了。

    现在流程层自己清掉会话和分片，在同一个协程里重新下一次，并且
    用 `stale_restarted` 告诉上层「这次是重新开始的」——界面据此
    显示提示，用户知道进度为什么归零。

    这里用**文件大小**做变化信号：测试服务器不发 ETag，校验时
    走不到 ETag 那一级（两边的信号都得存在才能比）。
    """
    dest = tmp_path / "stale.bin"
    state = SessionState(
        url=server.url,
        content_length=server.total_bytes + 1,
        chunks=[{"id": 0, "start": 0, "end": server.total_bytes, "bytes_done": 0}],
    )
    state.write(dest)

    async def _inner():
        flow = DownloadFlow(url=server.url, destination=dest)
        outcome = await flow.run()
        assert outcome is not None
        assert flow.stale_restarted, "资源变了要记下「重新开始过」"
        # 会话文件已经作废，不能留着。
        assert SessionState.read(dest) is None, "过期的会话文件该清掉"
        # 重新下的是服务器现在这份文件。
        assert dest.read_bytes() == server.expected_bytes()

    run_async(_inner)


def test_stale_session_detected_by_etag_restarts(tmp_path, server):
    """ETag 变了 → 同样自己重新下一次。

    用 `monkeypatch` 换掉探测结果，模拟「服务器上的文件被换掉了」。
    """

    dest = tmp_path / "stale2.bin"
    state = SessionState(
        url=server.url,
        etag='"v1"',
        content_length=server.total_bytes,
        chunks=[{"id": 0, "start": 0, "end": server.total_bytes, "bytes_done": 0}],
    )
    state.write(dest)

    async def _inner():
        flow = DownloadFlow(url=server.url, destination=dest)
        original_probe = flow.downloader.probe

        async def fake_probe(url, extra_headers=None):
            probe = await original_probe(url, extra_headers=extra_headers)
            probe.etag = '"v2"'
            return probe

        flow.downloader.probe = fake_probe  # type: ignore[method-assign]
        await flow.run()
        assert flow.stale_restarted, "ETag 变了要记下「重新开始过」"
        assert SessionState.read(dest) is None
        assert dest.read_bytes() == server.expected_bytes()

    run_async(_inner)


def test_session_file_removed_after_success(tmp_path, server):
    """下载完成后会话文件要删掉——它只是中途的进度。"""
    dest = tmp_path / "done.bin"

    async def _inner():
        flow = DownloadFlow(url=server.url, destination=dest)
        await flow.run()

    run_async(_inner)
    assert dest.exists()
    assert not SessionState.path_for(dest).exists(), "完成后不该留下会话文件"


# --------------------------------------------------------------------
# §5.1 错误分类
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,category,retryable",
    [
        (401, ErrorCategory.AUTH, False),
        (403, ErrorCategory.AUTH, False),
        (404, ErrorCategory.NOT_FOUND, False),
        (416, ErrorCategory.RANGE_ERROR, False),
        (429, ErrorCategory.RATE_LIMITED, True),
        (400, ErrorCategory.CLIENT_ERROR, False),
        (500, ErrorCategory.SERVER_ERROR, True),
        (503, ErrorCategory.SERVER_ERROR, True),
    ],
)
def test_status_maps_to_error_category(status, category, retryable):
    """HTTP 状态码 → 规格 §5.1 的类别和可重试性。"""
    err = error_for_status(status)
    assert err.category == category
    assert err.retryable is retryable


def test_error_category_values_match_spec():
    """类别的字符串值就是规格里的名字——前端和 CLI 直接用它们。"""
    assert str(ErrorCategory.NETWORK) == "network"
    assert str(ErrorCategory.TIMEOUT) == "timeout"
    assert str(ErrorCategory.SERVER_ERROR) == "server_error"
    assert str(ErrorCategory.RATE_LIMITED) == "rate_limited"
    assert str(ErrorCategory.CLIENT_ERROR) == "client_error"
    assert str(ErrorCategory.AUTH) == "auth"
    assert str(ErrorCategory.NOT_FOUND) == "not_found"
    assert str(ErrorCategory.RANGE_ERROR) == "range_error"
    assert str(ErrorCategory.DISK) == "disk"
    assert str(ErrorCategory.STALE_SESSION) == "stale_session"
    assert str(ErrorCategory.NAME_CONFLICT) == "name_conflict"
    assert str(ErrorCategory.UNKNOWN) == "unknown"


def test_retry_after_header_is_parsed():
    """`Retry-After: 120` 要能读出来。"""
    from sdm.engine.errors import parse_retry_after

    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


# --------------------------------------------------------------------
# §1.7 校验和
# --------------------------------------------------------------------


def test_checksum_is_computed_and_recorded(tmp_path, server):
    """完成后按选定的算法算校验值。"""
    import hashlib

    dest = tmp_path / "sum.bin"

    async def _inner():
        flow = DownloadFlow(
            url=server.url,
            destination=dest,
            checksum_algorithm="sha256",
        )
        outcome = await flow.run()
        assert outcome.checksum_algorithm == "sha256"
        expected = hashlib.sha256(server.expected_bytes()).hexdigest()
        assert outcome.checksum == expected

    run_async(_inner)


def test_no_checksum_algorithm_means_no_checksum(tmp_path, server):
    """没选算法就不算——几百 MB 的 SHA-256 要好几秒，不能偷偷花。"""
    dest = tmp_path / "nosum.bin"

    async def _inner():
        flow = DownloadFlow(url=server.url, destination=dest)
        outcome = await flow.run()
        assert outcome.checksum is None

    run_async(_inner)


# --------------------------------------------------------------------
# §1.8 任务队列
# --------------------------------------------------------------------


def test_max_concurrent_tasks_is_enforced(tmp_path, server):
    """同时活跃的任务数不超过上限。"""
    engine = DownloadEngine(max_concurrent_tasks=1)

    async def _inner():
        specs = [
            StartSpec(url=server.url, destination_directory=tmp_path, filename_override=f"q{i}.bin")
            for i in range(3)
        ]
        ids = await engine.start(specs)
        assert len(ids) == 3
        # 立刻检查：只有一个在跑，其余在排队
        downloading = sum(
            1 for t in engine.snapshot() if t.state == TaskState.DOWNLOADING.value
        )
        queued = sum(1 for t in engine.snapshot() if t.state == TaskState.QUEUED.value)
        assert downloading <= 1, f"同时跑了 {downloading} 个任务"
        assert queued >= 2

        for _ in range(300):
            states = [t.state for t in engine.snapshot()]
            if all(s in ("completed", "failed", "cancelled") for s in states):
                break
            await asyncio.sleep(0.05)
        assert all(t.state == TaskState.COMPLETED.value for t in engine.snapshot())

    run_async(_inner)


def test_move_task_reorders_queue(tmp_path):
    """置顶 / 置底（规格 §1.8）。"""
    # 用慢服务器：第一个任务必须一直占着槽位，队列里的顺序才是
    # 可测的。服务器快的话第一个任务秒完，队列自己就动了。
    srv = RangeServer(total_bytes=512 * 1024, chunk_delay=0.2, delay_per_block=True)
    try:
        engine = DownloadEngine(max_concurrent_tasks=1)

        async def _inner():
            specs = [
                StartSpec(
                    url=srv.url,
                    destination_directory=tmp_path,
                    filename_override=f"m{i}.bin",
                )
                for i in range(3)
            ]
            ids = await engine.start(specs)
            # 第一个在跑，后两个在排队
            assert engine.queue_order() == [ids[1], ids[2]], engine.queue_order()

            await engine.move_task(ids[2], "top")
            assert engine.queue_order() == [ids[2], ids[1]], engine.queue_order()

            await engine.move_task(ids[2], "bottom")
            assert engine.queue_order() == [ids[1], ids[2]], engine.queue_order()

            top = engine.get(ids[2])
            assert top is not None
            assert top.queued_position == 1, top.queued_position

        run_async(_inner)
    finally:
        srv.stop()


# --------------------------------------------------------------------
# §5.3 名字冲突
# --------------------------------------------------------------------


def test_existing_file_is_kept_not_overwritten(tmp_path, server):
    """同名文件已存在 → 保留两者，用户原来的文件不动。"""
    existing = tmp_path / "same.bin"
    existing.write_bytes(b"do not touch me")

    engine = DownloadEngine()

    async def _inner():
        spec = StartSpec(
            url=server.url,
            destination_directory=tmp_path,
            filename_override="same.bin",
        )
        ids = await engine.start([spec])
        for _ in range(300):
            task = engine.get(ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        return engine.get(ids[0])

    task = run_async(_inner)
    assert task is not None
    assert task.state == TaskState.COMPLETED.value
    # 原来的文件内容一个字都没变
    assert existing.read_bytes() == b"do not touch me"
    # 新文件另起了一个名字
    assert Path(task.destination).name != "same.bin"
    assert Path(task.destination).exists()


def test_duplicate_download_gets_macos_style_number(tmp_path, server):
    """真下载一次重名文件，落盘的名字要是 macOS 风格。

    **端到端验一次**——上面那条 `test_existing_file_is_kept_not_overwritten`
    只断言「名字不一样了」，没断言「是什么名字」。命名规则是用户
    明确提的需求（「末尾加个空格，然后加编号，最后是点扩展名」），
    得盯死。
    """
    existing = tmp_path / "movie.mkv"
    existing.write_bytes(b"old")

    engine = DownloadEngine()

    async def _inner():
        spec = StartSpec(
            url=server.url,
            destination_directory=tmp_path,
            filename_override="movie.mkv",
        )
        ids = await engine.start([spec])
        for _ in range(300):
            task = engine.get(ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        return engine.get(ids[0])

    task = run_async(_inner)
    assert task is not None
    assert task.state == TaskState.COMPLETED.value
    assert Path(task.destination).name == "movie 1.mkv"
    # 编号在扩展名之前，扩展名还在最后。
    assert Path(task.destination).suffix == ".mkv"
    # 老文件没被动。
    assert existing.read_bytes() == b"old"


def test_name_conflict_policy_default_is_keep_both():
    """默认策略是保留两者。"""
    engine = DownloadEngine()
    assert engine.name_conflict_policy == NAME_CONFLICT_KEEP_BOTH


class TestKeepBothNaming:
    """重名时自动加编号——**格式跟 macOS 一致**。

    macOS 的做法是：在文件名的**末尾**（扩展名之前）加一个空格，
    再加编号，编号表示这是第几个同名文件。也就是 `a 1.zip`、
    `a 2.zip`，不是 `a (1).zip` 也不是 `a-1.zip`。

    跟系统保持一致，用户不用适应第二套规则。
    """

    def test_first_duplicate_gets_number_1(self, tmp_path: Path) -> None:
        from sdm.engine.download_engine import _resolve_keep_both

        (tmp_path / "a.zip").write_bytes(b"x")
        out = _resolve_keep_both(tmp_path / "a.zip")
        assert out.name == "a 1.zip"

    def test_numbers_increment(self, tmp_path: Path) -> None:
        from sdm.engine.download_engine import _resolve_keep_both

        (tmp_path / "a.zip").write_bytes(b"x")
        (tmp_path / "a 1.zip").write_bytes(b"x")
        out = _resolve_keep_both(tmp_path / "a.zip")
        assert out.name == "a 2.zip"

    def test_extension_stays_at_the_end(self, tmp_path: Path) -> None:
        """扩展名必须在最后——编号插在主干和扩展名之间。"""
        from sdm.engine.download_engine import _resolve_keep_both

        (tmp_path / "movie.mkv").write_bytes(b"x")
        out = _resolve_keep_both(tmp_path / "movie.mkv")
        assert out.name == "movie 1.mkv"
        assert out.suffix == ".mkv"

    def test_no_extension_gets_the_number_at_the_end(self, tmp_path: Path) -> None:
        from sdm.engine.download_engine import _resolve_keep_both

        (tmp_path / "README").write_bytes(b"x")
        out = _resolve_keep_both(tmp_path / "README")
        assert out.name == "README 1"

    def test_absent_path_is_returned_unchanged(self, tmp_path: Path) -> None:
        from sdm.engine.download_engine import _resolve_keep_both

        out = _resolve_keep_both(tmp_path / "fresh.zip")
        assert out.name == "fresh.zip"


# --------------------------------------------------------------------
# §1.6 代理
# --------------------------------------------------------------------


def test_global_proxy_is_applied_to_the_client():
    """全局代理要真的进到 httpx 客户端里。

    只存着不生效是最坑的形态——用户在设置里填了代理，界面上看着
    配好了，下载还是直连。
    """
    from sdm.engine.http_downloader import HTTPDownloader

    dl = HTTPDownloader()
    dl.set_proxy("http://127.0.0.1:8888")
    assert dl.proxy == "http://127.0.0.1:8888"
    # 客户端要重建过：旧的没有代理配置。
    client = dl.client
    assert client is not None
    # 再设一次同样的值不会重建（没有意义）
    before = client
    dl.set_proxy("http://127.0.0.1:8888")
    assert dl.client is before


def test_socks_proxy_without_extra_package_fails_loudly():
    """SOCKS 代理缺依赖时要给能看懂的错误，不是 ImportError 堆栈。"""
    from sdm.engine.http_downloader import HTTPDownloader

    dl = HTTPDownloader()
    dl.set_proxy("socks5://127.0.0.1:1080")
    try:
        import httpx_socks  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="httpx-socks"):
            _ = dl.client
    else:
        assert dl.client is not None


def test_task_proxy_override_is_stored():
    """任务级代理覆盖要记在任务上，UI 才显示得出来。"""
    engine = DownloadEngine()

    async def _inner():
        spec = StartSpec(url="https://example.com/a.bin", destination_directory=Path("/tmp"))
        ids = await engine.start([spec])
        await engine.set_task_proxy(ids[0], "http://127.0.0.1:9999")
        task = engine.get(ids[0])
        assert task is not None
        assert task.proxy == "http://127.0.0.1:9999"
        await engine.cancel(ids[0])

    run_async(_inner)


# --------------------------------------------------------------------
# §5.1 任务错误
# --------------------------------------------------------------------


def test_task_error_is_retrievable_with_category(tmp_path, server):
    """失败任务要能拿到结构化错误——UI 的「复制错误详情」用它。"""
    srv = RangeServer(total_bytes=64 * 1024, force_status=404)
    try:
        engine = DownloadEngine()

        async def _inner():
            spec = StartSpec(
                url=srv.url,
                destination_directory=tmp_path,
                filename_override="missing.bin",
            )
            ids = await engine.start([spec])
            for _ in range(300):
                task = engine.get(ids[0])
                if task and task.state in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.05)
            return engine.get(ids[0]), ids[0]

        task, task_id = run_async(_inner)
        assert task is not None
        assert task.state == TaskState.FAILED.value
        assert task.error_category == str(ErrorCategory.NOT_FOUND)
        assert task.error_retryable is False
        error = engine.get_task_error(task_id)
        assert error is not None
        assert error["category"] == str(ErrorCategory.NOT_FOUND)
        assert error["message"]
    finally:
        srv.stop()


def test_retry_task_clears_the_error(tmp_path, server):
    """手动重试要清掉上一次的错误——不然界面上一直挂着旧提示。"""
    engine = DownloadEngine()

    async def _inner():
        spec = StartSpec(
            url=server.url,
            destination_directory=tmp_path,
            filename_override="r.bin",
        )
        ids = await engine.start([spec])
        for _ in range(300):
            task = engine.get(ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        await engine.retry_task(ids[0])
        for _ in range(300):
            task = engine.get(ids[0])
            if task and task.state in ("completed", "failed", "cancelled"):
                break
            await asyncio.sleep(0.05)
        return engine.get(ids[0])

    task = run_async(_inner)
    assert task is not None
    assert task.error_category is None
    assert task.error_message is None


# --------------------------------------------------------------------
# §1.6 浏览器接管的请求头要一路传到引擎
# --------------------------------------------------------------------


def test_dispatcher_passes_browser_headers_into_the_task(tmp_path):
    """扩展带来的 Cookie / UA 要进到 `StartSpec.headers`。

    规格 §1.6：「浏览器接管时自动继承页面请求头是**期望行为**，
    不是可选功能。」扩展把头发过来了，中间任何一段丢掉，
    需要登录态的站点就下不动。
    """
    from sdm.ipc.dispatcher import Dispatcher
    from sdm.ipc.protocol import IPCRequest

    engine = DownloadEngine()
    dispatcher = Dispatcher(engine)

    request = IPCRequest(
        id="1",
        method="add",
        params={
            "url": "https://example.com/a.bin",
            "filename": "a.bin",
            "referer": "https://example.com/page",
            "headers": {
                "Cookie": "session=abc",
                "User-Agent": "SDM-UA/1.0",
                "": "空名字要丢掉",
                "Bad": 123,
            },
        },
    )

    async def _inner():
        response = await dispatcher.dispatch(request)
        assert response.ok, response.error
        task_id = response.result["task_id"]
        spec = engine._specs[task_id]
        await engine.cancel(task_id)
        return spec

    spec = run_async(_inner)
    assert spec.referer == "https://example.com/page"
    assert spec.headers.get("Cookie") == "session=abc"
    assert spec.headers.get("User-Agent") == "SDM-UA/1.0"
    # 脏数据不能进 headers——空名字和非字符串值都丢掉
    assert "" not in spec.headers
    assert "Bad" not in spec.headers


def test_speed_history_records_recent_samples():
    """速度曲线要有真实采样点（规格 §2.3）。

    GUI 之前是自己轮询攒曲线的，daemon 模式下没人攒——引擎必须
    自己是数据源。
    """
    engine = DownloadEngine()
    summary = TaskSummary(id="t1", url="https://example.com/a.bin", state="downloading")
    engine.tasks["t1"] = summary

    engine._apply_speed(summary, delta_bytes=2048, elapsed=0.5)
    engine._apply_speed(summary, delta_bytes=4096, elapsed=0.5)

    history = engine.get_speed_history("t1")
    assert len(history) == 2
    assert all("t" in item and "v" in item for item in history)
    # 时间必须递增——前端直接连成折线
    assert history[0]["t"] <= history[1]["t"]
    assert history[-1]["v"] > 0


def test_speed_history_is_cleared_when_task_removed():
    """任务删掉后不留采样——否则内存里堆的是死任务的曲线。"""
    engine = DownloadEngine()
    summary = TaskSummary(id="t2", url="https://example.com/b.bin", state="downloading")
    engine.tasks["t2"] = summary
    engine._apply_speed(summary, delta_bytes=1024, elapsed=1.0)
    assert engine.get_speed_history("t2")

    run_async(engine.remove, "t2")
    assert engine.get_speed_history("t2") == []


# -- 探测的失败策略 ------------------------------------------------
#
# **用户报的现象**：解析链接时日志里一直在刷「所有重试都失败」，
# 但那个链接在别的下载器里是能下的。
#
# 两个根因：
# 1. 磁力链接（magnet:）httpx 根本解析不了，异常被当成「网络错误」
#    重试 5 次，最后报一句「所有重试都失败」——跟真实原因完全对不上
# 2. 404 这类**不可重试**的失败也重试 5 次，白等 7.5 秒


def test_probe_rejects_magnet_link_with_a_readable_message():
    """磁力链接要立刻失败，并说清楚「不支持 BT」。

    **不能重试**：重试一个协议不支持的地址，重试一万次结果都一样。
    """
    from sdm.engine.content_sniffer import ProbeError

    engine = DownloadEngine()
    result = run_async(
        engine.probe_url,
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
    )
    assert result["ok"] is False
    assert "磁力" in result["error"]
    assert "不支持" in result["error"]
    # 不是网络问题——是地址本身就不支持
    assert result["error_category"] == "client_error"
    assert isinstance(ProbeError("x").is_retryable, bool)


def test_probe_does_not_retry_unsupported_scheme():
    """非 http(s) 协议只试一次。

    计数器证明「只发了一次请求」——真重试的话会是 5 次。
    """
    from sdm.engine import content_sniffer

    calls: list[str] = []

    async def _fake_probe(url, client=None, extra_headers=None):
        calls.append(url)
        raise content_sniffer.ProbeError("不该走到这里", retryable=False)

    # 直接测 content_sniffer 的协议拦截：它在建请求前就抛了
    with pytest.raises(content_sniffer.ProbeError) as excinfo:
        run_async(
            content_sniffer.probe,
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        )
    assert excinfo.value.is_retryable is False
    assert "磁力" in excinfo.value.message
    assert calls == []


def test_probe_does_not_retry_a_404():
    """404 重试多少次都一样——一次就够。"""
    from sdm.engine import content_sniffer

    attempts: list[int] = []

    class _FakeResponse:
        status_code = 404
        headers: ClassVar[dict] = {}
        url = "https://example.com/missing.bin"

        async def aclose(self):
            return None

    class _FakeClient:
        def build_request(self, *args, **kwargs):
            return object()

        async def send(self, request, stream=False):
            attempts.append(1)
            return _FakeResponse()

    async def _run():
        with pytest.raises(content_sniffer.ProbeError):
            await content_sniffer.probe(
                "https://example.com/missing.bin", client=_FakeClient()
            )

    run_async(_run)
    assert len(attempts) == 1, f"404 被重试了 {len(attempts)} 次"


def test_probe_error_retryability_rules():
    """哪些失败值得重试，规则要写死。"""
    from sdm.engine.content_sniffer import ProbeError

    # 值得重试：网络类（没有状态码）
    assert ProbeError("断了").is_retryable is True
    # 值得重试：5xx、429、408
    assert ProbeError("服务器炸了", status=503).is_retryable is True
    assert ProbeError("限流", status=429).is_retryable is True
    assert ProbeError("慢", status=408).is_retryable is True
    # 不值得重试：404 / 416 / 403 这类 4xx
    assert ProbeError("没有", status=404).is_retryable is False
    assert ProbeError("不认 Range", status=416).is_retryable is False
    assert ProbeError("没权限", status=403).is_retryable is False
    # 链接失效同样不重试
    assert ProbeError("失效", status=410).is_expired_link is True
    assert ProbeError("失效", status=410).is_retryable is False
    # 显式指定的说了算
    assert ProbeError("x", retryable=False).is_retryable is False
    assert ProbeError("x", status=404, retryable=True).is_retryable is True


def test_probe_url_error_category_uses_the_status_taxonomy():
    """探测失败要归到规格 §5.1 的类别里，不能一律算 network。

    **不打真网络。** 用本地的 RangeServer 逼一个 404 出来。
    真去请求 example.com 有两个问题：一是 CI 依赖外网，二是
    example.com 哪天对子路径改成返回 200，这条测试就会红——
    而它红了没人知道是为什么。断言也松到 `in (...)`，
    网不通时是 network 错误、网通了是 404，两种都能过，
    等于没测。
    """
    srv = RangeServer(total_bytes=64 * 1024, force_status=404)
    try:
        engine = DownloadEngine()
        result = run_async(engine.probe_url, srv.url)
        assert result["ok"] is False
        # 404 → not_found（不是 network）
        assert result["error_category"] == "not_found"
    finally:
        srv.stop()


def test_probe_error_category_rules():
    """`ProbeError.category` 的规则表。

    **这个类别决定重试策略**（规格 §5.1），所以规则要写死：
    链接失效 → auth；404 → not_found；416 → range_error；
    磁力链接这种地址不支持 → client_error；网络断了 → network。
    """
    from sdm.engine.content_sniffer import ProbeError

    # 链接失效（403/410/451）→ auth
    assert ProbeError("失效", status=410).category is ErrorCategory.AUTH
    # 状态码归类
    assert ProbeError("没有", status=404).category is ErrorCategory.NOT_FOUND
    assert ProbeError("不认 Range", status=416).category is ErrorCategory.RANGE_ERROR
    assert ProbeError("炸了", status=503).category is ErrorCategory.SERVER_ERROR
    # 磁力链接：地址本身不支持，不是网络问题
    assert ProbeError("磁力", retryable=False).category is ErrorCategory.CLIENT_ERROR
    # 其余（网络断了、超时）→ network
    assert ProbeError("断了").category is ErrorCategory.NETWORK


def test_downloading_a_magnet_link_fails_as_client_error(tmp_path):
    """**下载路径**上磁力链接也要归成 client_error，且不可重试。

    之前下载那侧一律按 network 算：任务列表里显示成「网络连接
    中断，将自动重试」，还标着可重试——用户点了重试还是同样的
    结果。而新建任务对话框那边已经归成 client_error 了，两边
    说法不一致。
    """
    flow = DownloadFlow(
        url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        destination=tmp_path / "magnet.bin",
    )
    with pytest.raises(FlowError) as excinfo:
        run_async(flow.run)

    assert excinfo.value.category is ErrorCategory.CLIENT_ERROR
    assert excinfo.value.to_task_error().retryable is False
    assert "磁力" in excinfo.value.detail


def test_rate_limited_probe_failure_stays_retryable():
    """429 是限流，等一会儿就好——必须判成可重试。

    之前 `FlowError.to_task_error()` 自己写了一份类别列表，漏了
    `rate_limited`，429 被判成不可重试。可限流等几分钟就好，
    不该让用户手动重来。
    """
    err = FlowError(
        FlowFailure.PROBE_FAILED.value,
        "慢点",
        category=ErrorCategory.RATE_LIMITED,
        status=429,
    )
    assert err.to_task_error().retryable is True
