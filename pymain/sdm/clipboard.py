"""剪贴板监听：发现复制了下载链接就提示。

对应 Swift 版 App/MainWindow/DownloadListStore.swift 的
startClipboardMonitor / checkClipboard。

**这里保留 Swift v1.0.7 的修正**：v1.0.6 会把复制的每一个
http(s) 链接都自动加进下载，结果用户从聊天窗口复制一段话
就被弹一次下载确认。v1.0.7 起只对「看起来确实是文件」的链接
提示——扩展名能映射到已知分类才算。

跨平台取剪贴板：
- macOS 用 /usr/bin/pbpaste
- Windows 用 ctypes 调 user32.dll 的 GetClipboardData
- Linux 用 xclip / xsel，都没有就放弃（不弹窗）

**读不到的时候一定要出声。** 之前读失败只有一条 debug 级日志，
而 Windows 用户默认日志级别是 warning——debug 那条根本看不到，
于是「剪贴板监控用不了」这件事在用户那边完全没有任何线索。
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import threading
import time
from collections.abc import Callable

from .engine.categories import is_likely_file_url
from .logging_setup import CAT_GUI, get_logger

log = get_logger()

#: 读失败告警的节流间隔（秒）。
#:
#: 监听器每 0.5 秒轮询一次，不节流的话一次「打不开剪贴板」能刷出
#: 几千行日志，把真正的错误淹掉。30 秒一条足够定位问题。
_WARN_INTERVAL = 30.0

#: 上一次告警的时间 + 消息。同一类消息在节流窗口内只报一次。
_warn_state: dict[str, tuple[float, str]] = {}
_warn_lock = threading.Lock()


def _warn_read_failure(message: str) -> None:
    """读剪贴板失败时告警（带节流）。

    **不能静默。** 剪贴板监控是纯后台功能，用户看不到界面上的任何
    提示——它不工作的时候唯一的线索就是日志。日志级别低于 warning
    的话用户在设置里根本看不到。
    """
    now = time.monotonic()
    with _warn_lock:
        last_time, last_message = _warn_state.get(message, (0.0, ""))
        if now - last_time < _WARN_INTERVAL and last_message == message:
            return
        _warn_state[message] = (now, message)
    log.warning(CAT_GUI, f"剪贴板读取失败：{message}")


class ClipboardMonitor:
    """轮询剪贴板，发现新的下载链接就回调。

    **线程是常驻的。** 「开不开」由每轮读设置决定，不靠「谁负责
    起线程」。之前线程只在应用启动那一刻、且 `capture_clipboard`
    已经是开的时候才起——于是用户从设置页或 Windows 托盘把开关
    打开，**设置写对了但线程压根没跑**，剪贴板监控等于没有。
    """

    def __init__(
        self,
        on_candidate: Callable[[str], None],
        dedupe_seconds: float = 300.0,
        poll_interval: float = 0.5,
        idle_interval: float = 1.0,
    ) -> None:
        self._on_candidate = on_candidate
        self._dedupe = dedupe_seconds
        self._interval = poll_interval
        #: 关着的时候多久看一次开关。开着 0.5 秒，关着 1 秒——
        #: 关着时不必去动剪贴板，但要能及时发现用户把开关打开。
        self._idle_interval = idle_interval
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_change = -1
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sdm-clipboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            # **kill switch 每轮都查。**
            #
            # Swift 对照：`App/MainWindow/DownloadListStore.swift:388`
            # 的 `guard clipboardCapture`。
            #
            # 之前只在启动时判断一次（gui/main.py 里
            # `if settings.capture_clipboard: clipboard.start()`），
            # 用户在设置里关掉后，监听器还在跑——要重启 app 才生效。
            # 现在每轮读一次设置，下一轮就停。
            if not self._capture_enabled():
                # 关着的时候不碰剪贴板，只看开关有没有被打开。
                self._stop.wait(self._idle_interval)
                continue
            try:
                self._check()
            except Exception as err:
                _warn_read_failure(f"检查剪贴板时出错：{err}")
            self._stop.wait(self._interval)

    @staticmethod
    def _capture_enabled() -> bool:
        """用户有没有关掉剪贴板捕获。

        读 `SettingsStore.shared()`——那是单例，每轮读都能拿到最新值
        （用户在设置页改开关会写进同一个实例）。

        读不到就当开着——默认行为是捕获。
        """
        try:
            from .settings import SettingsStore

            return bool(SettingsStore.shared().settings.capture_clipboard)
        except Exception:
            return True

    def _check(self) -> None:
        text, change = read_clipboard()
        if text is None:
            return
        if change == self._last_change:
            return
        self._last_change = change

        candidate = text.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            return
        if not is_likely_file_url(candidate):
            return

        now = time.time()
        with self._lock:
            last = self._seen.get(candidate)
            if last is not None and now - last < self._dedupe:
                return
            self._seen[candidate] = now
            # 清掉过期记录，别让字典无限涨
            cutoff = now - self._dedupe * 2
            for key in [k for k, v in self._seen.items() if v < cutoff]:
                del self._seen[key]

        self._on_candidate(candidate)


# -- 跨平台剪贴板读取 ----------------------------------------------


def read_clipboard() -> tuple[str | None, int]:
    """读剪贴板。返回 (文本, 变更计数)。拿不到返回 (None, -1)。"""
    system = platform.system()
    if system == "Darwin":
        return _read_macos()
    if system == "Windows":
        return _read_windows()
    return _read_linux()


def _read_macos() -> tuple[str | None, int]:
    """读 macOS 剪贴板。

    **用 `pbpaste` 命令，不用 osascript。**

    之前写的是 `osascript -e "pbpaste"`——那是错的：`pbpaste` 是
    **shell 命令**，不是 AppleScript 命令。AppleScript 解释器把它
    当成一个变量名，退出码非 0，报的是「变量"pbpaste"没有定义」
    (-2753)。于是这一段**每一轮都失败**，返回 (None, -1)——
    **剪贴板监控从来没有工作过**，而日志里只有一条 debug 级记录，
    用户那边一点线索都没有。

    `pbpaste` 直接输出纯文本（剪贴板里是文件时输出空、退出码 0，
    不会误报），而且不用每 0.5 秒起一个 AppleScript 解释器。

    `pbpaste` 不存在时退到 `osascript -e "the clipboard"`——
    那是 AppleScript 里读剪贴板的**正确**写法（`the clipboard` 是
    Standard Additions 的属性，不是命令）。
    """
    for cmd in _macos_read_commands():
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as err:
            _warn_read_failure(f"{cmd[0]} 调不起来：{err}")
            continue
        if result.returncode != 0:
            _warn_read_failure(
                f"{cmd[0]} 退出码 {result.returncode}：{(result.stderr or '').strip()[:120]}"
            )
            continue
        text = result.stdout or ""
        return text, hash(text)
    return None, -1


def _macos_read_commands() -> list[list[str]]:
    """macOS 上读剪贴板的命令，按优先级排。"""
    commands: list[list[str]] = []
    pbpaste = shutil.which("pbpaste")
    if pbpaste:
        commands.append([pbpaste])
    commands.append(["osascript", "-e", "the clipboard"])
    return commands


def _read_windows() -> tuple[str | None, int]:
    """读 Windows 剪贴板。

    `ctypes.WinDLL` 只有 Windows 上才有——别的平台 import 不到，
    所以这段只在 Windows 上跑。
    """
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception as err:
        _warn_read_failure(f"加载 Win32 剪贴板 API 失败：{err}")
        return None, -1
    return _read_windows_with(user32, kernel32)


def _declare_clipboard_api(user32, kernel32) -> None:
    """给 Win32 剪贴板 API 声明参数和返回类型。

    **返回类型不声明就出大事。** `GetClipboardData` 返回的是
    `HANDLE`，而 ctypes 在没声明 `restype` 时默认按 `c_int` 返回
    ——64 位 Windows 上句柄被截断成 32 位。低 32 位最高位是 1 的
    时候（现代 Windows 的句柄值经常落在这个区间），
    `ctypes.c_void_p(handle)` 会把这个负数补成 `0xFFFFFFFFxxxxxxxx`
    的错指针，`GlobalLock` 拿不到数据 → 读出来是空 →
    **剪贴板监控等于没有**。

    所以 `restype` 必须是 `wintypes.HANDLE`（`c_void_p`），
    不能是默认的 `c_int`。
    """
    from ctypes import wintypes

    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.HANDLE
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = wintypes.BOOL


def _read_windows_with(user32, kernel32) -> tuple[str | None, int]:
    """真正读剪贴板的逻辑。

    **跟 Win32 的装载拆开**，这样在 macOS 上也能用假的 API 测它
    （真机上跑不到 Windows，测试必须能在开发机上守住这条路径）。
    """
    import ctypes

    _declare_clipboard_api(user32, kernel32)

    CF_UNICODETEXT = 13
    if not user32.OpenClipboard(None):
        # 别的程序正占着剪贴板是常事（复制的那一刻浏览器就占着），
        # 下一轮再来就是了。不算错误，别刷屏。
        log.debug(CAT_GUI, "剪贴板正被别的程序占用，本轮跳过")
        return None, -1
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            # 剪贴板里没有 Unicode 文本（复制的是文件、图片之类）。
            # 这不是错误，是正常情况。
            return None, -1
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            _warn_read_failure("GlobalLock 拿不到剪贴板数据")
            return None, -1
        try:
            text = ctypes.c_wchar_p(pointer).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
        return text, hash(text)
    finally:
        user32.CloseClipboard()


def _read_linux() -> tuple[str | None, int]:
    import shutil

    for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                return result.stdout, hash(result.stdout)
        except (OSError, subprocess.SubprocessError):
            continue
    return None, -1
