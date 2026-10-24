"""剪贴板监听：发现复制了下载链接就提示。

对应 Swift 版 App/MainWindow/DownloadListStore.swift 的
startClipboardMonitor / checkClipboard。

**这里保留 Swift v1.0.7 的修正**：v1.0.6 会把复制的每一个
http(s) 链接都自动加进下载，结果用户从聊天窗口复制一段话
就被弹一次下载确认。v1.0.7 起只对「看起来确实是文件」的链接
提示——扩展名能映射到已知分类才算。

跨平台取剪贴板：
- macOS 用 pbcopy/pbpaste（pbcopy 只管写，读用 AppKit 或
  osascript）
- Windows 用 ctypes 调 user32.dll 的 GetClipboardData
- Linux 用 xclip / xsel，都没有就放弃（不弹窗）
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time
from typing import Callable

from .engine.categories import is_likely_file_url
from .logging_setup import CAT_GUI, get_logger

log = get_logger()


class ClipboardMonitor:
    """轮询剪贴板，发现新的下载链接就回调。"""

    def __init__(
        self,
        on_candidate: Callable[[str], None],
        dedupe_seconds: float = 300.0,
        poll_interval: float = 0.5,
    ) -> None:
        self._on_candidate = on_candidate
        self._dedupe = dedupe_seconds
        self._interval = poll_interval
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
            try:
                self._check()
            except Exception as err:  # noqa: BLE001
                log.debug(CAT_GUI, f"剪贴板检查失败: {err}")
            self._stop.wait(self._interval)

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
    # 用 osascript 读 pbpaste 的内容，同时用 pasteboard 的 change count
    # 做变更判断。change count 拿不到时退化用内容哈希。
    try:
        result = subprocess.run(
            ["osascript", "-e", "pbpaste"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None, -1
        text = result.stdout
    except (OSError, subprocess.SubprocessError):
        return None, -1
    return text, hash(text)


def _read_windows() -> tuple[str | None, int]:
    try:
        import ctypes
        from ctypes import wintypes

        windll = getattr(ctypes, "windll")
        user32 = windll.user32
        kernel32 = windll.kernel32

        CF_UNICODETEXT = 13
        if not user32.OpenClipboard(None):
            return None, -1
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None, -1
            kernel32.GlobalLock.restype = ctypes.c_void_p
            pointer = kernel32.GlobalLock(ctypes.c_void_p(handle))
            if not pointer:
                return None, -1
            try:
                text = ctypes.c_wchar_p(pointer).value or ""
            finally:
                kernel32.GlobalUnlock(ctypes.c_void_p(handle))
            return text, hash(text)
        finally:
            user32.CloseClipboard()
    except Exception:  # noqa: BLE001
        return None, -1


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
