"""下载流：一次完整下载的编排。

对应 Swift 版 Engine/DownloadFlow.swift。

流程：
1. 探测（拿总长度、是否支持 Range、真实文件名）。
2. 规划分片；扫盘上已有的分片临时文件，把已下载的字节计入。
3. 并发跑分片 worker，字节边收边落盘。
4. 全部分片完成后合并成目标文件，写 sidecar。

**为什么第 2 步要扫盘**：暂停/取消/崩溃都会留下分片临时文件。
不扫盘的话，续传会从 0 开始重下，续传功能等于没有。
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..logging_setup import CAT_ENGINE, get_logger
from .content_sniffer import ContentProbe, ProbeError
from .file_assembler import FileAssembler
from .http_downloader import DownloadError, DownloadFailure, HTTPDownloader
from .progress_channel import ChunkProgress, ProgressChannel
from .range_planner import Chunk, RangePlanner, RangeStrategy, SpeedSample
from .resume_state import ResumeState
from .speed_limiter import SpeedLimiter

log = get_logger()


class FlowError(Exception):
    """下载流程的错误分类。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class FlowFailure(enum.Enum):
    PROBE_FAILED = "probe_failed"
    NO_CONTENT_LENGTH = "no_content_length"
    NOT_SEGMENTABLE = "not_segmentable"
    PARTIAL = "partial"
    MERGE_FAILED = "merge_failed"
    SIDECAR_FAILED = "sidecar_failed"
    EXPIRED_LINK = "expired_link"
    CANCELLED = "cancelled"


@dataclass
class FlowOutcome:
    destination: Path
    bytes_written: int
    elapsed: float


class DownloadFlow:
    """一次下载的完整执行体。

    用法：
        flow = DownloadFlow(url=..., destination=...)
        await flow.run()
    """

    def __init__(
        self,
        url: str,
        destination: Path,
        strategy: RangeStrategy | None = None,
        parallelism_cap: int = 32,
        limiter: SpeedLimiter | None = None,
        downloader: HTTPDownloader | None = None,
        progress_channel: ProgressChannel | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.destination = Path(destination)
        self.strategy = strategy or RangeStrategy()
        self.parallelism_cap = parallelism_cap
        self.limiter = limiter or SpeedLimiter(0)
        self.downloader = downloader or HTTPDownloader()
        self.progress_channel = progress_channel or ProgressChannel()
        self.headers = headers or {}
        self._cancelled = False
        self._chunk_tasks: set[asyncio.Task] = set()
        #: 规划好的分片布局。详情面板要展示真实的分片状态，
        #: Swift 版这里是「合成的」（假的），这里是真的。
        self.planned_chunks: list[Chunk] = []
        #: 分片进度回调。引擎用它把分片状态同步到任务快照。
        self.on_chunk_progress: Callable[[list[dict]], None] | None = None

    def cancel(self) -> None:
        """请求中止当前下载。

        置标志 + 取消所有在途的分片任务。标志给「还没起跑」的分片用，
        取消给「已经在传」的分片用——两者都需要，光设标志的话
        已经在传的分片会把整个文件下完（实测踩过）。
        """
        self._cancelled = True
        for task in list(self._chunk_tasks):
            if not task.done():
                task.cancel()

    async def run(self) -> FlowOutcome:
        started = time.monotonic()

        # 1. 探测
        try:
            probe = await self.downloader.probe(self.url)
        except ProbeError as err:
            # 403/410/451 是链接失效，不是探测出错——任务要进
            # 「等待新链接」状态，已下载的字节要留着。
            if err.is_expired_link:
                raise FlowError(FlowFailure.EXPIRED_LINK.value, err.message) from err
            raise FlowError(FlowFailure.PROBE_FAILED.value, err.message) from err
        except Exception as err:  # noqa: BLE001
            raise FlowError(FlowFailure.PROBE_FAILED.value, str(err)) from err

        content_length = probe.content_length
        if not content_length or content_length <= 0:
            raise FlowError(FlowFailure.NO_CONTENT_LENGTH.value, "服务器没有返回 Content-Length")

        # 探测时跟随了重定向的话，分片请求必须用**最终 URL**。
        # 否则每个分片都要重走一遍重定向链——CDN 会把半开的
        # 连接留着，后续分片全排在它后面，表现就是「只有一个
        # 分片在动」。
        download_url = probe.url

        # 2. 规划分片
        planner = RangePlanner(self.strategy)
        planner.start_plan(content_length)
        chunks = planner.all_chunks()

        # 扫盘上已有的分片临时文件，把已下载的字节计入。
        pre_existing = 0
        for chunk in chunks:
            partial = self._partial_path(chunk.id)
            if partial.exists():
                size = partial.stat().st_size
                cap = chunk.end - chunk.start
                clamped = min(size, cap)
                if clamped > 0:
                    chunk.bytes_done = clamped
                    pre_existing += clamped
        planner.replace_chunks(chunks)

        # 预规划重划分。
        # Swift 版的 `RangePlanner.rearrange()` 从来没有调用方，
        # 所以「动态分裂合并」这个特性实际从未运行过。这里真正接上，
        # 但只在**任何字节落盘之前**做——那时改变分片布局是安全的，
        # 不会出现「split 后两半 bytesDone 重复计算」的数据损坏。
        # 传输开始后再重划分就有风险了，所以那时不再调整布局，
        # 只按原计划跑完。
        chunks = self._replan_before_transfer(planner, chunks, content_length)
        if not chunks:
            raise FlowError(FlowFailure.NO_CONTENT_LENGTH.value, "分片规划为空")

        # 需要下载的分片。
        #
        # 这里**不能**按 parallelism_cap 截断——分片数大于并发上限时，
        # 被截掉的分片就永远没人下载，文件会缺一截。
        # Swift 版就有这个洞（`active` 取前 N 个，剩下的丢掉了）。
        # 正确做法是：分片数固定，并发上限只控制「同时开几个连接」，
        # 用信号量限流，任务照跑完。
        self.planned_chunks = list(chunks)
        active = [c for c in chunks if c.bytes_done < c.size]

        assembler = FileAssembler(self.destination)
        for chunk in active:
            assembler.open_chunk(chunk.id, chunk.start)

        if pre_existing > 0:
            self.progress_channel.report(pre_existing)

        # 3. 并发跑分片
        chunk_progress = ChunkProgress(self.progress_channel)
        try:
            await self._run_chunks(download_url, active, assembler, chunk_progress, probe)
        except FlowError:
            raise
        except DownloadError as err:
            if err.is_expired_link:
                raise FlowError(FlowFailure.EXPIRED_LINK.value, err.detail) from err
            raise FlowError(FlowFailure.PARTIAL.value, str(err)) from err
        except asyncio.CancelledError:
            raise FlowError(FlowFailure.CANCELLED.value, "任务被取消") from None

        # 4. 合并 + sidecar
        try:
            await asyncio.to_thread(assembler.finalize, [c.id for c in chunks])
        except Exception as err:  # noqa: BLE001
            raise FlowError(FlowFailure.MERGE_FAILED.value, str(err)) from err

        state = ResumeState.from_probe(probe, self.destination.name, chunks)
        try:
            state.write(self.destination)
        except OSError as err:
            raise FlowError(FlowFailure.SIDECAR_FAILED.value, str(err)) from err

        self.progress_channel.flush()
        self.progress_channel.finish()

        elapsed = time.monotonic() - started
        log.notice(
            CAT_ENGINE,
            f"下载完成 {self.destination.name} bytes={content_length} elapsed={elapsed:.2f}s",
        )
        return FlowOutcome(destination=self.destination, bytes_written=content_length, elapsed=elapsed)

    def _replan_before_transfer(
        self,
        planner: RangePlanner,
        chunks: list[Chunk],
        content_length: int,
    ) -> list[Chunk]:
        """传输开始前的布局调整。

        规则：分片数与单片大小的关系明显不合理时（比如文件很大但分片
        太少，或文件很小却切了太多片），重新规划一次。
        这里刻意**不**做运行期的 split/merge——那需要重开正在传输的
        工作单元，Swift 版正因为没接线才没踩到这个坑；我们不重复它。
        """
        # 已经有字节落盘 → 说明是续传，布局必须保持原样。
        # 关键：分片 id 是 partial 文件名的一部分（sdm.partial-<id>），
        # 改布局就等于改 id，会让盘上已有的字节归属错乱。
        # 所以只要扫到任何非空 partial，就绝不重划。
        if any(c.bytes_done > 0 for c in chunks):
            return chunks

        n = len(chunks)
        if n == 0 or content_length <= 0:
            return chunks

        # 单片实际大小 vs 策略期望的大小
        actual_chunk_size = content_length // n
        target_chunk_size = self.strategy.initial_chunk_size

        # 偏离太多才重划：
        #   片太大 → 每片超过目标的 4 倍，连接利用率低（串行段太长）
        #   片太小 → 每片不到目标的 1/4，开销占比高
        too_big = actual_chunk_size > target_chunk_size * 4
        too_small = actual_chunk_size < target_chunk_size // 4
        if not (too_big or too_small):
            return chunks

        # 按目标片大小重算分片数
        desired = max(1, min(self.strategy.max_chunks, content_length // target_chunk_size))
        desired = min(desired, self.parallelism_cap)
        if desired == n:
            return chunks

        planner2 = RangePlanner(self.strategy)
        planner2.start_plan(content_length, chunk_count=desired)
        new_chunks = planner2.all_chunks()
        if len(new_chunks) == n:
            return chunks

        log.notice(
            CAT_ENGINE,
            f"重新规划分片: {n} → {len(new_chunks)} 片"
            f"（文件 {content_length} 字节，单片 {actual_chunk_size} → {content_length // len(new_chunks)}）",
        )
        return new_chunks

    async def _run_chunks(
        self,
        url: str,
        active: list[Chunk],
        assembler: FileAssembler,
        chunk_progress: ChunkProgress,
        probe: ContentProbe,
    ) -> None:
        """并发跑分片。任一分片失败则整体失败。"""
        semaphore = asyncio.Semaphore(self.parallelism_cap)

        async def worker(chunk: Chunk) -> None:
            async with semaphore:
                if self._cancelled:
                    raise asyncio.CancelledError()
                headers = dict(self.headers)
                result = await self.downloader.download_chunk(
                    url=url,
                    chunk=chunk,
                    assembler=assembler,
                    progress=lambda n: chunk_progress.report(n),
                    headers=headers,
                    limiter=self.limiter,
                )
                self._notify_chunk_progress()
                planner_done = result.bytes_written
                # 分片完成：把 planner 里的 bytes_done 顶到末尾。
                for c in active:
                    if c.id == chunk.id:
                        c.bytes_done = chunk.end
                        break

        tasks = [asyncio.create_task(worker(chunk)) for chunk in active]
        self._chunk_tasks = set(tasks)
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            self._chunk_tasks = set()
            self.progress_channel.flush()

    def _notify_chunk_progress(self) -> None:
        """把当前分片状态推给上层（详情面板用）。"""
        if self.on_chunk_progress is None:
            return
        states = [
            {
                "id": c.id,
                "start": c.start,
                "end": c.end,
                "size": c.size,
                "bytes_done": c.bytes_done,
            }
            for c in self.planned_chunks
        ]
        try:
            self.on_chunk_progress(states)
        except Exception:  # noqa: BLE001
            pass

    def _partial_path(self, chunk_id: int) -> Path:
        return self.destination.with_name(self.destination.name + f".sdm.partial-{chunk_id}")
