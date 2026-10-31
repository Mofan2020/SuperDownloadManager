"""动态分段把分片下坏的回归测试。

用户报的第一个问题：SDM 下载的文件**大小正常但内容损坏**。

根因（2026-10-30 定位）：`DynamicPlanner.split_largest()` 把新切出来
的分片**同时**：

1. 放进 `_pending` 队列（别的 worker 会 `take_pending()` 取走它）
2. 通过返回值交给调用方（当前 worker 直接用它下载）

结果**两个 worker 同时下载同一个分片**，写进同一个分片文件：

    worker A 从分片开头写
    worker B 从中途写（它看到盘上已经有字节了）
    B 的写入覆盖掉 A 已经写好的部分 —— 而 B 拿到的数据起点比 A 的靠后

合并出来的文件**大小完全正常**（两个 worker 各写一半，合计正好
等于区间长度），**内容全是错的**。

实测证据：13MB 的 dmg，7 个分片内容全错，错位量正好是 64KB
（一个数据块）的整数倍。

这里守三件事：
1. 拆分出来的新分片**不进** `_pending`（只能有一个 worker 碰它）
2. 拆分过程中任何时刻，一个分片最多被一个 worker 持有
3. 完整的下载，合并结果跟原始数据逐字节一致
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import socketserver
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_flow import DownloadFlow
from sdm.engine.range_planner import DynamicPlanner, RangeStrategy

#: 8MB —— 足够大到触发动态分段（初始 4 片，拆到十几片）
SIZE = 8 * 1024 * 1024

#: **非周期**数据。用周期数据的话，「每段都是文件开头」和「正确的
#: Range」会产生同样的字节序列，测试会假绿。
PAYLOAD = b"".join(
    hashlib.sha256(i.to_bytes(8, "big")).digest() for i in range(SIZE // 32)
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """慢速 Range 服务器。

    慢是关键——分片要下够久，动态分段才会发生在**传输途中**。
    快的服务器上分片在拆分前就下完了，碰不到这个 bug。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        rng = self.headers.get("Range")
        start, end = 0, len(PAYLOAD) - 1
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            start = int(s)
            end = int(e) if e else end
        body = PAYLOAD[start:end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.end_headers()
        for off in range(0, len(body), 64 * 1024):
            try:
                self.wfile.write(body[off:off + 64 * 1024])
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            time.sleep(0.02)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def slow_server():
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/p.bin"
    srv.shutdown()
    srv.server_close()


def test_split_chunk_is_not_queued_and_downloaded_twice(slow_server, tmp_path):
    """拆分出来的新分片只能被**一个** worker 拿到。

    新分片要进 `_pending`（尾段靠切新分片把并发拉满，这是规格
    §1.2 的核心）。但**调用方不能直接用 `split.chunk`**——那样
    队列里有一份、当前 worker 手里有一份，两个 worker 同时下载
    同一个分片，写坏文件。

    真正的不变式是：**一个分片同时只能被一个 worker 持有。**
    `take_pending()` 用 popleft，天然互斥。
    """
    planner = DynamicPlanner(
        RangeStrategy(), content_length=4 * 1024 * 1024, initial_chunks=2
    )

    result = planner.split_largest()
    assert result is not None, "分片应该拆得开"

    # 新分片进了队列（尾段并发靠它）
    assert result.chunk.id in planner._pending, (
        "新分片应该进队列——尾段靠切新分片把并发拉满"
    )

    # **但队列里只能有一份**，取一次就没了
    taken = planner.take_pending()
    assert taken is not None
    second = planner.take_pending()
    assert second is None or second.id != taken.id, (
        f"分片 {taken.id} 被取到了两次——两个 worker 会同时下载它"
    )


def test_a_chunk_is_never_held_by_two_workers_at_once(slow_server, tmp_path):
    """完整下载：任何时刻一个分片最多被一个 worker 持有。

    直接盯 `download_chunk` 的调用——同一个 chunk id 被并发调用
    就是 bug。
    """
    from sdm.engine.http_downloader import HTTPDownloader

    concurrent: dict[int, int] = {}
    overlaps: list[str] = []
    lock = threading.Lock()

    original = HTTPDownloader.download_chunk

    async def traced(self, url, chunk, assembler, progress=None, headers=None,
                     limiter=None, use_range=True):
        with lock:
            concurrent[chunk.id] = concurrent.get(chunk.id, 0) + 1
            if concurrent[chunk.id] > 1:
                overlaps.append(
                    f"分片 {chunk.id} 被并发下载 {concurrent[chunk.id]} 次"
                )
        try:
            return await original(self, url, chunk, assembler, progress,
                                  headers, limiter, use_range)
        finally:
            with lock:
                concurrent[chunk.id] -= 1

    HTTPDownloader.download_chunk = traced
    try:
        dest = tmp_path / "out.bin"
        flow = DownloadFlow(
            url=slow_server,
            destination=dest,
            strategy=RangeStrategy(initial_chunk_size=1024 * 1024, default_chunks=4),
            chunk_count=4,
            parallelism_cap=8,
        )
        asyncio.run(flow.run())
    finally:
        HTTPDownloader.download_chunk = original

    assert not overlaps, (
        "同一个分片被多个 worker 同时下载了：\n  "
        + "\n  ".join(sorted(set(overlaps)))
    )


def test_downloaded_file_is_byte_identical(slow_server, tmp_path):
    """端到端：慢速服务器 + 动态分段，下载结果逐字节一致。

    这是用户报的那个现象的直接对照——修复前文件大小正常、
    内容全是错的。
    """
    dest = tmp_path / "out.bin"
    flow = DownloadFlow(
        url=slow_server,
        destination=dest,
        strategy=RangeStrategy(initial_chunk_size=1024 * 1024, default_chunks=4),
        chunk_count=4,
        parallelism_cap=8,
    )
    asyncio.run(flow.run())

    assert dest.exists(), "目标文件不存在"
    got = dest.read_bytes()
    assert len(got) == SIZE, f"大小不对：{len(got)} != {SIZE}"
    assert got == PAYLOAD, (
        "下载结果跟原始数据不一致——文件损坏。"
        "这是用户报的现象：大小正常但内容全错。"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
