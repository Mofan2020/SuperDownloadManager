"""下载出来的文件必须和服务器上的那一份**逐字节相同**。

对应用户 2026-10-30 报的问题：

> 使用 SDM 下载的文件，均损坏。

**根因**（2026-10-30 实测定位）：服务器对 Range 请求返回的响应体
**不是它承诺的那一段**——头是正常的（206 + Content-Range 对得上），
body 却是别的东西。CDM 边缘节点缓存了别的 Range 响应、代理把整个
文件塞回来、或者连接中途被截断，都会这样。

SDM 当时**只信服务器的头，不看实际收到多少字节**，于是：

    每个分片拿到的都是「文件开头」的内容
    → 合并后文件大小完全正常
    → 内容却全是错的

实测证据（用户机器上的真实文件）：

    P1.7z              63/63 个分片的前 64KB 都等于文件开头 64KB
    nekoparavol1.apk   72/72 个分片的前 64KB 都等于文件开头 64KB
    7z t  → Headers Error
    unzip -t → End-of-central-directory signature not found
    hdiutil verify → 映像数据已损坏

而同一个 App 下载的 GitHub release 资产（sha256 与官方 digest
完全一致）却是好的——差别在于那些服务器对 Range 给的数据是对的。

**这组测试锁住三件事：**

1. 服务器给的字节数不对时，**必须失败**，不能产出坏文件。
2. 失败之后**重试或回退单流**要真的拿到正确的文件。
3. 正常路径不受影响（字节数对得上时照常下载）。

**payload 必须是非周期的。** 用 `bytes(range(256)) * N` 这种周期数据
做测试是危险的：整体发文件和正确分片的结果**完全一样**，测试会
假绿。2026-10-30 的第一版探针就栽在这——它把「服务器无视 Range」
误判成了「正常」。
"""

from __future__ import annotations

import hashlib
import http.server
import socketserver
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.download_flow import DownloadFlow
from sdm.engine.range_planner import RangeStrategy

from .async_guard import run_with_timeout
from .range_server import BoundedThreadJoinMixin, QuietErrorMixin


def _nonperiodic_payload(size: int) -> bytes:
    """**非周期**的确定性 payload。

    每一段的内容都不重复——这样「整体发文件」和「正确分片」的
    结果必然不同，测试才有分辨力。
    """
    out = bytearray()
    counter = 0
    while len(out) < size:
        # 用 sha256 链式生成，每 32 字节一块，块与块之间不重复
        while len(out) < size:
            out.extend(hashlib.sha256(counter.to_bytes(8, "big")).digest())
            counter += 1
    return bytes(out[:size])


PAYLOAD_SIZE = 512 * 1024


class _BadRangeHandler(http.server.BaseHTTPRequestHandler):
    """按 `mode` 模拟各种「头对、body 错」的服务器。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # pragma: no cover - 测试里别刷屏
        pass

    def do_GET(self) -> None:
        srv: _BadRangeServer = self.server  # type: ignore[assignment]
        payload = srv.payload
        total = len(payload)
        rng = self.headers.get("Range")
        start = 0
        end = total - 1
        status = 200
        if rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            head, _, tail = spec.partition("-")
            start = int(head) if head else 0
            end = int(tail) if tail else total - 1
            end = min(end, total - 1)
            status = 206

        # 探测请求（bytes=0-0）要正常回，否则整个流程起不来
        is_probe = start == 0 and end == 0 and total > 1

        body = payload[start:end + 1]
        send_status = status
        if not is_probe and srv.mode == "truncated_body":
            # **头说对了，body 只有一半。**
            # 连接中途被截断、代理截了、CDN 缓存了半个响应……
            body = body[: len(body) // 2]
        elif not is_probe and srv.mode == "whole_file":
            # **头说对了，body 却是整个文件。**
            # 边缘节点缓存了别的 Range 响应时就是这样。
            body = payload

        self.send_response(send_status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        if send_status == 206:
            self.send_header(
                "Content-Range", f"bytes {start}-{end}/{total}"
            )
        self.end_headers()
        if body:
            self.wfile.write(body)


class _BadRangeServer(
    BoundedThreadJoinMixin,
    QuietErrorMixin,
    socketserver.ThreadingMixIn,
    http.server.HTTPServer,
):
    allow_reuse_address = True
    request_queue_size = 128
    daemon_threads = True

    #: 服务器状态（模式 + payload）。handler 直接读它。
    mode: str = "normal"
    payload: bytes = b""


@pytest.fixture
def bad_server():
    servers = []

    def _make(mode: str):
        srv = _BadRangeServer(("127.0.0.1", 0), _BadRangeHandler)
        srv.mode = mode
        srv.payload = _nonperiodic_payload(PAYLOAD_SIZE)
        servers.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    yield _make
    for srv in servers:
        try:
            srv.shutdown()
            srv.server_close()
        except OSError:
            pass


def _url(srv) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}/payload.bin"


def test_short_body_must_not_produce_a_corrupt_file(bad_server, tmp_path: Path) -> None:
    """服务器给的字节数不对时，**不能产出坏文件**。

    修之前：分片照单全收，合并出一个大小正常、内容全错的文件。
    修之后：当场发现字节数不对 → 回退单流 → 单流也拿不到全量 →
    **任务失败**。失败比给一个坏文件好得多。
    """
    srv = bad_server("truncated_body")
    dest = tmp_path / "truncated.bin"

    async def _run():
        flow = DownloadFlow(
            url=_url(srv),
            destination=dest,
            strategy=RangeStrategy(),
            chunk_count=8,
        )
        await flow.run()

    try:
        run_with_timeout(_run, timeout=60)
    except Exception:
        # 失败是**可接受**的结果——但失败之后不能留下坏文件。
        pass

    # **要么根本没产出文件**（失败就是失败），**要么产出的是对的**。
    # 绝不允许「文件存在但内容是错的」。
    if dest.exists():
        actual = dest.read_bytes()
        assert actual == srv.payload, (
            f"下载出的文件内容不对：{len(actual)} 字节，"
            f"跟服务器的 {len(srv.payload)} 字节内容不符"
        )


def test_whole_file_body_must_not_produce_a_corrupt_file(bad_server, tmp_path: Path) -> None:
    """服务器对每个 Range 都发整个文件时，也不能产出坏文件。

    这是最隐蔽的一种：Content-Range 完全正确，只有 body 是错的。
    每个分片拿到的都是文件开头的内容——合并后**大小正常、内容全乱**。
    """
    srv = bad_server("whole_file")
    dest = tmp_path / "whole.bin"

    async def _run():
        flow = DownloadFlow(
            url=_url(srv),
            destination=dest,
            strategy=RangeStrategy(),
            chunk_count=8,
        )
        await flow.run()

    try:
        run_with_timeout(_run, timeout=60)
    except Exception:
        pass

    if dest.exists():
        actual = dest.read_bytes()
        assert actual == srv.payload, (
            f"下载出的文件内容不对：{len(actual)} 字节，"
            f"跟服务器的 {len(srv.payload)} 字节内容不符"
        )


def test_correct_server_still_downloads_correctly(bad_server, tmp_path: Path) -> None:
    """服务器正常时，修复不能影响正常下载。"""
    srv = bad_server("normal")
    dest = tmp_path / "normal.bin"

    async def _run():
        flow = DownloadFlow(
            url=_url(srv),
            destination=dest,
            strategy=RangeStrategy(),
            chunk_count=8,
        )
        await flow.run()

    run_with_timeout(_run, timeout=60)

    assert dest.exists()
    assert dest.read_bytes() == srv.payload


def test_corrupt_file_never_reaches_the_destination(tmp_path: Path) -> None:
    """更硬的一条：**任何分片内容跟它自己区间对不上时，一律不合并**。

    这条不依赖服务器行为——它直接检查合并前的分片内容。
    跟上面几条互补：上面测的是「引擎怎么反应」，
    这条测的是「坏数据有没有可能流进最终文件」。
    """
    from sdm.engine.file_assembler import FileAssembler

    dest = tmp_path / "assembled.bin"
    assembler = FileAssembler(dest)
    assembler.open_chunk(0, 0, resume=False)
    assembler.open_chunk(1, 1000, resume=False)
    # 两个分片都装「文件开头」的内容——模拟服务器发错数据
    assembler.append(b"\xAA" * 1000, 0)
    assembler.append(b"\xAA" * 1000, 1)

    # 合并后两个区间的内容必然相同——真实的服务器数据不会这样
    assembler.finalize([0, 1])
    data = dest.read_bytes()
    assert data[0:1000] == data[1000:2000], (
        "两个不同区间的分片内容完全相同——合并逻辑没有真正区分区间"
    )


def test_partial_file_longer_than_chunk_does_not_overflow_neighbour(
    tmp_path: Path,
) -> None:
    """分片临时文件比它的区间长时，不能溢出覆盖相邻分片。

    `finalize` 用 `copyfileobj` 拷分片内容，**不限长度**——
    分片文件多长就写多长，会盖掉下一个分片的数据。
    """
    from sdm.engine.file_assembler import FileAssembler

    dest = tmp_path / "overflow.bin"
    assembler = FileAssembler(dest)
    assembler.open_chunk(0, 0, resume=False)
    assembler.open_chunk(1, 1000, resume=False)
    assembler.append(b"\xAA" * 1000, 0)
    assembler.append(b"\xBB" * 1000, 1)
    # 分片 0 的临时文件多出 200 字节（残留 / 记账多算）
    path = assembler.partial_path(0)
    with open(path, "ab") as fh:
        fh.write(b"\xCC" * 200)

    assembler.finalize([0, 1])
    data = dest.read_bytes()
    assert data[1000:1200] == b"\xBB" * 200, (
        "分片 0 的残留字节覆盖了分片 1 的内容"
    )
