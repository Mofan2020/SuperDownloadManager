"""引擎编排器：任务队列 + 生命周期控制。

对应 Swift 版 Engine/DownloadEngine.swift 与 Engine/TaskQueue.swift。

**这里修 Swift v1.1.3 的「暂停被报成失败」bug。**

Swift 版用 `CancelGuard` 这个进程级 Set 传递「这次取消是用户
主动点的，不是真失败」。竞态很多：取消先到、异常处理后到，顺序
反了就判错。

Python 版不用这套：每个任务有自己的 `asyncio.Task`，状态变更
全部走同一个事件循环，**取消是协程内的有序操作**，不存在竞态。
暂停时先写状态再取消协程，异常处理器读到的状态一定是「已暂停」。
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..history_store import HistoryEntry, HistoryStore
from ..logging_setup import CAT_ENGINE, CAT_QUEUE, CAT_STORE, get_logger
from .categories import category_from_url
from .download_flow import DownloadFlow, FlowError, FlowFailure
from .file_assembler import _unlink_with_retry
from .filenames import sanitize_filename
from .http_downloader import DownloadError, HTTPDownloader
from .progress_channel import ProgressChannel
from .range_planner import RangeStrategy
from .resume_state import ResumeState
from .speed_display import SpeedInput, compute
from .speed_limiter import SpeedLimiter
from .speed_policy import SpeedPolicy

log = get_logger()


class TaskState(enum.Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_NEW_LINK = "awaiting_new_link"

    @property
    def is_terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (TaskState.QUEUED, TaskState.DOWNLOADING)


def _is_terminal_state(state: str) -> bool:
    """状态字符串是不是终态（完成/失败/取消）。

    历史落库和速度清零都以终态为准。TaskState.is_terminal 是
    唯一真源，这里从字符串反查——快照里的 state 是字符串，
    不是枚举。
    """
    try:
        return TaskState(state).is_terminal
    except ValueError:
        return False


@dataclass
class TaskSummary:
    """任务的对外视图。GUI、CLI、IPC 都读这个结构。"""

    id: str
    url: str
    state: str
    bytes_done: int = 0
    total_bytes: int | None = None
    speed_bytes_per_sec: float = 0.0
    error_message: str | None = None
    destination: str = ""
    speed_limit_bytes_per_sec: float = 0.0
    category: str = "other"
    filename: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    chunks: list[dict] = field(default_factory=list)
    chunk_count: int = 0
    completed_chunks: int = 0
    #: 显示速度的 EMA 缓存（Swift 版在 GUI 的 store 里，这里放引擎）。
    #: 终态/暂停时清零——Swift v1.0.7 的 bug 就是进度满了速度还在显示。
    speed_ema: float = 0.0

    def to_dict(self, with_chunks: bool = False) -> dict:
        """转成 JSON 可序列化的字典。

        `with_chunks=False`（默认）时**不带分片明细**——分片数组
        有 32~128 项，每个任务每 700ms 传一次的话，光是 JSON
        序列化就是无谓的开销。分片只在用户打开详情面板时才需要，
        那时再单独取。
        """
        payload = {
            "id": self.id,
            "url": self.url,
            "state": self.state,
            "bytes_done": self.bytes_done,
            "total_bytes": self.total_bytes,
            "speed_bytes_per_sec": round(self.speed_bytes_per_sec, 2),
            "error_message": self.error_message,
            "destination": self.destination,
            "speed_limit_bytes_per_sec": self.speed_limit_bytes_per_sec,
            "category": self.category,
            "filename": self.filename,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "chunk_count": self.chunk_count,
            "completed_chunks": self.completed_chunks,
        }
        if with_chunks:
            payload["chunks"] = self.chunks
        return payload


@dataclass
class StartSpec:
    """启动一个下载所需的参数。"""

    url: str
    destination_directory: Path
    filename_override: str | None = None
    max_threads_override: int | None = None
    referer: str | None = None


class LiveTask:
    """一个正在跑的任务。"""

    def __init__(self, task_id: str, spec: StartSpec) -> None:
        self.id = task_id
        self.spec = spec
        self.task: asyncio.Task | None = None
        self.flow: DownloadFlow | None = None
        self.paused = False
        self.cancelled = False


class DownloadEngine:
    """引擎主对象。"""

    def __init__(
        self,
        strategy: RangeStrategy | None = None,
        parallelism_cap: int = 32,
        default_download_dir: Path | None = None,
        downloader: HTTPDownloader | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        self.strategy = strategy or RangeStrategy()
        self.parallelism_cap = parallelism_cap
        self.default_download_dir = Path(default_download_dir) if default_download_dir else None
        self.downloader = downloader or HTTPDownloader()
        self.limiter = SpeedLimiter(0)
        # 时间窗口限速。Swift 版这块是死代码，这里真正接上：
        # 引擎每 2 秒重算一次生效速率，推给全局限速器。
        self.speed_policy = SpeedPolicy()
        self._policy_timer: asyncio.Task | None = None
        self.tasks: dict[str, TaskSummary] = {}
        self.live: dict[str, LiveTask] = {}
        self._task_limiters: dict[str, SpeedLimiter] = {}
        self._lock = asyncio.Lock()
        # 每个任务的分片状态：{task_id: [分片dict, ...]}
        #
        # 任务还在跑的时候实时更新；任务结束后保留最终状态——详情
        # 面板要能查已完成任务的分片明细（Swift 版用合成数据展示，
        # 这里是真实数据）。但不能无限留：每个任务几十到上百个
        # dict，攒多了占内存。超过上限就丢最老的。
        self._chunk_states: dict[str, list[dict]] = {}
        self._chunk_state_order: list[str] = []
        # 历史库。引擎是终态的唯一权威，所以落库放在这里而不是
        # GUI 层——daemon 模式没有 GUI，历史也得写进去。
        # 构造 HistoryStore 不碰磁盘（路径解析 + 惰性连接），
        # 真正建库发生在第一次 insert()。
        self.history_store = history_store or HistoryStore()

    #: 保留多少个已结束任务的分片明细
    CHUNK_STATE_RETENTION = 20

    # -- 配置 --------------------------------------------------------

    async def start_policy_timer(self, interval: float = 2.0) -> None:
        """启动时间窗限速的刷新循环。

        Swift 版有这个策略类但从没调用过，所以限速规则存了也不生效。
        这里每 2 秒重算一次：窗口变了就把新速率推给全局限速器。
        """
        if self._policy_timer is not None:
            return

        async def loop() -> None:
            while True:
                await asyncio.sleep(interval)
                rate = self.speed_policy.effective_rate()
                if rate != self.limiter.rate:
                    await self.limiter.set_rate(rate)
                    log.notice(CAT_ENGINE, f"时间窗限速生效: {rate / 1024:.0f} KB/s")

        self._policy_timer = asyncio.create_task(loop())

    def set_default_download_dir(self, path: Path) -> None:
        self.default_download_dir = Path(path)

    def set_max_threads(self, n: int) -> None:
        clamped = max(1, min(128, n))
        self.strategy.default_chunks = clamped
        self.parallelism_cap = clamped

    def set_ignore_ssl(self, value: bool) -> None:
        self.downloader.set_ignore_ssl(value)

    async def set_speed_limit(self, bytes_per_sec: float) -> None:
        """全局带宽限制。真正推给限速器——Swift v1.0.x 存了值但没生效。"""
        self.limiter.set_rate(bytes_per_sec)

    async def set_task_speed_limit(self, task_id: str, bytes_per_sec: float) -> None:
        """单任务限速。0 表示清除覆盖，回到全局限制。"""
        if bytes_per_sec <= 0:
            self._task_limiters.pop(task_id, None)
        else:
            limiter = self._task_limiters.get(task_id)
            if limiter is None:
                limiter = SpeedLimiter(bytes_per_sec)
                self._task_limiters[task_id] = limiter
            else:
                limiter.set_rate(bytes_per_sec)
        summary = self.tasks.get(task_id)
        if summary:
            summary.speed_limit_bytes_per_sec = max(0.0, bytes_per_sec)

    # -- 启动 --------------------------------------------------------

    @staticmethod
    def filename_for(url: str) -> str:
        """从 URL 猜文件名。取不到就用主机名，再不行就 download.bin。

        结果一定是跨平台安全的——Windows 的文件名限制比 macOS 严，
        不清洗的话下载到最后一步落盘会炸。
        """
        from urllib.parse import unquote, urlparse

        path = urlparse(url).path
        name = unquote(path.rsplit("/", 1)[-1]) if path else ""
        if name:
            return sanitize_filename(name, fallback="download.bin")
        host = urlparse(url).hostname
        if host:
            return sanitize_filename(host, fallback="download.bin")
        return "download.bin"

    async def start(self, specs: list[StartSpec]) -> list[str]:
        """启动一个或多个下载，返回任务 id 列表。"""
        ids: list[str] = []
        for spec in specs:
            task_id = str(uuid.uuid4())
            filename = sanitize_filename(
                spec.filename_override or self.filename_for(spec.url),
                fallback="download.bin",
            )
            destination = Path(spec.destination_directory) / filename

            summary = TaskSummary(
                id=task_id,
                url=spec.url,
                state=TaskState.QUEUED.value,
                destination=str(destination),
                category=category_from_url(spec.url).value,
                filename=filename,
                started_at=time.time(),
            )
            self.tasks[task_id] = summary

            live = LiveTask(task_id, spec)
            self.live[task_id] = live
            live.task = asyncio.create_task(self._run_task(task_id, live))
            ids.append(task_id)
        return ids

    async def _run_task(self, task_id: str, live: LiveTask) -> None:
        """任务的执行体。"""
        spec = live.spec
        summary = self.tasks.get(task_id)
        if summary is None:
            return
        summary.state = TaskState.DOWNLOADING.value

        # 探测一次拿真实文件名和总长度。
        # 用共享的 downloader——Swift v1.1.8 在这里另建了一个
        # HTTPDownloader()，那个实例没有 SSL 绕过开关，
        # CDN 拒证书时探测静默失败，任务就永远卡在「下载中」。
        try:
            probe = await self.downloader.probe(
                spec.url,
                # 任务级的头（Referer 等）——探测阶段有些站点就 403。
                extra_headers={"Referer": spec.referer} if spec.referer else None,
            )
        except Exception:
            # 探测失败不直接判死——下面的正式流程会再探一次，
            # 那时才会带着真实错误信息回来（403 会在那里被
            # 正确归类成「等待新链接」）。
            probe = None

        if probe is not None:
            summary.total_bytes = probe.content_length
            real_name = probe.suggested_filename
            if real_name and not spec.filename_override and summary.destination:
                # 服务器给的文件名可能带 Windows 非法字符
                # （`:` `/` `?` `*` 等等），不洗一遍的话
                # 下载到最后落盘那一步会炸。
                real_name = sanitize_filename(real_name, fallback="download.bin")
                new_dest = Path(summary.destination).parent / real_name
                summary.destination = str(new_dest)
                summary.filename = real_name

        destination = Path(summary.destination)

        channel = ProgressChannel()
        channel.bind_loop(asyncio.get_running_loop())
        live.flow = DownloadFlow(
            url=spec.url,
            destination=destination,
            strategy=self.strategy,
            parallelism_cap=spec.max_threads_override or self.parallelism_cap,
            limiter=self._task_limiters.get(task_id) or self.limiter,
            downloader=self.downloader,
            progress_channel=channel,
            # 任务级的 Referer。
            #
            # `add` 方法可以带 referer 参数（浏览器扩展派单时会把
            # 页面地址带过来），这个头必须真的发出去——很多站点要
            # Referer 才给下载。
            #
            # 站点规则的 Referer 优先级更高（在 http_downloader 里
            # 合并时规则覆盖任务级），规则是用户为该主机显式配置的
            # 全局设置。
            headers={"Referer": spec.referer} if spec.referer else None,
        )

        # 记录分片布局，供详情面板展示。
        # Swift 版的详情页分片是「合成的」（假的），这里是真的。
        self._chunk_states[task_id] = self._build_chunk_states(live)
        if task_id not in self._chunk_state_order:
            self._chunk_state_order.append(task_id)
        # 分片进度变化时同步到引擎
        live.flow.on_chunk_progress = lambda states: self.update_chunk_states(task_id, states)

        progress_task = asyncio.create_task(self._consume_progress(task_id, channel))

        try:
            await live.flow.run()
            summary.state = TaskState.COMPLETED.value
            summary.bytes_done = summary.total_bytes or summary.bytes_done
            self._zero_speed(summary)
            summary.finished_at = time.time()
            log.notice(CAT_ENGINE, f"任务完成 {summary.filename}")
            self._record_history(summary)
        except asyncio.CancelledError:
            # 用户点了暂停或取消。状态在 pause/cancel 里已经写好了，
            # 这里绝不能改成 failed。
            log.notice(CAT_QUEUE, f"任务被取消 {summary.filename}")
            if live.cancelled:
                summary.state = TaskState.CANCELLED.value
            elif live.paused:
                summary.state = TaskState.PAUSED.value
            self._zero_speed(summary)
            # 取消是终态，要落历史；暂停不是终态，_record_history
            # 内部会自己判断，这里调用是安全的。
            self._record_history(summary)
        except FlowError as err:
            if err.kind == FlowFailure.EXPIRED_LINK.value:
                summary.state = TaskState.AWAITING_NEW_LINK.value
                summary.error_message = err.detail or "服务器拒绝了下载链接"
            elif err.kind == FlowFailure.CANCELLED.value:
                # 取消不是失败。走到这里说明取消被转成了 FlowError
                # （flow 层现在直接抛 CancelledError，这里是双保险）。
                summary.state = TaskState.CANCELLED.value
                summary.error_message = None
            else:
                summary.state = TaskState.FAILED.value
                summary.error_message = err.detail or str(err)
            self._zero_speed(summary)
            summary.finished_at = time.time()
            log.error(CAT_ENGINE, f"任务失败 {summary.filename}: {err}")
            self._record_history(summary)
        except DownloadError as err:
            summary.state = TaskState.FAILED.value
            summary.error_message = err.detail or str(err)
            self._zero_speed(summary)
            summary.finished_at = time.time()
            self._record_history(summary)
        finally:
            progress_task.cancel()
            self.live.pop(task_id, None)
            # 任务结束：分片明细留一份最终状态，详情面板还能查。
            # 超过保留上限就丢最老的。
            self._retire_chunk_states(task_id)

    def _build_chunk_states(self, live: LiveTask) -> list[dict]:
        """构造分片状态列表。

        分片布局在 DownloadFlow 内部算好，这里拿不到，所以从
        下载器的 assembler 反查——拿不到就返回空列表，详情页
        会显示「无分片信息」而不是假的。
        """
        flow = live.flow
        if flow is None:
            return []
        planner_chunks = getattr(flow, "planned_chunks", None)
        if not planner_chunks:
            return []
        states: list[dict] = []
        for chunk in planner_chunks:
            states.append({
                "id": chunk.id,
                "start": chunk.start,
                "end": chunk.end,
                "size": chunk.size,
                "bytes_done": chunk.bytes_done,
            })
        return states

    def update_chunk_states(self, task_id: str, states: list[dict]) -> None:
        """下载过程中更新分片进度。"""
        self._chunk_states[task_id] = states
        if task_id not in self._chunk_state_order:
            self._chunk_state_order.append(task_id)
        # 摘要里的分片计数也要跟着走。列表页那行「已完成分片/总片数」
        # 读的是摘要字段，不读 _chunk_states——不同步的话永远是 0/0。
        summary = self.tasks.get(task_id)
        if summary is not None:
            summary.chunk_count = len(states)
            summary.completed_chunks = sum(
                1 for s in states if s.get("bytes_done", 0) >= s.get("size", 0)
            )

    def _retire_chunk_states(self, task_id: str) -> None:
        """任务结束：分片明细保留有限时间，超上限丢最老的。"""
        if task_id in self._chunk_state_order:
            self._chunk_state_order.remove(task_id)
        self._chunk_state_order.append(task_id)
        while len(self._chunk_state_order) > self.CHUNK_STATE_RETENTION:
            oldest = self._chunk_state_order.pop(0)
            self._chunk_states.pop(oldest, None)

    async def _consume_progress(self, task_id: str, channel: ProgressChannel) -> None:
        """把进度流写进任务快照。"""
        # 起点用快照里已有的字节数，不是 0。
        #
        # 恢复下载时，进度流的第一帧报的是**整个已下载量**
        # （flow 里 `progress_channel.report(pre_existing)`），
        # 从 0 起算的话第一帧的 delta 就是整个文件，算出来的
        # 速度是个巨大的假值，EMA 第一帧直接被这个假值带跑。
        summary = self.tasks.get(task_id)
        last = summary.bytes_done if summary is not None else 0
        last_time = time.monotonic()
        async for total in channel.stream():
            summary = self.tasks.get(task_id)
            if summary is None:
                return
            summary.bytes_done = total
            # 分片状态跟着进度一起更新（节流：每次进度都算一次
            # 32~128 个分片的状态有点浪费，但进度本身也是节流过的，
            # 频率不高，可以接受）
            live = self.live.get(task_id)
            if live is not None and live.flow is not None:
                live.flow._notify_chunk_progress()
            now = time.monotonic()
            elapsed = now - last_time
            if elapsed > 0:
                delta = total - last
                last = total
                last_time = now
                self._apply_speed(summary, delta, elapsed)

    def _apply_speed(self, summary: TaskSummary, delta_bytes: int, elapsed: float) -> float:
        """把一次进度增量折算成对外显示的速度（EMA 平滑）。

        对应 Swift 版 `DownloadListStore.tick()` 里调
        `SpeedDisplay.compute()` 的那一步。Swift 那边是 200ms
        固定 tick，这里是事件驱动（ProgressChannel 推一次算一次），
        所以窗口长度用实际 elapsed。

        **EMA 缓存在任务快照上**（每个任务一份），不在全局——
        Swift 版的 `smoothedSpeed` 是 GUI store 里的字典，
        Python 版没有 store 这一层，引擎是速度的唯一来源，
        GUI/CLI/IPC 都读同一个字段。

        终态/暂停时 `compute()` 直接返回 0 并把 EMA 缓存清零：
        Swift v1.0.7 的 bug 就是进度满了速度还在显示（EMA 以
        0.6 的系数往 0 衰减，要过一秒才掉下去）。

        `engine_speed_bytes_per_sec` 传 0：Python 引擎没有独立的
        测速源，速度只能从进度增量除以实际间隔得到——这件事
        交给 `compute()` 里的 `delta / tick_interval` 分支做，
        公式只有一份，引擎不自己重写。
        """
        out = compute(
            SpeedInput(
                state=summary.state,
                engine_speed_bytes_per_sec=0.0,
                delta_bytes_since_last_tick=max(0, delta_bytes),
                tick_interval_seconds=max(0.0, elapsed),
            ),
            summary.speed_ema,
        )
        summary.speed_ema = out.next_ema
        summary.speed_bytes_per_sec = out.displayed_speed
        return out.displayed_speed

    def _zero_speed(self, summary: TaskSummary) -> None:
        """显示速度硬清零，EMA 缓存一起清掉。

        终态和暂停都走这里。**不是**让 EMA 衰减到 0——那正是
        Swift v1.0.7 的 bug：进度条满了速度还在显示。
        """
        summary.speed_bytes_per_sec = 0.0
        summary.speed_ema = 0.0

    def _record_history(self, summary: TaskSummary) -> None:
        """任务进终态时写一条历史。

        **为什么在引擎里写，而不是 GUI 层**：引擎是终态状态的
        唯一权威——daemon 模式没有 GUI，历史也得落库。Swift 版
        写在 `SDMApp.swift` 的 `onChange(of: store.tasks)` 里，
        那是 GUI 层的观察者，daemon 跑起来就没有了。

        **写库失败绝不能影响下载主流程**：磁盘满、库被锁、
        表结构不对……任何异常都吞掉，只记日志。

        幂等：`INSERT OR REPLACE`，同一个 task id 写多少次都只有
        一行。Swift 版每次 store 变化都重写所有终态行，效果一样。
        """
        if not _is_terminal_state(summary.state):
            return
        try:
            self.history_store.insert(
                HistoryEntry(
                    id=summary.id,
                    url=summary.url,
                    filename=summary.filename,
                    destination_path=summary.destination,
                    total_bytes=summary.total_bytes,
                    bytes_done=summary.bytes_done,
                    state=summary.state,
                    error_message=summary.error_message,
                    started_at=summary.started_at,
                    finished_at=summary.finished_at or time.time(),
                    sha256=None,
                )
            )
        except Exception as err:
            log.warning(CAT_STORE, f"历史落库失败 {summary.id}: {err}")

    # -- 控制 --------------------------------------------------------

    async def pause(self, task_id: str) -> None:
        """暂停任务。已下载的分片留在盘上，resume 时接着下。"""
        live = self.live.get(task_id)
        summary = self.tasks.get(task_id)
        if summary is None:
            return
        if live is not None:
            live.paused = True
            if live.flow is not None:
                live.flow.cancel()
            if live.task is not None and not live.task.done():
                live.task.cancel()
        summary.state = TaskState.PAUSED.value
        # 暂停也要清 EMA 缓存——恢复后第一帧不该带着暂停前的
        # 旧速度往下衰减（Swift v1.0.7 的 bug 就是这个）。
        self._zero_speed(summary)

    async def resume(self, task_id: str) -> None:
        """恢复任务。分片文件在盘上，所以是真的从断点继续。"""
        summary = self.tasks.get(task_id)
        if summary is None:
            return
        live = self.live.get(task_id)
        if live is not None and live.task is not None and not live.task.done():
            return  # 还在跑

        spec = StartSpec(
            url=summary.url,
            destination_directory=Path(summary.destination).parent,
            filename_override=Path(summary.destination).name,
        )
        new_live = LiveTask(task_id, spec)
        self.live[task_id] = new_live
        summary.state = TaskState.DOWNLOADING.value
        summary.error_message = None
        # 恢复后第一帧从零开始算，不带暂停前的旧 EMA。
        self._zero_speed(summary)
        new_live.task = asyncio.create_task(self._run_task(task_id, new_live))

    async def cancel(self, task_id: str) -> None:
        """取消任务。

        分片临时文件会被删掉——取消的意思是「这个任务不要了」，
        留着 `.sdm.partial-N` 只会让下载目录里堆垃圾，而且下次
        同名任务会扫到它们。

        暂停（pause）不一样：暂停要保留分片，resume 时接着下。
        两者的区别就是这里删不删分片。
        """
        live = self.live.get(task_id)
        summary = self.tasks.get(task_id)
        running = None
        # flow 引用要先抓住。任务结束后引擎会在 finally 里
        # self.live.pop(task_id)，那时候再取 live.flow 就成了 None——
        # 分片句柄没人关，Windows 上文件删不掉。
        flow = live.flow if live is not None else None
        if live is not None:
            live.cancelled = True
            live.paused = False
            if flow is not None:
                flow.cancel()
            if live.task is not None and not live.task.done():
                live.task.cancel()
                running = live.task
        if summary is not None:
            if summary.state != TaskState.COMPLETED.value:
                summary.state = TaskState.CANCELLED.value
                summary.finished_at = time.time()
            self._zero_speed(summary)
            # 取消是终态，落历史。幂等——引擎里其他路径也可能
            # 写过同一条，INSERT OR REPLACE 不会多出第二行。
            self._record_history(summary)
        # 等分片任务真正停下再删文件。
        #
        # task.cancel() 只是请求取消，任务还在收尾、文件还开着。
        # macOS 上删打开的文件没事（inode 留到最后一个句柄关闭），
        # Windows 上直接 PermissionError——分片就留下了。
        if running is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(running), timeout=5.0)
        # 先关句柄，再删文件。
        #
        # 顺序不能反：句柄开着的时候 Windows 删不掉。
        if flow is not None:
            flow.close_partial_handles()
        # 删掉分片临时文件。放在这里而不是 flow.cancel() 里，
        # 因为 pause 也要调 flow.cancel()——那一路要保留分片。
        self._discard_partials(task_id, summary, flow)

    def _discard_partials(
        self,
        task_id: str,
        summary: TaskSummary | None,
        flow: DownloadFlow | None = None,
    ) -> None:
        """删掉某个任务的分片临时文件。

        优先走 flow 的 discard_partials()——它知道分片文件在哪，
        而且会先把文件句柄关掉。Windows 上文件开着就删不掉
        （PermissionError），自己按文件名 glob 删会漏。

        flow 已经不在了（任务早就结束了）时，用 destination 反推
        分片文件名——分片名是 `<目标名>.sdm.partial-<id>`。
        """
        if flow is None:
            live = self.live.get(task_id)
            flow = live.flow if live is not None else None
        if flow is not None:
            flow.discard_partials()
        if summary is None:
            return
        base = Path(summary.destination)
        try:
            for path in base.parent.glob(base.name + ".sdm.partial-*"):
                _unlink_with_retry(path)
        except OSError:
            pass

    async def remove(self, task_id: str) -> None:
        """从队列移除。磁盘上的文件不动。"""
        await self.cancel(task_id)
        self.tasks.pop(task_id, None)
        self.live.pop(task_id, None)
        self._task_limiters.pop(task_id, None)
        self._chunk_states.pop(task_id, None)
        if task_id in self._chunk_state_order:
            self._chunk_state_order.remove(task_id)

    async def clear_completed(self) -> None:
        for summary in list(self.tasks.values()):
            if summary.state == TaskState.COMPLETED.value:
                await self.remove(summary.id)

    async def relink(self, task_id: str, new_url: str) -> dict:
        """给等待新链接的任务换链接。"""
        from .expired_link import ExpiredLinkHandler, RelinkDecision

        summary = self.tasks.get(task_id)
        if summary is None:
            return {"decision": "unsafeReject", "reason": "任务不存在"}

        destination = Path(summary.destination)
        sidecar = ResumeState.sidecar_path(destination)
        original = None
        if sidecar.exists():
            try:
                original = ResumeState.read(sidecar)
            except Exception:
                original = None
        if original is None:
            original = ResumeState(
                url=summary.url,
                content_length=summary.total_bytes or 0,
                filename=destination.name,
            )

        handler = ExpiredLinkHandler(original, self.downloader)
        result = await handler.evaluate_relink(new_url)

        if result.decision == RelinkDecision.SAFE_TO_RESUME:
            summary.url = new_url
            summary.state = TaskState.QUEUED.value
            summary.error_message = None
            await self.resume(task_id)
        return result.to_dict()

    # -- 查询 --------------------------------------------------------

    def snapshot(self) -> list[TaskSummary]:
        return list(self.tasks.values())

    def get(self, task_id: str) -> TaskSummary | None:
        return self.tasks.get(task_id)

    def get_chunks(self, task_id: str) -> list[dict]:
        """取单个任务的分片明细。

        列表接口不返回分片（省内存 + 省序列化），只有详情面板
        展开时单独取。
        """
        return list(self._chunk_states.get(task_id, []))

    async def aclose(self) -> None:
        if self._policy_timer is not None:
            self._policy_timer.cancel()
            self._policy_timer = None
        for live in list(self.live.values()):
            if live.task is not None and not live.task.done():
                live.task.cancel()
        await self.downloader.aclose()
