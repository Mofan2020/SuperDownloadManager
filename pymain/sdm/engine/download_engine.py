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
import os
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..history_store import HistoryEntry, HistoryStore
from ..logging_setup import CAT_ENGINE, CAT_QUEUE, CAT_STORE, get_logger
from . import work_dir
from .categories import DownloadCategory, category_for_download, category_from_url
from .content_sniffer import ContentProbe, ProbeError
from .download_flow import DownloadFlow, FlowError, FlowFailure
from .errors import ErrorCategory, TaskError
from .file_assembler import _unlink_with_retry
from .filenames import sanitize_filename
from .http_downloader import DownloadError, HTTPDownloader
from .progress_channel import ProgressChannel
from .range_planner import RangeStrategy
from .resume_state import ResumeState
from .speed_display import SpeedInput, compute
from .speed_limiter import SpeedLimiter
from .speed_policy import SpeedPolicy
from .work_dir import PARTIAL_INFIX, task_key, work_root

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
    # -- 规格 §5 / §2.3 的字段 -------------------------------------
    #: 失败的结构化分类（规格 §5.1 的 12 个类别之一）。
    error_category: str | None = None
    #: 这个失败能不能自动重试。
    error_retryable: bool = False
    #: 当前同时在传的分片数（= 连接数）。
    active_connections: int = 0
    #: 已上传字节。本项目只做下载，这个永远是 0——
    #: 但字段留着，详情面板的「已下载 / 已上传」两行才对称。
    uploaded_bytes: int = 0
    #: 校验算法与校验值（规格 §1.7）。
    checksum_algorithm: str | None = None
    checksum_value: str | None = None
    #: 服务器支不支持分块。False = 退化成单流下载了。
    server_supports_ranges: bool = True
    #: 队列里的位置。0 = 下一个就跑。
    queued_position: int = 0
    #: 是不是还在「连接中」。
    #:
    #: **从任务开跑到收到第一个字节之间，界面不能说「0 KB/s」。**
    #: 探测 + 建连 + 等第一个字节这段，下载器确实一个字节都没
    #: 拿到，但用户看到「0 KB/s」会以为卡死了——实测用户报的
    #: 「下载一直卡在 0 KB/s」就是这段。这里的 0 是**还没开始**，
    #: 不是**下了 0 字节还很快**，两者必须区分。
    connecting: bool = False
    #: 完成后行为（规格 §1.7）：none / open_file / open_folder / run_script / sleep。
    on_complete: str = "none"
    #: 任务级代理覆盖（规格 §1.6）。None = 走全局代理。
    proxy: str | None = None

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
            "error_category": self.error_category,
            "error_retryable": self.error_retryable,
            "active_connections": self.active_connections,
            "uploaded_bytes": self.uploaded_bytes,
            "checksum_algorithm": self.checksum_algorithm,
            "checksum_value": self.checksum_value,
            "server_supports_ranges": self.server_supports_ranges,
            "queued_position": self.queued_position,
            "on_complete": self.on_complete,
            "proxy": self.proxy,
            "connecting": self.connecting,
        }
        if with_chunks:
            payload["chunks"] = self.chunks
        return payload


def _resolve_category_id(name: str | None) -> str | None:
    """把外部传来的分类名解析成合法的分类 id。

    `auto` / 空串 / 认不出来的值都返回 None——意思是「**自动**」，
    由引擎自己判断。前端的「自动」选项就是这么表达的。

    认不出来的值也当自动处理：宁可让引擎猜一个，也别把任务归到
    一个不存在的分类里（那样列表按分类筛就找不到它）。
    """
    if not name:
        return None
    value = str(name).strip().lower()
    if not value or value == "auto":
        return None
    try:
        return DownloadCategory(value).value
    except ValueError:
        return None


@dataclass
class StartSpec:
    """启动一个下载所需的参数。"""

    url: str
    destination_directory: Path
    filename_override: str | None = None
    max_threads_override: int | None = None
    referer: str | None = None
    #: 任务级自定义请求头（规格 §1.6）。浏览器接管时自动继承
    #: 页面请求头是**期望行为**，不是可选项——扩展会把 Cookie /
    #: UA 一并发过来。
    headers: dict[str, str] = field(default_factory=dict)
    #: 任务级代理覆盖（规格 §1.6）。None = 用全局代理。
    proxy: str | None = None
    #: 分块数。None = 自动（动态分段自己长）。
    chunk_count_override: int | None = None
    #: 用户在添加对话框里**显式选**的分类（分类 id）。
    #: None = 自动——由引擎按 URL / 服务器给的文件名判断。
    #:
    #: **必须区分「自动」和「用户选了视频」。** 早先前端把下拉框的
    #: 默认值（视频）当成分类发过来，引擎按 URL 猜出来的正确分类
    #: 就被覆盖了——用户加一个 .dmg，列表里却归在视频。
    category: str | None = None
    #: 开始时机：`now` / `queue` / `scheduled`。
    start_when: str = "now"
    #: 校验算法：md5 / sha1 / sha256 / None。
    checksum_algorithm: str | None = None
    #: 完成后行为（规格 §1.7）。
    on_complete: str = "none"
    on_complete_script: str = ""


class LiveTask:
    """一个正在跑的任务。"""

    def __init__(self, task_id: str, spec: StartSpec) -> None:
        self.id = task_id
        self.spec = spec
        self.task: asyncio.Task | None = None
        self.flow: DownloadFlow | None = None
        self.paused = False
        self.cancelled = False


#: 名字冲突的处理策略（规格 §5.3）。
NAME_CONFLICT_KEEP_BOTH = "keep_both"
NAME_CONFLICT_OVERWRITE = "overwrite"
NAME_CONFLICT_ASK = "ask"


def _resolve_keep_both(path: Path) -> Path:
    """找一个不重名的文件名：`a.zip` → `a 1.zip` → `a 2.zip`。

    **默认策略。** 用户下了一个已经存在的文件名时，最安全的做法是
    两个都留着——把用户的旧文件悄悄删掉或者覆盖掉，是不可逆的损失。

    **编号的格式跟 macOS 一致：空格 + 编号，编号在扩展名之前。**
    也就是 `a 1.zip`，不是 `a (1).zip` 也不是 `a-1.zip`。
    系统（Finder / Safari / 邮件客户端）都是这么处理重名的，
    用户不用适应第二套规则。
    """
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(1, 1000):
        candidate = parent / f"{stem} {index}{suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} {int(time.time())}{suffix}"


def _trash_or_rename(path: Path) -> bool:
    """把文件丢进回收站。失败就改名让路。

    回收站优先用 `send2trash`——它跨平台。没装就退回「改名」：
    把旧文件改成 `<名字>.sdm-replaced-<时间戳>`，用户还能找回来。
    直接删除是最后手段，不做。
    """
    try:
        from send2trash import send2trash  # type: ignore[import-not-found]

        send2trash(str(path))
        return True
    except Exception:
        pass
    try:
        backup = path.with_name(f"{path.name}.sdm-replaced-{int(time.time())}")
        path.rename(backup)
        return True
    except OSError:
        return False


def _open_path(path: Path) -> None:
    """用系统默认程序打开文件。"""
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _reveal_path(path: Path) -> None:
    """在文件管理器里显示文件（打开所在文件夹）。"""
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def _run_script(script: Path) -> None:
    """跑一个脚本。"""
    import subprocess
    import sys

    if sys.platform.startswith("win"):
        subprocess.Popen([str(script)], shell=False)
    else:
        subprocess.Popen(["/bin/sh", str(script)])


def _sleep_machine() -> None:
    """让机器休眠。

    **只在 macOS 上真的执行。** 其他平台没有「睡眠」这个概念，
    硬凑的话要么没反应，要么直接把用户会话踢下线。
    """
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.Popen(["pmset", "sleepnow"])


class DownloadEngine:
    """引擎主对象。"""

    def __init__(
        self,
        strategy: RangeStrategy | None = None,
        parallelism_cap: int = 32,
        default_download_dir: Path | None = None,
        downloader: HTTPDownloader | None = None,
        history_store: HistoryStore | None = None,
        max_concurrent_tasks: int = 5,
    ) -> None:
        self.strategy = strategy or RangeStrategy()
        self.parallelism_cap = parallelism_cap
        #: 用户在设置页设的「分片上限」。真正生效的是
        #: `_sync_chunk_cap()` 算出来的那个（不小于连接数 × 4）。
        self._user_max_chunks = self.strategy.max_chunks
        self._sync_chunk_cap()
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
        #: **同时活跃的任务数上限**（规格 §1.8）。
        #: 超出的任务停在 `queued`，有槽位时自动开跑。
        self.max_concurrent_tasks = max(1, max_concurrent_tasks)
        #: 等待队列。FIFO，`move_task` 可以置顶 / 置底。
        self._queue: list[str] = []
        #: 名字冲突策略（规格 §5.3）。
        self.name_conflict_policy = NAME_CONFLICT_KEEP_BOTH
        #: 每个任务的后续可改选项（校验算法 / 完成后行为 / 代理）。
        self._task_options: dict[str, dict] = {}
        #: 每个任务最后的结构化错误，供 UI 的「复制错误详情」用。
        self._task_errors: dict[str, dict] = {}
        #: 每个任务最初的启动参数。恢复时要用——浏览器接管带过来的
        #: Cookie / UA 丢了，续传的请求就发不对了。
        self._specs: dict[str, StartSpec] = {}
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
        #: 每个任务最近 60 秒的速度采样（规格 §2.3 的速度曲线）。
        #:
        #: 为什么放引擎而不是 GUI：daemon 模式没有 GUI，但 API 消费者
        #: （CLI / IPC）同样要能拿到曲线；GUI 那个自己攒的环形缓冲
        #: 只是后端不可用时的兜底。
        self._speed_history: dict[str, deque[tuple[float, float]]] = {}

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
        self._sync_chunk_cap()

    def set_chunk_limits(self, initial_connections: int, max_chunks: int) -> None:
        """设置页的「初始连接数 / 分片上限」——**以前这两个是死设置**。

        界面上能改，引擎压根不读，用户调完发现什么都没变，只能得出
        「这程序没有动态分段能力」的结论。

        - `initial_connections` → 起步时开几条连接（默认 2，保守）。
        - `max_chunks` → 分片数的硬上限。分片数**必须大于连接数**，
          否则空闲连接没活干，下载末尾会长出很长的尾巴。

        上限被用户设得比连接数还小时，**按连接数的 4 倍兜底**——
        宁可多分片（每个分片一个独立请求，有开销），也不要长尾。
        """
        self.strategy.initial_chunks = max(1, min(32, int(initial_connections)))
        self._user_max_chunks = max(1, min(512, int(max_chunks)))
        self._sync_chunk_cap()

    def _sync_chunk_cap(self) -> None:
        """分片数硬上限 = 用户设的上限 和 「连接数 × 4」里大的那个。"""
        floor = max(1, self.parallelism_cap) * 4
        self.strategy.max_chunks = max(self._user_max_chunks, floor, 32)

    async def set_ignore_ssl(self, value: bool) -> None:
        self.downloader.set_ignore_ssl(value)

    async def set_user_agent(self, value: str | None) -> None:
        """换全局 User-Agent。

        空 = 恢复内置默认。**站点规则的 UA 仍然优先**——规则是
        用户为某个主机显式配的，覆盖全局设置。

        跟 SSL 开关一样是 async：改 UA 等于重建 httpx 客户端，
        这件事应该在引擎自己的事件循环里做，别从别的线程插手。
        """
        self.downloader.set_user_agent(value)

    async def set_global_proxy(self, proxy: str | None) -> None:
        """全局代理（规格 §1.6）。空 / None = 直连。

        **格式**：`http://host:port` / `https://…` / `socks5://…` /
        `socks4://…`，可以带用户名密码
        （`socks5://user:pass@host:port`）。
        """
        self.downloader.set_proxy(proxy)

    async def set_task_proxy(self, task_id: str, proxy: str | None) -> None:
        """任务级代理覆盖。"""
        options = self._task_options.setdefault(task_id, {})
        options["proxy"] = proxy or None
        summary = self.tasks.get(task_id)
        if summary is not None:
            summary.proxy = proxy or None

    async def probe_url(self, url: str, referer: str | None = None) -> dict:
        """探测 URL，给新建任务对话框用（规格 §2.2 的自动解析）。

        **探测失败不是终态。** 返回 `ok=False` 和错误信息，调用方
        让用户改完重试——不能因为一次探测失败就把对话框关了。
        """
        headers: dict[str, str] = {}
        if referer:
            headers["Referer"] = referer
        try:
            probe = await self.downloader.probe(url, extra_headers=headers or None)
        except ProbeError as err:
            return {
                "ok": False,
                "filename": None,
                "size": None,
                "final_url": None,
                "accepts_ranges": False,
                "error": err.message,
                "error_category": str(err.category),
            }
        except Exception as err:
            return {
                "ok": False,
                "filename": None,
                "size": None,
                "final_url": None,
                "accepts_ranges": False,
                "error": str(err),
                "error_category": str(ErrorCategory.NETWORK),
            }
        return {
            "ok": True,
            "filename": probe.suggested_filename,
            "size": probe.content_length,
            "final_url": probe.url,
            "accepts_ranges": bool(probe.range_supported),
            # **把分类一起给出去。** 添加对话框据此把「自动」换成
            # 具体的分类，保存目录也跟着落到对应的子目录里——
            # 用户加一个 .dmg，不用手动选就知道它该进 Software。
            "category": category_for_download(url, probe.suggested_filename).value,
            "error": None,
            "error_category": None,
        }

    async def set_task_checksum(self, task_id: str, algorithm: str | None) -> None:
        """设置任务的校验算法（规格 §1.7）。"""
        value = (algorithm or "").strip().lower() or None
        if value not in (None, "md5", "sha1", "sha256"):
            return
        summary = self.tasks.get(task_id)
        if summary is not None:
            summary.checksum_algorithm = value
            # 换算法就清掉旧值——旧值是另一个算法的结果，留着会误导。
            summary.checksum_value = None
        options = self._task_options.setdefault(task_id, {})
        options["checksum_algorithm"] = value

    async def set_task_on_complete(self, task_id: str, action: str, script: str = "") -> None:
        """设置任务的完成后行为（规格 §1.7）。"""
        valid = ("none", "open_file", "open_folder", "run_script", "sleep")
        value = action if action in valid else "none"
        summary = self.tasks.get(task_id)
        if summary is not None:
            summary.on_complete = value
        options = self._task_options.setdefault(task_id, {})
        options["on_complete"] = value
        if script:
            options["on_complete_script"] = script

    async def set_task_options(self, task_id: str, options: dict) -> None:
        """一次性改任务的若干可选项。"""
        if not isinstance(options, dict):
            return
        if "checksum" in options or "checksum_algorithm" in options:
            await self.set_task_checksum(
                task_id,
                options.get("checksum", options.get("checksum_algorithm")),
            )
        if "on_complete" in options:
            await self.set_task_on_complete(
                task_id,
                str(options.get("on_complete") or "none"),
                str(options.get("on_complete_script") or ""),
            )
        if "proxy" in options:
            await self.set_task_proxy(task_id, options.get("proxy"))
        if "chunk_count" in options:
            try:
                count = int(options.get("chunk_count"))
            except (TypeError, ValueError):
                count = 0
            if count > 0:
                self._task_options.setdefault(task_id, {})["chunk_count"] = count

    def get_task_error(self, task_id: str) -> dict | None:
        """取任务最后的结构化错误（规格 §5.1）。

        UI 的「复制错误详情」按钮用它——用户报障时能把分类、状态码、
        技术细节一起发出来。
        """
        return self._task_errors.get(task_id)

    async def retry_task(self, task_id: str) -> None:
        """手动重试：从持久化的进度恢复，不是从头再来（规格 §5.2）。

        会话文件还在就按它的布局续传；会话文件没了（用户清理过
        目录）就重新开始。
        """
        summary = self.tasks.get(task_id)
        if summary is None:
            return
        self._task_errors.pop(task_id, None)
        summary.error_message = None
        summary.error_category = None
        summary.error_retryable = False
        await self.resume(task_id)

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
        """启动一个或多个下载，返回任务 id 列表。

        任务先进队列（规格 §1.8）。同时活跃的任务数有上限，
        超出的停在 `queued`，有槽位时自动开跑。
        """
        ids: list[str] = []
        for spec in specs:
            task_id = str(uuid.uuid4())
            filename = sanitize_filename(
                spec.filename_override or self.filename_for(spec.url),
                fallback="download.bin",
            )
            destination = Path(spec.destination_directory) / filename

            # 分类：用户显式选的优先，否则按 URL 猜一个「预估值」。
            # 预估值在探测拿到真实文件名之后还会被修正（见 _run_task）。
            chosen = _resolve_category_id(spec.category)
            summary = TaskSummary(
                id=task_id,
                url=spec.url,
                state=TaskState.QUEUED.value,
                destination=str(destination),
                category=chosen or category_from_url(spec.url).value,
                filename=filename,
                started_at=time.time(),
                checksum_algorithm=spec.checksum_algorithm,
                on_complete=spec.on_complete or "none",
            )
            self.tasks[task_id] = summary

            live = LiveTask(task_id, spec)
            self.live[task_id] = live
            self._specs[task_id] = spec
            if spec.on_complete:
                self._task_options[task_id] = {
                    "on_complete": spec.on_complete,
                    "on_complete_script": spec.on_complete_script,
                }
            ids.append(task_id)

            # **队列是 FIFO**（规格 §1.8）。新任务一律排到队尾——
            # 「立即开始」不等于「插队」，插队会让用户前面排着的
            # 任务莫名其妙被推后。
            #
            # `scheduled` 的任务**不进队列**：它等计划任务触发，
            # 在那之前一直停在 queued 状态。放进队列的话它会占着
            # 队首，把后面的全堵住。
            if (spec.start_when or "now").lower() != "scheduled":
                self._queue.append(task_id)

        self._schedule()
        return ids

    # -- 队列 --------------------------------------------------------

    def _active_count(self) -> int:
        """当前正在跑的任务数。"""
        return sum(
            1 for live in self.live.values() if live.task is not None and not live.task.done()
        )

    def _schedule(self) -> None:
        """把队列里的任务开跑，直到活跃数达上限。

        **必须在事件循环里调用**——它要 `create_task`。
        """
        while self._queue and self._active_count() < self.max_concurrent_tasks:
            task_id = self._queue.pop(0)
            live = self.live.get(task_id)
            summary = self.tasks.get(task_id)
            if live is None or summary is None:
                continue
            if live.task is not None and not live.task.done():
                continue
            summary.state = TaskState.DOWNLOADING.value
            live.task = asyncio.create_task(self._run_task(task_id, live))
        self._refresh_queue_positions()

    def _refresh_queue_positions(self) -> None:
        """更新每个排队任务的位置（0 = 下一个就跑）。"""
        for index, task_id in enumerate(self._queue):
            summary = self.tasks.get(task_id)
            if summary is not None:
                summary.queued_position = index

    def queue_order(self) -> list[str]:
        """当前队列顺序（任务 id，从队首到队尾）。

        给 UI 显示队列用的——「置顶 / 置底」之后用户得能看到结果。
        """
        return list(self._queue)

    async def move_task(self, task_id: str, position: str) -> None:
        """把任务置顶 / 置底（规格 §1.8「支持手动置顶/置底」）。

        只对**排队中**的任务有意义——已经在跑的任务动不了。
        """
        if task_id not in self._queue:
            return
        self._queue.remove(task_id)
        if position == "top":
            self._queue.insert(0, task_id)
        elif position == "bottom":
            self._queue.append(task_id)
        else:
            self._queue.append(task_id)
        self._refresh_queue_positions()

    def set_max_concurrent_tasks(self, value: int) -> None:
        """改同时活跃的任务数上限。"""
        self.max_concurrent_tasks = max(1, int(value))
        self._schedule()

    async def _run_task(self, task_id: str, live: LiveTask) -> None:
        """任务的执行体。"""
        spec = live.spec
        summary = self.tasks.get(task_id)
        if summary is None:
            return
        summary.state = TaskState.DOWNLOADING.value
        # 从这一刻起还没收到字节——界面显示「连接中」而不是 0 KB/s。
        summary.connecting = True

        # 任务级的请求头（规格 §1.6）。浏览器接管带过来的 Cookie /
        # UA 在这里生效——拿不到登录态就下不动。
        headers: dict[str, str] = {}
        if spec.referer:
            headers["Referer"] = spec.referer
        headers.update(spec.headers or {})

        # 探测一次拿真实文件名和总长度。
        # 用共享的 downloader——Swift v1.1.8 在这里另建了一个
        # HTTPDownloader()，那个实例没有 SSL 绕过开关，
        # CDN 拒证书时探测静默失败，任务就永远卡在「下载中」。
        #
        # **探到的结果直接交给下面的流程层**，流程层不再探第二次
        # （早先两边各探一次 = 两个 RTT，用户看到的是 5 秒以上的
        # 纯等待，还没有任何提示）。
        probe: ContentProbe | None = None
        probe_error: ProbeError | None = None
        try:
            probe = await self.downloader.probe(
                spec.url,
                # 任务级的头——探测阶段有些站点就 403。
                extra_headers=headers or None,
            )
        except ProbeError as err:
            # 探测失败**不直接判死**：把错误带下去，让流程层统一
            # 分类（403 进「等待新链接」，404 进「文件不存在」…）。
            # 流程层拿到这个错误不会再探一次——探测本身已经带
            # 重试和预算了，再探一遍只是让用户等两倍时间。
            probe_error = err
        except Exception as err:
            probe_error = ProbeError(str(err) or type(err).__name__)

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

            # **分类按真实文件名修正。**
            #
            # URL 可能是无扩展名的 CDN 路径（`/download/12345`），
            # 服务器在 `Content-Disposition` 里给的真实文件名才是
            # 文件真正的类型。用户没手动选分类时（自动），这里用
            # 真实文件名重判一次；用户手动选了就一个字都不改。
            if not _resolve_category_id(spec.category):
                refined = category_for_download(spec.url, probe.suggested_filename)
                if refined.value != summary.category:
                    log.info(
                        CAT_ENGINE,
                        f"任务 {summary.filename} 的分类按真实文件名修正: "
                        f"{summary.category} → {refined.value}",
                    )
                    summary.category = refined.value

        destination = Path(summary.destination)

        # 磁盘空间预检（规格 §5.3）。空间不够就别开始了——
        # 下到 99% 再失败，用户已经等了几十分钟。
        disk_error = self._check_disk_space(destination.parent, summary.total_bytes)
        if disk_error is not None:
            summary.state = TaskState.FAILED.value
            summary.error_message = disk_error.message
            summary.error_category = str(disk_error.category)
            summary.error_retryable = disk_error.retryable
            summary.finished_at = time.time()
            self._task_errors[task_id] = disk_error.to_dict()
            log.error(CAT_ENGINE, f"任务失败 {summary.filename}: {disk_error.detail}")
            self._record_history(summary)
            self._finish_task(task_id)
            return

        # 名字冲突（规格 §5.3）。默认「保留两者」——把用户已有的
        # 文件悄悄覆盖掉是不可逆的损失。
        destination = self._resolve_name_conflict(destination)
        summary.destination = str(destination)
        summary.filename = destination.name

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
            # 任务级的头（Referer / Cookie / UA …）。
            #
            # `add` 方法可以带 referer 参数（浏览器扩展派单时会把
            # 页面地址带过来），这个头必须真的发出去——很多站点要
            # Referer 才给下载。
            #
            # 站点规则的 Referer 优先级更高（在 http_downloader 里
            # 合并时规则覆盖任务级），规则是用户为该主机显式配置的
            # 全局设置。
            headers=headers or None,
            initial_connections=self.strategy.initial_chunks,
            chunk_count=spec.chunk_count_override,
            checksum_algorithm=spec.checksum_algorithm,
            # 探测结果复用：流程层不再探第二次。
            probe=probe,
            probe_error=probe_error,
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
            outcome = await live.flow.run()
            summary.state = TaskState.COMPLETED.value
            summary.bytes_done = summary.total_bytes or summary.bytes_done
            summary.server_supports_ranges = outcome.server_supports_ranges
            if outcome.checksum:
                summary.checksum_value = outcome.checksum
                summary.checksum_algorithm = outcome.checksum_algorithm
            self._zero_speed(summary)
            summary.finished_at = time.time()
            log.notice(CAT_ENGINE, f"任务完成 {summary.filename}")
            self._record_history(summary)
            self._run_on_complete(summary)
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
            task_error = err.to_task_error()
            self._task_errors[task_id] = task_error.to_dict()
            summary.error_category = str(task_error.category)
            summary.error_retryable = task_error.retryable
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
            task_error = err.to_task_error()
            self._task_errors[task_id] = task_error.to_dict()
            summary.state = TaskState.FAILED.value
            summary.error_message = err.detail or str(err)
            summary.error_category = str(task_error.category)
            summary.error_retryable = task_error.retryable
            self._zero_speed(summary)
            summary.finished_at = time.time()
            self._record_history(summary)
        finally:
            progress_task.cancel()
            # 任务结束（成功/失败/取消/暂停）——不再「连接中」。
            summary.connecting = False
            self.live.pop(task_id, None)
            # 任务结束：分片明细留一份最终状态，详情面板还能查。
            # 超过保留上限就丢最老的。
            self._retire_chunk_states(task_id)
            self._finish_task(task_id)

    def _finish_task(self, task_id: str) -> None:
        """任务收尾：释放队列槽位，让下一个排队的开跑。"""
        if task_id in self._queue:
            self._queue.remove(task_id)
        self._schedule()

    # -- 磁盘 / 名字冲突 ---------------------------------------------

    @staticmethod
    def _check_disk_space(directory: Path, total_bytes: int | None) -> TaskError | None:
        """目标分区的可用空间够不够（规格 §5.3）。

        留 5% 余量：文件系统还要写元数据、日志、sidecar。
        大小未知时不检查——下到一半失败比开始就失败更糟。
        """
        if not total_bytes or total_bytes <= 0:
            return None
        try:
            usage = shutil.disk_usage(str(directory if directory.exists() else Path.home()))
        except OSError:
            return None
        needed = int(total_bytes * 1.05)
        if usage.free >= needed:
            return None
        return TaskError(
            category=ErrorCategory.DISK,
            message="磁盘空间不足",
            detail=(
                f"需要 {needed / 1024 / 1024:.1f} MB（含 5% 余量），"
                f"可用 {usage.free / 1024 / 1024:.1f} MB"
            ),
            retryable=False,
        )

    def _resolve_name_conflict(self, destination: Path) -> Path:
        """目标文件已存在时怎么办（规格 §5.3）。

        - `keep_both`（默认）：加序号，两个都留着。
        - `overwrite`：旧的挪去回收站；挪不动就退回保留两者。
        - `ask`：引擎层没法问，按保留两者处理（UI 层会先问一遍）。

        **占位者是未完成的任务时不适用「覆盖」**——那是同一个任务
        的中间产物，覆盖等于自毁。
        """
        if not destination.exists():
            return destination
        if self._is_unfinished_artifact(destination):
            return _resolve_keep_both(destination)
        policy = self.name_conflict_policy
        if policy == NAME_CONFLICT_OVERWRITE and _trash_or_rename(destination):
            return destination
        return _resolve_keep_both(destination)

    def _is_unfinished_artifact(self, destination: Path) -> bool:
        """目标位置是不是某个未完成任务的产物。"""
        for summary in self.tasks.values():
            if summary.state in (TaskState.COMPLETED.value,):
                continue
            if summary.destination and Path(summary.destination) == destination:
                return True
        return False

    # -- 完成后行为 --------------------------------------------------

    def _run_on_complete(self, summary: TaskSummary) -> None:
        """完成后行为（规格 §1.7）：打开文件 / 打开文件夹 / 跑脚本 / 休眠。

        默认「无操作」。**任何一步失败都不能让任务变成失败**——
        文件已经下好了，「打开所在文件夹」打不开不是下载的问题。
        """
        options = self._task_options.get(summary.id, {})
        action = options.get("on_complete") or summary.on_complete or "none"
        if action in ("", "none"):
            return
        try:
            if action == "open_file":
                _open_path(Path(summary.destination))
            elif action == "open_folder":
                _reveal_path(Path(summary.destination))
            elif action == "run_script":
                script = options.get("on_complete_script", "")
                if script:
                    _run_script(Path(script))
            elif action == "sleep":
                _sleep_machine()
        except Exception as err:
            log.warning(CAT_ENGINE, f"完成后行为失败（{action}）: {err}")

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
            # 收到字节了——不再是「连接中」。从这一刻起速度才有意义。
            if total > 0 and summary.connecting:
                summary.connecting = False
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
        self._push_speed_sample(summary.id, out.displayed_speed)
        return out.displayed_speed

    #: 速度曲线的采样窗口（秒）。规格 §2.3 要的是「最近 60 秒」。
    SPEED_HISTORY_WINDOW = 60.0

    def _push_speed_sample(self, task_id: str, speed: float) -> None:
        """记一个速度采样点。

        **窗口按时间剪，不按条数剪**：条数剪会让慢速任务（比如每秒
        只推一次进度）的曲线只剩几个点，图形上是一条横线，看不出
        速度变化；按时间剪则不管多慢都是「最近 60 秒」。
        """
        now = time.time()
        buf = self._speed_history.get(task_id)
        if buf is None:
            buf = deque()
            self._speed_history[task_id] = buf
        buf.append((now, max(0.0, speed)))
        cutoff = now - self.SPEED_HISTORY_WINDOW
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def get_speed_history(self, task_id: str) -> list[dict]:
        """最近 60 秒的速度采样（契约 §3.1）。

        返回 `[{"t": 时间戳, "v": 字节/秒}, ...]`，按时间升序。

        任务不在引擎里（比如刚启动还没入队）时返回空列表，
        前端会退回自己攒的采样。
        """
        buf = self._speed_history.get(task_id)
        if not buf:
            return []
        return [{"t": round(ts, 3), "v": round(value, 2)} for ts, value in buf]

    def _zero_speed(self, summary: TaskSummary) -> None:
        """显示速度硬清零，EMA 缓存一起清掉。

        终态和暂停都走这里。**不是**让 EMA 衰减到 0——那正是
        Swift v1.0.7 的 bug：进度条满了速度还在显示。
        """
        summary.speed_bytes_per_sec = 0.0
        summary.speed_ema = 0.0
        # 曲线也要归零——否则详情面板看到的是「速度还在跑但任务
        # 已经停了」的曲线，跟进度条上那 0 对不上。
        self._push_speed_sample(summary.id, 0.0)

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
        # 暂停了就不是「连接中」了。
        summary.connecting = False
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

        options = self._task_options.get(task_id, {})
        original = self._specs.get(task_id)
        spec = StartSpec(
            url=summary.url,
            destination_directory=Path(summary.destination).parent,
            filename_override=Path(summary.destination).name,
            referer=original.referer if original else None,
            headers=dict(original.headers) if original else {},
            proxy=options.get("proxy"),
            chunk_count_override=options.get("chunk_count"),
            checksum_algorithm=options.get("checksum_algorithm", summary.checksum_algorithm),
            on_complete=options.get("on_complete", summary.on_complete),
            on_complete_script=options.get("on_complete_script", ""),
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
            summary.connecting = False
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
        分片文件名——分片名是 `downloads/<key>.sdm.partial-<id>`。
        """
        if flow is None:
            live = self.live.get(task_id)
            flow = live.flow if live is not None else None
        if flow is not None:
            flow.discard_partials()
        if summary is None:
            return
        base = Path(summary.destination)
        # 工作目录里的分片。
        try:
            for path in work_root().glob(task_key(base) + PARTIAL_INFIX + "*"):
                _unlink_with_retry(path)
        except OSError:
            pass
        # 老位置（升级前下载目录里）的分片，也顺手清掉。
        work_dir.remove_legacy_partials(base)

    async def remove(self, task_id: str) -> None:
        """从队列移除。磁盘上的文件不动。"""
        await self.cancel(task_id)
        self.tasks.pop(task_id, None)
        self.live.pop(task_id, None)
        self._task_limiters.pop(task_id, None)
        self._chunk_states.pop(task_id, None)
        self._speed_history.pop(task_id, None)
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
        # 升级前的 sidecar 还在下载目录旁边，搬过来。
        sidecar = work_dir.adopt_legacy_sidecar(destination)
        original = None
        if sidecar is not None and sidecar.exists():
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
