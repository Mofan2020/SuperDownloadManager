"""下载流：一次完整下载的编排。

对应 Swift 版 Engine/DownloadFlow.swift。

流程：
1. 探测（拿总长度、是否支持 Range、真实文件名）。
2. 恢复：盘上有会话文件就按它的布局续传，并校验资源有没有变。
3. 规划分片；**下载过程中分片布局会持续变化**（规格 §1.2）。
4. 并发跑分片 worker，字节边收边落盘。
5. 全部分片完成后合并成目标文件，写 sidecar，删会话文件。

**为什么第 2 步要扫盘**：暂停/取消/崩溃都会留下分片临时文件。
不扫盘的话，续传会从 0 开始重下，续传功能等于没有。

**为什么第 3 步的布局是动态的**：规格 §1.2 要求「不是开始时一次性
切死分片」。分片布局在下载过程中由 `DynamicPlanner` 持续拆分，
会话文件负责把这个布局记下来，崩溃之后才能原样接着下。
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..logging_setup import CAT_ENGINE, CAT_STORE, get_logger
from . import work_dir
from .content_sniffer import ContentProbe, ProbeError
from .errors import RETRYABLE_BY_DEFAULT, ErrorCategory, TaskError
from .file_assembler import FileAssembler
from .http_downloader import DownloadError, HTTPDownloader
from .progress_channel import ChunkProgress, ProgressChannel
from .range_planner import Chunk, ConnectionGovernor, DynamicPlanner, RangeStrategy
from .resume_state import ResumeState
from .session_state import SessionChunk, SessionState
from .speed_limiter import SpeedLimiter

log = get_logger()


class FlowError(Exception):
    """下载流程的错误分类。"""

    def __init__(
        self,
        kind: str,
        detail: str = "",
        category: ErrorCategory | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        #: 显式指定的错误类别。规格 §5.1 的分类靠它——
        #: 「探测时拿到 404」和「探测时 DNS 解析失败」都是
        #: probe_failed，但前者是 `not_found`、后者是 `network`，
        #: 重试策略完全不一样。
        self._category = category
        self.status = status

    @property
    def category(self) -> ErrorCategory:
        """这个错误对应的规格 §5.1 类别。"""
        if self._category is not None:
            return self._category
        return _FLOW_CATEGORY.get(self.kind, ErrorCategory.UNKNOWN)

    def to_task_error(self) -> TaskError:
        return TaskError(
            category=self.category,
            detail=self.detail or str(self),
            status=self.status,
            # 用共享的默认可重试表，别自己写一份——之前这里漏了
            # `rate_limited`，429（被限流）被判成不可重试，可限流
            # 等一会儿就好，不该让用户手动重来。
            retryable=self.category in RETRYABLE_BY_DEFAULT,
        )


class FlowFailure(enum.Enum):
    PROBE_FAILED = "probe_failed"
    NO_CONTENT_LENGTH = "no_content_length"
    NOT_SEGMENTABLE = "not_segmentable"
    PARTIAL = "partial"
    MERGE_FAILED = "merge_failed"
    SIDECAR_FAILED = "sidecar_failed"
    EXPIRED_LINK = "expired_link"
    CANCELLED = "cancelled"
    #: 服务器不支持分块（对 Range 请求返回 200）——规格 §1.2.4。
    RANGE_UNSUPPORTED = "range_unsupported"
    #: 恢复时资源变了——规格 §1.3。
    STALE_SESSION = "stale_session"
    #: 磁盘写不进去——规格 §5.3。
    DISK_ERROR = "disk_error"


#: 流程级失败 → 规格 §5.1 的错误类别。
_FLOW_CATEGORY: dict[str, ErrorCategory] = {
    FlowFailure.PROBE_FAILED.value: ErrorCategory.NETWORK,
    FlowFailure.NO_CONTENT_LENGTH.value: ErrorCategory.CLIENT_ERROR,
    FlowFailure.PARTIAL.value: ErrorCategory.NETWORK,
    FlowFailure.MERGE_FAILED.value: ErrorCategory.DISK,
    FlowFailure.SIDECAR_FAILED.value: ErrorCategory.DISK,
    FlowFailure.EXPIRED_LINK.value: ErrorCategory.AUTH,
    FlowFailure.RANGE_UNSUPPORTED.value: ErrorCategory.RANGE_ERROR,
    FlowFailure.STALE_SESSION.value: ErrorCategory.STALE_SESSION,
    FlowFailure.DISK_ERROR.value: ErrorCategory.DISK,
}


#: 资源标识的中文名，给「服务器文件已变化」的错误详情用。
_STALE_LABELS = {
    "etag": "ETag",
    "last_modified": "Last-Modified",
    "content_length": "文件大小",
}


def _stale_detail(reason: str) -> str:
    """「资源变了」的错误详情。"""
    label = _STALE_LABELS.get(reason, reason)
    return f"资源标识 {label} 与下载开始时不一致"


@dataclass
class FlowOutcome:
    destination: Path
    bytes_written: int
    elapsed: float
    checksum: str | None = None
    checksum_algorithm: str | None = None
    server_supports_ranges: bool = True


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
        initial_connections: int | None = None,
        chunk_count: int | None = None,
        checksum_algorithm: str | None = None,
        probe: ContentProbe | None = None,
        probe_error: ProbeError | None = None,
    ) -> None:
        self.url = url
        self.destination = Path(destination)
        self.strategy = strategy or RangeStrategy()
        self.parallelism_cap = max(1, parallelism_cap)
        self.limiter = limiter or SpeedLimiter(0)
        self.downloader = downloader or HTTPDownloader()
        self.progress_channel = progress_channel or ProgressChannel()
        self.headers = headers or {}
        #: 初始连接数（规格 §1.2.3「初始连接数保守」）。
        self.initial_connections = max(
            1,
            min(
                initial_connections if initial_connections is not None else self.strategy.initial_chunks,
                self.parallelism_cap,
            ),
        )
        #: 目标分片数。None = 用策略里的 `default_chunks`。
        self.chunk_count = chunk_count
        self.checksum_algorithm = checksum_algorithm
        #: 调用方已经探好的结果。有就直接用，**不再探第二次**。
        #:
        #: 引擎层为了拿真实文件名和总长度会先探一次，流程层
        #: 原先又探一次——两次探测 = 两个 RTT。用户看到的是
        #: 「下载卡在 0 KB/s」，实测这条链路上服务器光建连就
        #: 要 2.5 秒，两次探测就是 5 秒纯等待，还没有任何提示。
        self._probe_hint = probe
        #: 调用方探测时拿到的错误。有就直接抛，**不再探第二次**。
        #:
        #: 探测本身已经带重试和预算了，重复探一次只是把同一个
        #: 错误再等一遍（用户等的是两倍时间，看到的是同一个错）。
        self._probe_error = probe_error
        self._cancelled = False
        self._chunk_tasks: set[asyncio.Task] = set()
        self._assembler: FileAssembler | None = None
        self._planner: DynamicPlanner | None = None
        self._governor = ConnectionGovernor(self.initial_connections, self.parallelism_cap)
        #: 空闲 worker 等新分片的轮询间隔（秒）。
        #:
        #: 拆不动但还没下完时，worker 得等着别人切出新分片。20 毫秒
        #: 足够跟得上，又不至于让几十个 worker 空转烧 CPU。
        self._idle_poll = 0.02
        #: **worker 因为「拆不出新分片」而空等的次数。**
        #:
        #: 这个计数是长尾修复的**直接证据**：worker 空等，说明
        #: 「当前没有可拆的活，但下载还没完」——这一刻如果 worker
        #: 直接退出，那条连接就永久没了，末尾的活只能由剩下的连接
        #: 慢慢磨。修复后它等着，切出新分片的 worker 会把活分过来。
        #:
        #: 暴露出来是为了让测试能盯住这个行为本身（而不是只盯
        #: 「末尾速度够不够快」这种间接指标）。
        self.worker_idle_waits = 0
        #: worker **提前下班**的次数（下载还没完就退出）。
        #:
        #: **这个数必须恒为 0。** 非零就说明还有 worker 在活没干完
        #: 的时候跑了——那正是长尾的成因。
        self.workers_exited_early = 0
        #: 当前活着的 worker 数。
        self._live_workers = 0
        #: 下载**还没完**的时候，存活 worker 数的最低值。
        #:
        #: 这是长尾的直接度量：连接数是随着 worker 一起消失的，
        #: 所以「活着的 worker 越来越少」就等于「速度越来越慢」。
        #: 好的实现里这个数应该一直是满的（= 并发上限），
        #: 直到最后一刻才一起下班。
        self.min_live_workers = 0
        #: 每个分片开始传输的时刻，用于算该分片自己的耗时
        self._chunk_started_at: dict[int, float] = {}
        #: 规划好的分片布局。详情面板要展示真实的分片状态，
        #: Swift 版这里是「合成的」（假的），这里是真的。
        self.planned_chunks: list[Chunk] = []
        #: 分片进度回调。引擎用它把分片状态同步到任务快照。
        self.on_chunk_progress: Callable[[list[dict]], None] | None = None
        #: 服务器支不支持分块。False 时整个任务退化成单流下载。
        self.server_supports_ranges = True
        #: 最近一次失败的结构化错误。
        self.last_error: TaskError | None = None
        #: 最近一次收到字节的时刻。连接数放开要看它——卡住的时候
        #: 加连接没有意义。
        self._last_progress_at = time.monotonic()
        #: 退避过一次的标记——单流回退只做一次。
        self._fell_back = False
        self._probe: ContentProbe | None = None

    # -- 对外状态 ----------------------------------------------------

    @property
    def active_connections(self) -> int:
        """当前同时在传的分片数。详情面板要显示「连接数」。"""
        return self._governor.running

    @property
    def chunk_count_now(self) -> int:
        return self._planner.chunk_count if self._planner else 0

    def cancel(self) -> None:
        """请求中止当前下载。

        置标志 + 取消所有在途的分片任务。标志给「还没起跑」的分片用，
        取消给「已经在传」的分片用——两者都需要，光设标志的话
        已经在传的分片会把整个文件下完（实测踩过）。

        分片临时文件**不删**——暂停（pause）也走这条路，
        暂停要保留分片以便 resume 接着下。真正删除分片是
        引擎层 `cancel()` 的职责。
        """
        self._cancelled = True
        for task in list(self._chunk_tasks):
            if not task.done():
                task.cancel()

    def close_partial_handles(self) -> None:
        """关掉分片文件的句柄。

        引擎层删除分片前调用。Windows 上文件被打开时删不掉
        （PermissionError），POSIX 上删打开的文件只是延迟释放 inode。

        句柄由 FileAssembler 缓存着——分片写完不立刻关，是为了
        同一分片的后续写入不用反复 open。任务结束时没人关它们，
        所以删除前得显式关。
        """
        assembler = self._assembler
        if assembler is None:
            return
        try:
            assembler.close_all()
        except Exception:
            pass

    def discard_partials(self) -> None:
        """删掉分片临时文件（引擎层取消任务时用）。

        公开方法——引擎层要通过它删，不能自己按文件名 glob 删。
        自己 glob 删会漏掉文件句柄：Windows 上文件还开着，
        unlink 直接 PermissionError，分片就留在下载目录里了。

        暂停（pause）不能调这个——暂停要保留分片。
        """
        self._cleanup_partials()
        self._remove_session()

    def _cleanup_partials(self) -> None:
        """删掉所有分片临时文件。

        取消或失败时调用。留着它们的后果是：用户取消后下载目录里
        躺着一堆 `.sdm.partial-N`，看起来像下载到一半的垃圾，
        而且下次同名任务会扫到它们。

        链接失效（403/410/451）**不**清理——那种情况下分片数据
        要留着，用户换新链接后能接着下。
        """
        assembler = self._assembler
        if assembler is None:
            return
        try:
            assembler.cleanup_partials()
        except Exception:
            pass

    # -- 主流程 ------------------------------------------------------

    async def run(self) -> FlowOutcome:
        """跑完一次下载。服务器不支持分块时自动退化成单流。"""
        try:
            return await self._run_once(use_range=True)
        except FlowError as err:
            if err.kind == FlowFailure.RANGE_UNSUPPORTED.value and not self._fell_back:
                # 规格 §1.2.4：服务器对 Range 请求返回 200 → 立刻回退
                # 为单流下载，不进行分块。
                self._fell_back = True
                self.server_supports_ranges = False
                log.notice(
                    CAT_ENGINE,
                    "服务器不支持分块请求（对 Range 返回 200），改用单流下载",
                )
                # 单流重来之前要把分片清干净——它们的区间是按分块
                # 算的，跟单流的内容对不上。
                self._cleanup_partials()
                self._remove_session()
                return await self._run_once(use_range=False)
            raise

    async def _run_once(self, use_range: bool) -> FlowOutcome:
        started = time.monotonic()

        # 1. 探测
        #
        # 调用方探过了就直接用——探测是整个下载里最贵的一步
        # （一次完整 RTT），重复探等于让用户白等一次。
        probe = self._probe_hint
        if probe is None and self._probe_error is not None:
            # 调用方探过了，但失败了。错误直接用它的——
            # 分类、状态码都在里面，再探一次只会让用户等两倍时间。
            err = self._probe_error
            if err.is_expired_link:
                raise FlowError(FlowFailure.EXPIRED_LINK.value, err.message) from err
            raise FlowError(
                FlowFailure.PROBE_FAILED.value,
                err.message,
                category=err.category,
                status=err.status,
            ) from err
        if probe is None:
            try:
                probe = await self.downloader.probe(
                    self.url,
                    # 任务级的头（Referer 等）也要带上——探测阶段
                    # 有些站点就 403，不带头根本起不来。
                    extra_headers=dict(self.headers) or None,
                )
            except ProbeError as err:
                # 403/410/451 是链接失效，不是探测出错——任务要进
                # 「等待新链接」状态，已下载的字节要留着。
                if err.is_expired_link:
                    raise FlowError(FlowFailure.EXPIRED_LINK.value, err.message) from err
                # 类别由 `ProbeError` 自己给（规格 §5.1）。
                #
                # **不能一律按 `network` 算**：404 是「文件不存在」，
                # 503 是「服务器错误」，磁力链接是「这个地址压根不走
                # HTTP」——三者的重试策略完全不同。`ProbeError.category`
                # 跟新建任务对话框用的是同一份，不会出现两边说法不一。
                raise FlowError(
                    FlowFailure.PROBE_FAILED.value,
                    err.message,
                    category=err.category,
                    status=err.status,
                ) from err
            except Exception as err:
                raise FlowError(FlowFailure.PROBE_FAILED.value, str(err)) from err

        self._probe = probe
        # **把探测结果缓存下来。** 回退（不支持 Range / 会话过期）
        # 会再跑一次 `_run_once`，那时候不该再探一次——探测是整个
        # 下载里最贵的一步（一次完整 RTT），重复探等于让用户白等。
        self._probe_hint = probe
        content_length = probe.content_length
        if not content_length or content_length <= 0:
            raise FlowError(FlowFailure.NO_CONTENT_LENGTH.value, "服务器没有返回 Content-Length")

        # 探测时跟随了重定向的话，分片请求必须用**最终 URL**。
        # 否则每个分片都要重走一遍重定向链——CDN 会把半开的
        # 连接留着，后续分片全排在它后面，表现就是「只有一个
        # 分片在动」。
        download_url = probe.url

        # 探测阶段就知道服务器认不认 Range：对 `Range: bytes=0-0`
        # 回 206 就是认，回 200 就是不认（规格 §1.2.4）。
        if probe.range_supported is False:
            use_range = False
            self.server_supports_ranges = False

        # 2. 恢复：盘上有会话文件就按它的布局接着下
        planner, pre_existing = await self._prepare_planner(probe, content_length, use_range)

        # 3. 合并 + sidecar
        assembler = FileAssembler(self.destination)
        self._assembler = assembler
        for chunk in planner.chunks():
            assembler.open_chunk(chunk.id, chunk.start)

        if pre_existing > 0:
            self.progress_channel.report(pre_existing)

        # 4. 并发跑分片
        #
        # `base=pre_existing`：续传时盘上已有的字节要算进汇总基数，
        # 否则第一次上报比盘上已有的还小，进度条会倒退一次。
        chunk_progress = ChunkProgress(self.progress_channel, base=pre_existing)
        session_task = asyncio.create_task(self._persist_session_loop())
        try:
            await self._run_chunks(download_url, planner, assembler, chunk_progress, use_range)
        except FlowError:
            raise
        except DownloadError as err:
            self.last_error = err.to_task_error()
            if err.is_expired_link:
                # 链接失效：分片文件要留着，用户换新链接后能接着下。
                raise FlowError(FlowFailure.EXPIRED_LINK.value, err.detail) from err
            raise FlowError(FlowFailure.PARTIAL.value, str(err)) from err
        except asyncio.CancelledError:
            # 取消要原样往上抛，不能转成 FlowError。
            #
            # 转了的话，调用方（引擎）会走 except FlowError 分支，
            # 把任务状态标成「失败」——暂停/取消就变成了失败。
            # Swift 版就有这个毛病（暂停被报成失败），这里不能重复。
            #
            # CancelledError 在 Python 3.8+ 继承自 BaseException，
            # 不转的话 asyncio 才知道这是「请求取消」而不是「出错」。
            raise
        except OSError as err:
            # 磁盘满、权限拒绝。
            self.last_error = TaskError(
                category=ErrorCategory.DISK,
                detail=str(err),
                retryable=False,
            )
            raise FlowError(FlowFailure.DISK_ERROR.value, str(err)) from err
        finally:
            session_task.cancel()
            self._write_session()

        # 5. 合并
        chunks = planner.chunks()
        try:
            await asyncio.to_thread(assembler.finalize, [c.id for c in chunks])
        except Exception as err:
            raise FlowError(FlowFailure.MERGE_FAILED.value, str(err)) from err

        state = ResumeState.from_probe(probe, self.destination.name, chunks)
        try:
            state.write(self.destination)
        except OSError as err:
            raise FlowError(FlowFailure.SIDECAR_FAILED.value, str(err)) from err

        # 下完了，会话文件没用了。
        self._remove_session()

        checksum = self._compute_checksum()

        self.progress_channel.flush()
        self.progress_channel.finish()

        elapsed = time.monotonic() - started
        log.notice(
            CAT_ENGINE,
            f"下载完成 {self.destination.name} bytes={content_length} elapsed={elapsed:.2f}s",
        )
        return FlowOutcome(
            destination=self.destination,
            bytes_written=content_length,
            elapsed=elapsed,
            checksum=checksum,
            checksum_algorithm=self.checksum_algorithm,
            server_supports_ranges=self.server_supports_ranges,
        )

    # -- 恢复 --------------------------------------------------------

    async def _prepare_planner(
        self,
        probe: ContentProbe,
        content_length: int,
        use_range: bool = True,
    ) -> tuple[DynamicPlanner, int]:
        """决定这次是「续传」还是「从头下」，并给出分片规划。

        返回 `(规划器, 盘上已有的字节数)`。
        """
        session = SessionState.read(self.destination) if use_range else None

        if session is not None:
            if session.url and session.url != self.url and session.url != probe.url:
                # 会话文件是**另一个 URL** 的。同名文件重新下载时
                # 不能拿旧的布局续传——内容根本不是一回事。
                log.notice(CAT_STORE, "会话文件的 URL 与本次不同，重新下载")
                session = None
            elif session.content_length and session.content_length != content_length:
                # 资源大小变了——会话文件里的布局对不上新文件。
                # 这不是能自动修的，得用户决定：重新下载还是强制续传。
                raise FlowError(
                    FlowFailure.STALE_SESSION.value,
                    _stale_detail("content_length"),
                )
            else:
                stale = session.staleness(probe)
                # **「ETag 变了但长度一样」先别急着判死。**
                #
                # 有些服务器每次请求都给不同的强 ETag（按节点生成、
                # 按请求时间生成都见过）。这种情况内容其实没变，
                # 判成 stale 就等于把用户的「继续下载」变成「从头
                # 重下」——正是最该避免的行为。
                #
                # 用字节比对确认：拿盘上已下的那段字节，去服务器取
                # 同样的偏移，逐字节比。一样 = 资源没变，可以续。
                if (
                    stale == "etag"
                    and session.content_length == content_length
                    and not await self._confirm_changed_by_bytes(session, probe)
                ):
                    stale = None
                    log.notice(
                        CAT_STORE,
                        "服务器的 ETag 变了但内容一致，按可续传处理",
                    )
                if stale:
                    log.notice(CAT_STORE, f"会话文件校验不通过（{stale}），需要用户决策")
                    raise FlowError(
                        FlowFailure.STALE_SESSION.value,
                        _stale_detail(stale),
                    )

        if session is None:
            # **单流模式只有一片。** 服务器无视 Range，每个请求都会
            # 把整个文件发过来——分成多片的话每片都写一遍完整文件，
            # 合并出来是乱的。
            initial = 1 if not use_range else self.initial_connections
            target = 1 if not use_range else (self.chunk_count or self.strategy.default_chunks)
            planner = DynamicPlanner(
                self.strategy,
                content_length=content_length,
                initial_chunks=initial,
                target_chunks=target,
            )
            return planner, 0

        # 恢复布局。**字节数以盘上的文件为准**，不以会话文件为准——
        # 文件才是真正能续传的东西，会话文件只是「这段对应哪块字节」
        # 的索引。
        chunks: list[Chunk] = []
        pre_existing = 0
        for session_chunk in session.chunks:
            chunk = Chunk(
                id=session_chunk.id,
                start=session_chunk.start,
                end=session_chunk.end,
                bytes_done=0,
            )
            partial = self._partial_path(chunk.id)
            if partial.exists():
                try:
                    on_disk = partial.stat().st_size
                except OSError:
                    on_disk = 0
                chunk.bytes_done = min(on_disk, chunk.size)
                pre_existing += chunk.bytes_done
            chunks.append(chunk)

        planner = DynamicPlanner(
            self.strategy,
            content_length=content_length,
            initial_chunks=1,
            target_chunks=max(len(chunks), self.chunk_count or self.strategy.default_chunks),
        )
        planner.restore(chunks)
        log.notice(
            CAT_ENGINE,
            f"续传 {self.destination.name}：{len(chunks)} 个分片，已有 {pre_existing} 字节",
        )
        return planner, pre_existing

    async def _confirm_changed_by_bytes(
        self,
        session: SessionState,
        probe: ContentProbe,
    ) -> bool:
        """资源是不是真的变了？

        **用盘上已下载的字节跟服务器现在的字节比一比。**

        判「变了」的依据只有 ETag 时，会被服务器「每次请求换 ETag」
        的行为骗到——那种情况下内容其实一模一样，把用户的续传打成
        「资源已变化」是最糟的结果。字节比对是内容级的，不会骗人。

        返回 True = 确实变了（维持 stale 判断）；
        返回 False = 内容一样，可以续传。

        **一段字节都没比成的时候返回 True。** 没有证据不等于
        「没变」——拿不到样本时必须维持原来的判断，不能因为
        「没法验证」就放行。
        """
        compared = 0
        for chunk in sorted(session.chunks, key=lambda c: c.start):
            path = self._partial_path(chunk.id)
            try:
                if not path.exists():
                    continue
                on_disk = path.stat().st_size
                if on_disk <= 0:
                    continue
                with path.open("rb") as handle:
                    data = handle.read(min(4096, on_disk))
            except OSError:
                continue
            if not data:
                continue
            compared += 1
            end = chunk.start + len(data) - 1
            try:
                remote = await self.downloader.fetch_range(probe.url, chunk.start, end)
            except Exception as err:
                # 取不到就没法证明「没变」——保守起见维持「变了」。
                log.warning(
                    CAT_ENGINE,
                    f"取样失败（偏移 {chunk.start}），无法确认资源未变化: {err}",
                )
                return True
            if not remote:
                return True
            if remote != data:
                log.notice(
                    CAT_ENGINE,
                    f"偏移 {chunk.start} 处的字节不同，资源确实变了",
                )
                return True
        # **一个字节都没比成**——盘上没东西可比。没有证据就
        # 不能推翻「资源变了」的判断。
        return compared == 0

    # -- 并发 --------------------------------------------------------

    async def _run_chunks(
        self,
        url: str,
        planner: DynamicPlanner,
        assembler: FileAssembler,
        chunk_progress: ChunkProgress,
        use_range: bool,
    ) -> None:
        """并发跑分片。任一分片失败则整体失败。

        **这里是动态分段的落点。** worker 池不是「每个分片一个任务」，
        而是「固定数量的 worker 抢分片」——抢不到就拆分当前最大的
        未完成分片，从拆分点接着下（规格 §1.2）。
        """
        self._planner = planner
        self._governor = ConnectionGovernor(self.initial_connections, self.parallelism_cap)
        self._governor.bind_loop(asyncio.get_running_loop())

        async def worker() -> None:
            self._live_workers += 1
            if self._live_workers > self.min_live_workers:
                self.min_live_workers = self._live_workers
            try:
                await _worker_body()
            finally:
                self._live_workers -= 1
                # **下载还没完就有 worker 走了 = 提前下班。**
                #
                # 记下来是为了让测试能盯住这个行为本身——「末尾速度
                # 够不够快」是间接指标，机器一忙就飘；「有没有 worker
                # 在活没干完的时候跑了」是直接的事实。
                if not planner.all_complete():
                    self.workers_exited_early += 1

        async def _worker_body() -> None:
            while True:
                if self._cancelled:
                    raise asyncio.CancelledError()

                # 1) 找活干：先看待办队列，再看能不能切一块出来。
                #
                # **找活干不占连接名额**——名额只在真的在下载的时候
                # 才占。这样「连接数」限制的是**同时在传输的连接数**，
                # 不是「已经分好片的数量」。
                chunk = planner.take_pending()
                if chunk is None:
                    split = planner.split_largest()
                    if split is not None:
                        if split.was_inflight:
                            # 被拆的分片正在传输：它的区间缩短了，
                            # 已经写超的部分要砍掉——那些字节属于
                            # 新分片的区间，留着会重叠。
                            assembler.shrink_chunk(split.source.id, split.split_at)
                        chunk = split.chunk

                if chunk is None:
                    # 2) 没活干。
                    if planner.all_complete():
                        # 全下完了，这个 worker 可以下班。
                        return
                    # **下载还没完，但这个 worker 拿不到活。**
                    #
                    # 这是长尾的成因所在。以前这里直接 `return`：
                    # worker 一退出，那条连接就永久没了。下载末尾只剩
                    # 几个分片时，活跃连接数一路往下掉（32 → 8 → 2 → 1），
                    # 速度跟着掉到 1/N——这就是「下到 96% 就不动了」。
                    #
                    # 现在改成等：别的 worker 切出新分片时自己会醒，
                    # 末尾的活照样分给所有连接。
                    self.worker_idle_waits += 1
                    await asyncio.sleep(self._idle_poll)
                    continue

                # 3) 有活：占一个连接名额下。
                await self._governor.acquire()
                try:
                    planner.mark_inflight(chunk.id)
                    try:
                        await self._download_one(
                            url, chunk, planner, assembler, chunk_progress, use_range
                        )
                    finally:
                        planner.mark_finished(chunk.id)
                finally:
                    self._governor.release()

        worker_count = max(1, min(self.parallelism_cap, self.parallelism_cap))
        tasks = [asyncio.create_task(worker()) for _ in range(worker_count)]
        self._chunk_tasks = set(tasks)
        ramp_task = asyncio.create_task(self._ramp_connections(planner))
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            ramp_task.cancel()
            self._chunk_tasks = set()
            self.progress_channel.flush()

    async def _download_one(
        self,
        url: str,
        chunk: Chunk,
        planner: DynamicPlanner,
        assembler: FileAssembler,
        chunk_progress: ChunkProgress,
        use_range: bool,
    ) -> None:
        """下载一个分片。"""
        self._chunk_started_at[chunk.id] = time.monotonic()

        def on_progress(delta: int) -> None:
            # 增量上报：所有分片的增量求和 = 文件级进度。
            chunk_progress.report(delta)
            planner.update_progress(chunk.id, delta)
            self._last_progress_at = time.monotonic()

        await self.downloader.download_chunk(
            url=url,
            chunk=chunk,
            assembler=assembler,
            progress=on_progress,
            headers=dict(self.headers),
            limiter=self.limiter,
            use_range=use_range,
        )

        # 分片完成：把 bytes_done 顶到末尾。
        planner.update_progress(chunk.id, chunk.size)
        chunk.bytes_done = chunk.size
        # 通知放在 bytes_done 更新之后——顺序反了的话，最后
        # 完成的那个分片在详情面板里永远显示「未完成」。
        self._notify_chunk_progress()

    async def _ramp_connections(self, planner: DynamicPlanner) -> None:
        """逐步放开并发连接数（规格 §1.2.3）。

        初始连接数保守（默认 2）。服务器健康就放开一个，一直到上限；
        出现错误就收回来——服务器在报错的时候继续加连接只会让它更糟。

        **间隔 0.1 秒，不是「几秒一级」。** 用户在设置里把线程数
        调到 32，等 8 秒才开满 32 条连接，用起来会以为是设置没生效。
        0.1 秒一级意味着 32 条连接 3 秒开满——保守只体现在最开始
        那 100 毫秒（2 条连接），足够服务器认出「这不是 DDoS」。

        **要有进度才放开。** 字节卡住不动的时候加连接只会让情况更糟，
        所以「最近有进度」是放开的前提。
        """
        interval = 0.1
        while True:
            await asyncio.sleep(interval)
            if self._cancelled:
                return
            try:
                if planner.all_complete():
                    continue
                # 最近没有字节流动 → 不加连接。
                if self._last_progress_at < time.monotonic() - 2.0:
                    continue
                if self._governor.limit < self.parallelism_cap:
                    self._governor.set_limit(self._governor.limit + 1)
            except Exception as err:
                # **后台任务不能静默死掉。** 之前这里抛过一次
                # AttributeError（属性名写错），任务当场没了，
                # 连接数从此再也不涨——下载看着「正常」，只是永远
                # 用 2 条连接。异常必须留下痕迹。
                log.warning(CAT_ENGINE, f"连接数调节出错: {err}")
                return

    # -- 会话文件 ----------------------------------------------------

    async def _persist_session_loop(self) -> None:
        """定期把分片布局写到会话文件。

        崩了、进程被杀了、机器断电——布局得在盘上，否则续传时
        不知道哪个 id 对应哪段字节，已下的字节就白下了。
        """
        try:
            while True:
                await asyncio.sleep(2.0)
                self._write_session()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _write_session(self) -> None:
        planner = self._planner
        probe = self._probe
        if planner is None or probe is None:
            return
        try:
            state = SessionState(
                url=self.url,
                etag=probe.etag,
                last_modified=probe.last_modified,
                content_length=probe.content_length,
                chunks=[
                    SessionChunk(
                        id=c.id,
                        start=c.start,
                        end=c.end,
                        bytes_done=c.bytes_done,
                    )
                    for c in planner.chunks()
                ],
            )
            state.write(self.destination)
        except Exception as err:
            log.warning(CAT_STORE, f"会话文件写入失败: {err}")

    def _remove_session(self) -> None:
        try:
            SessionState.remove(self.destination)
        except Exception as err:
            # **不要静默吞掉。** 之前这里只有一个 `except Exception:
            # pass`，结果 `SessionState.remove` 少了 `@classmethod`
            # 装饰器、调用直接抛 TypeError，异常被吞掉，会话文件永远
            # 留在下载目录里，症状是「下完之后目录里多一个
            # .sdm.session.json」，查不出原因。
            log.warning(CAT_STORE, f"会话文件删除失败: {err}")

    # -- 校验和 ------------------------------------------------------

    def _compute_checksum(self) -> str | None:
        """算校验和（规格 §1.7）。

        只算用户明确选了的算法，没选就不算——SHA-256 一遍几百 MB
        要好几秒，用户没要求就别偷偷花他的 CPU。
        """
        algorithm = (self.checksum_algorithm or "").strip().lower()
        if not algorithm or algorithm == "none":
            return None
        digests = {
            "md5": hashlib.md5,
            "sha1": hashlib.sha1,
            "sha256": hashlib.sha256,
        }
        factory = digests.get(algorithm)
        if factory is None:
            log.warning(CAT_ENGINE, f"未知的校验算法: {algorithm}")
            return None
        digest = factory()
        try:
            with open(self.destination, "rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
        except OSError as err:
            log.warning(CAT_ENGINE, f"校验和计算失败: {err}")
            return None
        return digest.hexdigest()

    # -- 分片状态 ----------------------------------------------------

    def _notify_chunk_progress(self) -> None:
        """把当前分片状态推给上层（详情面板用）。

        每个分片带自己的「距上次写入秒数」。所有分片显示同一个值
        是没意义的——快的分片和慢的分片差别很大，而这个数字能
        告诉用户「哪个分片卡住了」。
        """
        if self.on_chunk_progress is None:
            return
        now = time.monotonic()
        # 用规划器的**实时**布局。动态分段会不断加新分片，快照要是
        # 启动时那份，用户看到的分片数就跟实际下载对不上了。
        planner = self._planner
        if planner is not None:
            current = planner.chunks()
            self.planned_chunks = current
        else:
            current = self.planned_chunks
        states = []
        for c in current:
            states.append({
                "id": c.id,
                "start": c.start,
                "end": c.end,
                "size": c.size,
                "bytes_done": c.bytes_done,
                "idle_seconds": self._chunk_idle_seconds(c.id, now),
            })
        try:
            self.on_chunk_progress(states)
        except Exception:
            pass

    def _chunk_idle_seconds(self, chunk_id: int, now: float) -> float | None:
        """某个分片距上次写入过了多少秒。

        还没开始写的分片返回 None——「0 秒」会被误读成「刚写过」。
        """
        assembler = self._assembler
        if assembler is None:
            return None
        updated = assembler.chunk_updated_at(chunk_id)
        if updated is None:
            return None
        return max(0.0, now - updated)

    def _partial_path(self, chunk_id: int) -> Path:
        """分片临时文件的位置。

        **在工作目录里，不在下载目录里**——用户在下载文件夹里
        看到一堆 `.sdm.partial-N` 只会以为软件坏了。

        升级前下的分片还在老位置，这里把它们搬过来。
        """
        return work_dir.adopt_legacy_partial(self.destination, chunk_id)
