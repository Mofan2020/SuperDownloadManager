"""集中日志系统。

对应 Swift 版的 Engine/SDMLogger.swift：分级、落盘、轮转、自动清理、
可预览。Python 版额外做了一件事——同时写 stderr，便于 GUI 模式下
从终端抓日志。

线程安全靠一把全局锁；写入走后台线程，不阻塞下载热路径。
"""

from __future__ import annotations

import enum
import queue
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import paths
from .console import safe_print


class LogLevel(enum.IntEnum):
    DEBUG = 0
    INFO = 1
    NOTICE = 2
    WARNING = 3
    ERROR = 4

    @property
    def display_name(self) -> str:
        return {
            LogLevel.DEBUG: "Debug",
            LogLevel.INFO: "Info",
            LogLevel.NOTICE: "Notice",
            LogLevel.WARNING: "Warning",
            LogLevel.ERROR: "Error",
        }[self]

    @property
    def short_tag(self) -> str:
        return {
            LogLevel.DEBUG: "DBG",
            LogLevel.INFO: "INF",
            LogLevel.NOTICE: "NTC",
            LogLevel.WARNING: "WRN",
            LogLevel.ERROR: "ERR",
        }[self]


@dataclass
class LogSettings:
    level: LogLevel = LogLevel.INFO
    retention_days: int = 14
    max_bytes_per_file: int = 4 * 1024 * 1024
    max_lines: int = 5000
    persistence_enabled: bool = True

    def validated(self) -> LogSettings:
        """夹紧用户填的值，避免 -7 或 999999999 把清理逻辑弄崩。"""
        return LogSettings(
            level=LogLevel(min(LogLevel.ERROR, max(LogLevel.DEBUG, self.level))),
            retention_days=min(365, max(1, self.retention_days)),
            max_bytes_per_file=min(256 * 1024 * 1024, max(32 * 1024, self.max_bytes_per_file)),
            max_lines=min(1_000_000, max(100, self.max_lines)),
            persistence_enabled=self.persistence_enabled,
        )


@dataclass
class LogEntry:
    timestamp: datetime
    level: LogLevel
    category: str
    message: str

    def format(self) -> str:
        return (
            f"{self.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"[{self.level.short_tag}] [{self.category}] {self.message}"
        )


class SDMLogger:
    """进程级单例日志器。

    写入路径：内存队列 → 后台线程 → 落盘。
    这样下载热路径上的 log 调用只是 put 到队列，不做磁盘 IO。
    """

    _instance: SDMLogger | None = None
    _instance_lock = threading.Lock()

    #: 每日清理的默认间隔（24 小时）。对应 Swift 版
    #: `SDMLogger.init` 里 `Timer.scheduledTimer(withTimeInterval: 24*3600)`。
    #: **测试可以注入更短的间隔**——生产默认就是 24 小时。
    DEFAULT_CLEANUP_INTERVAL = 24 * 3600

    def __init__(
        self,
        cleanup_interval: float | None = None,
        log_dir: Path | None = None,
        cleanup_enabled: bool | None = None,
    ) -> None:
        self._settings = LogSettings()
        # 有界队列：高速下载时每个分片都写日志，无界队列会一直涨。
        # 满了就丢最旧的——日志丢几条不影响功能，但内存不能无限涨。
        self._queue: queue.Queue[LogEntry | None] = queue.Queue(maxsize=2000)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._file_size = 0
        self._recent: list[LogEntry] = []
        self._recent_lock = threading.Lock()
        self._stderr = False
        self._started = False
        # 每日清理定时器。间隔可注入（测试用短间隔），
        # 停止靠 Event 而不是 sleep——Event.wait 到点就醒，
        # 不用等满一个周期。
        self._cleanup_interval = (
            self.DEFAULT_CLEANUP_INTERVAL if cleanup_interval is None else float(cleanup_interval)
        )
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        # 日志目录覆盖。生产用 paths.log_dir()，测试传临时目录——
        # 清理逻辑会**删文件**，绝不能让测试碰到用户真实的日志目录。
        self._log_dir_override = Path(log_dir) if log_dir is not None else None
        # 清理开关。默认开着，但 pytest 进程里必须关掉——
        # `get_logger()` 在模块 import 时就会把单例起起来，
        # 定时器随之启动，清理会删掉用户真实日志目录里的过期文件。
        # 测试要验证清理逻辑就自己 new 一个带 log_dir= 的实例，
        # 显式 cleanup_enabled=True。
        if cleanup_enabled is None:
            cleanup_enabled = "pytest" not in sys.modules
        self._cleanup_enabled = bool(cleanup_enabled)
        # 落盘用的锁。写线程和清理线程都要动同一个文件，
        # 清理是「读出来重写」，写是「追加」——不串起来的话
        # 清理会把刚写进去的几行冲掉。
        self._file_lock = threading.Lock()

    @classmethod
    def shared(cls) -> SDMLogger:
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
                cls._instance.start()
            return cls._instance

    # -- 生命周期 ----------------------------------------------------

    def start(self, to_stderr: bool = False) -> None:
        if self._started:
            return
        self._started = True
        self._stderr = to_stderr
        if self._log_dir_override is not None:
            # 测试实例：只建自己的目录，不碰用户的真实路径。
            self._log_dir_override.mkdir(parents=True, exist_ok=True)
        else:
            paths.ensure_dirs()
        self._file_size = self._current_file_size()
        self._thread = threading.Thread(target=self._writer_loop, name="sdm-logger", daemon=True)
        self._thread.start()
        self.start_cleanup_timer()

    def stop(self) -> None:
        if not self._started:
            return
        self.stop_cleanup_timer()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._started = False

    # -- 每日定时清理 ------------------------------------------------

    def start_cleanup_timer(self) -> None:
        """启动每日清理定时器（幂等）。

        对应 Swift 版 `SDMLogger.init` 里的
        `Timer.scheduledTimer(withTimeInterval: 24*3600, repeats: true)`。
        Swift 版那个 Timer 在 init 里就挂上了；Python 版挂在
        `start()` 上——定时器属于运行期的东西，进程没跑起来
        不需要清理。

        间隔默认 24 小时，构造时传 `cleanup_interval=` 可注入
        更短的间隔（测试用，生产别改）。
        """
        if not self._cleanup_enabled:
            return
        if self._cleanup_thread is not None and self._cleanup_thread.is_alive():
            return
        self._cleanup_stop.clear()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="sdm-log-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def stop_cleanup_timer(self) -> None:
        """停掉每日清理定时器。

        用 Event 而不是让线程自己跑完一轮——`Event.wait(timeout)`
        会被 set 立刻唤醒，不用干等一个 24 小时周期。
        """
        self._cleanup_stop.set()
        thread = self._cleanup_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._cleanup_thread = None

    def _cleanup_loop(self) -> None:
        """定时清理的循环体。

        启动时先跑一次清理（Swift 版在 `updateSettings` 里
        补跑一次），之后每 `cleanup_interval` 秒再跑一次。

        `Event.wait(timeout)` 返回 True 表示被 stop 唤醒，
        循环退出。清理本身不抛异常——定时器线程死了就没人在
        下一次周期再清理了。
        """
        # 先清一次：进程可能挂了很久没清理过（比如机器睡眠）。
        try:
            self._run_cleanup()
        except Exception:
            pass
        while not self._cleanup_stop.wait(self._cleanup_interval):
            try:
                self._run_cleanup()
            except Exception:
                pass

    # -- 设置 --------------------------------------------------------

    @property
    def settings(self) -> LogSettings:
        with self._lock:
            return self._settings

    def update_settings(self, settings: LogSettings) -> None:
        validated = settings.validated()
        with self._lock:
            self._settings = validated
        # 设置变更时立刻清一次（Swift 版 updateSettings 同理）。
        # cleanup_enabled=False 时跳过——清理会删文件，pytest 进程里
        # 不能让它碰用户真实的日志目录。
        if self._cleanup_enabled:
            self._run_cleanup()

    # -- 写入 --------------------------------------------------------

    def log(self, level: LogLevel, category: str, message: str) -> None:
        with self._lock:
            threshold = self._settings.level
        if level < threshold:
            return
        entry = LogEntry(timestamp=datetime.now(), level=level, category=category, message=message)
        with self._recent_lock:
            self._recent.append(entry)
            if len(self._recent) > 500:
                del self._recent[:-500]
        if self._stderr:
            # 用 safe_print 而不是 print：Windows 控制台默认 GBK，
            # 日志里的中文路径会抛 UnicodeEncodeError。
            safe_print(f"[{entry.level.short_tag}] [{category}] {message}")
        if self._started:
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                # 队列满：丢掉最旧的一条，腾位置给新的
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(entry)
                except (queue.Empty, queue.Full):
                    pass

    def debug(self, category: str, message: str) -> None:
        self.log(LogLevel.DEBUG, category, message)

    def info(self, category: str, message: str) -> None:
        self.log(LogLevel.INFO, category, message)

    def notice(self, category: str, message: str) -> None:
        self.log(LogLevel.NOTICE, category, message)

    def warning(self, category: str, message: str) -> None:
        self.log(LogLevel.WARNING, category, message)

    def error(self, category: str, message: str) -> None:
        self.log(LogLevel.ERROR, category, message)

    # -- 落盘 --------------------------------------------------------

    @property
    def log_dir(self) -> Path:
        """日志目录。测试可以覆盖（构造参数 log_dir=）。"""
        if self._log_dir_override is not None:
            return self._log_dir_override
        return paths.log_dir()

    @property
    def file_path(self) -> Path:
        return self.log_dir / "sdm.log"

    def _current_file_size(self) -> int:
        try:
            return self.file_path.stat().st_size
        except OSError:
            return 0

    def _writer_loop(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                break
            try:
                self._append_to_disk(entry)
            except Exception:
                pass
        # 退出前把剩余的排空
        while True:
            try:
                entry = self._queue.get_nowait()
            except queue.Empty:
                break
            if entry is None:
                break
            try:
                self._append_to_disk(entry)
            except Exception:
                pass

    def _append_to_disk(self, entry: LogEntry) -> None:
        with self._lock:
            settings = self._settings
        if not settings.persistence_enabled:
            return
        line = entry.format() + "\n"
        # 落盘全程持锁：清理线程可能正在重写同一个文件。
        with self._file_lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.file_path, "a", encoding="utf-8") as fh:
                fh.write(line)
            self._file_size += len(line.encode("utf-8"))
            if self._file_size > settings.max_bytes_per_file:
                self._rotate()

    def _rotate(self) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d")
        archive = self.log_dir / f"sdm-{stamp}.log"
        index = 0
        while archive.exists():
            index += 1
            archive = self.log_dir / f"sdm-{stamp}.{index}.log"
        try:
            self.file_path.rename(archive)
        except OSError:
            # 轮转失败就让缓存大小归零，下次写会再试
            self._file_size = 0
            return
        self._file_size = 0

    def _run_cleanup(self) -> None:
        """删掉过期的轮转文件，并把当前文件的行数压到上限。

        清理是「读出来重写」，写线程是「追加」——两者动同一个
        文件，必须串起来，否则刚写进去的几行会被清理冲掉。
        """
        settings = self.settings
        cutoff = datetime.now() - timedelta(days=settings.retention_days)
        try:
            entries = list(self.log_dir.iterdir())
        except OSError:
            return
        for path in entries:
            if not path.name.startswith("sdm-") or path.suffix != ".log":
                continue
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    path.unlink()
                except OSError:
                    pass
        # 行数上限：只裁当前文件，轮转文件不动
        with self._file_lock:
            try:
                raw = self.file_path.read_text(encoding="utf-8")
            except OSError:
                return
            lines = [ln for ln in raw.split("\n") if ln]
            if len(lines) > settings.max_lines:
                trimmed = "\n".join(lines[-settings.max_lines:]) + "\n"
                try:
                    self.file_path.write_text(trimmed, encoding="utf-8")
                    self._file_size = len(trimmed.encode("utf-8"))
                except OSError:
                    pass

    # -- 读取 --------------------------------------------------------

    def read_all(self) -> list[LogEntry]:
        """读出全部日志行（当前文件 + 轮转文件，按时间倒序）。"""
        entries: list[LogEntry] = []
        try:
            paths_list = sorted(
                (p for p in self.log_dir.iterdir() if p.suffix == ".log"),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            paths_list = []
        for path in paths_list:
            entries.extend(self._parse_file(path))
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def _parse_file(self, path: Path) -> list[LogEntry]:
        out: list[LogEntry] = []
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for line in raw.split("\n"):
            if not line.strip():
                continue
            parsed = self._parse_line(line)
            if parsed:
                out.append(parsed)
        return out

    @staticmethod
    def _parse_line(line: str) -> LogEntry | None:
        # 格式："YYYY-MM-DD HH:MM:SS.mmm [LVL] [cat] msg"
        try:
            ts_end = line.index("[")
            ts_str = line[:ts_end].strip()
            rest = line[ts_end:]
            lvl_end = rest.index("]")
            level_raw = rest[1:lvl_end].strip()
            rest2 = rest[lvl_end + 1:]
            cat_end = rest2.index("]")
            category = rest2[1:cat_end].strip()
            message = rest2[cat_end + 1:].strip()
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S.%f")
        except (ValueError, IndexError):
            return None
        level = next((lv for lv in LogLevel if lv.short_tag == level_raw), LogLevel.INFO)
        return LogEntry(timestamp=ts, level=level, category=category, message=message)

    def tail(self, count: int = 200) -> list[LogEntry]:
        """给 GUI 预览用的最近若干条。"""
        with self._recent_lock:
            return list(self._recent[-count:])


# 常用分类名
CAT_ENGINE = "engine"
CAT_NET = "net"
CAT_CHUNK = "chunk"
CAT_QUEUE = "queue"
CAT_IPC = "ipc"
CAT_GUI = "gui"
CAT_STORE = "store"


def get_logger() -> SDMLogger:
    return SDMLogger.shared()
