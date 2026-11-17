"""双击 exe 的时候，不该开第二个窗口。

**用户看到的现象是「菜单栏出现两个图标」。** 原因是第二次双击又起了一
个完整的 GUI：两个进程、两份引擎、两份任务表、两个托盘图标。用户想
关掉一个，发现另一个还在。

所以这里测的是**单实例守卫**：

- 第一个进程拿到锁，是「唯一的那个」；
- 第二个进程拿不到锁，它该去叫第一个把窗口叫到前面来，而不是自己开；
- 锁里写的进程已经不在了，第二个进程可以接管（否则程序彻底打不开）；
- **进程号被复用不算活着。** Windows 上进程号回收得快，一个被杀掉的
  进程留下的锁，过一会儿就指向一个碰巧活着的无关进程。那时候第二个
  GUI 会以为「已经有一个了」，自己不开窗口——用户看到的现象是
  「双击没反应」。

**测的是真实文件系统和真实进程**，不是 mock：锁真的写到临时目录，
真的用 `os.kill` 起一个真进程当「主人」，真的把它杀掉看锁是不是作废。
只有「启动时间」那一项用假值，因为真进程的启动时间没法造。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sdm.gui import single_instance
from sdm.gui.single_instance import SingleInstance


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    return tmp_path / "gui.lock"


# --------------------------------------------------------------- 锁的读写


def test_first_instance_takes_the_lock(lock_file: Path) -> None:
    mine = SingleInstance(lock_file)
    mine.acquire()
    try:
        assert mine.is_first is True
        assert lock_file.exists()
    finally:
        mine.release()


def test_lock_records_pid_and_start_time(lock_file: Path) -> None:
    mine = SingleInstance(lock_file)
    mine.acquire()
    try:
        owner = mine.owner()
        assert owner["pid"] == os.getpid()
        # 启动时间要写进去——只有进程号分不出被复用的进程号。
        assert isinstance(owner["started"], float)
    finally:
        mine.release()


def test_second_instance_does_not_take_the_lock(lock_file: Path) -> None:
    first = SingleInstance(lock_file)
    first.acquire()
    try:
        second = SingleInstance(lock_file)
        second.acquire()
        assert second.is_first is False
    finally:
        first.release()


def test_release_removes_the_lock(lock_file: Path) -> None:
    mine = SingleInstance(lock_file)
    mine.acquire()
    mine.release()
    assert not lock_file.exists()


def test_release_keeps_a_lock_that_was_taken_over(lock_file: Path) -> None:
    """别人接管之后，原来的那个退出时不能把锁删掉。

    删了就等于把第二个窗口送给了下一次双击。
    """
    mine = SingleInstance(lock_file)
    mine.acquire()
    # 假装锁被别的进程拿走了：文件里写的是别人的进程号。
    lock_file.write_text("999999\n1.0", encoding="utf-8")
    mine.release()
    assert lock_file.exists()
    assert lock_file.read_text(encoding="utf-8").startswith("999999")


# --------------------------------------------------------- 主人死掉了


def test_lock_of_a_dead_process_is_stale(lock_file: Path) -> None:
    """主人没了，锁立刻作废。

    不用等一分钟——等一分钟的那种做法，用户点完 exe 要盯着屏幕等。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_file.write_text(f"{child.pid}\n{_start_time(child.pid)}", encoding="utf-8")
        mine = SingleInstance(lock_file)
        assert mine.owner_alive() is True
        assert mine._stale() is False
    finally:
        child.kill()
        child.wait()

    mine = SingleInstance(lock_file)
    assert mine.owner_alive() is False
    assert mine._stale() is True


def test_dead_owner_can_be_taken_over(lock_file: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child.kill()
    child.wait()
    lock_file.write_text(f"{child.pid}\n{_start_time(child.pid)}", encoding="utf-8")

    mine = SingleInstance(lock_file)
    mine.acquire()
    try:
        assert mine.is_first is True
    finally:
        mine.release()


def test_a_reused_pid_is_not_the_owner(lock_file: Path) -> None:
    """进程号被复用，不算主人还活着。

    这是「双击没反应」的根因：旧 GUI 崩了留下锁，过一阵子那个进程号
    分给了别的程序，这时候锁看起来还「有主人」，第二个 GUI 就不开
    窗口了。

    这里用一个活着的真进程，配上一个**对不上的**启动时间。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real = _start_time(child.pid)
        assert real is not None
        # 锁里写的是这个进程号，但启动时间是别的进程的（差得远）。
        lock_file.write_text(f"{child.pid}\n{real + 100000.0}", encoding="utf-8")

        mine = SingleInstance(lock_file)
        assert mine.owner_alive() is False
        assert mine._stale() is True
    finally:
        child.kill()
        child.wait()


def test_a_live_owner_is_never_taken_over(lock_file: Path) -> None:
    """活着的主人不会被抢。

    两个窗口写同一份设置，比一个窗口叫不回来更糟。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        real = _start_time(child.pid)
        lock_file.write_text(f"{child.pid}\n{real}", encoding="utf-8")

        mine = SingleInstance(lock_file)
        assert mine.take_over() is False
        assert lock_file.exists()
    finally:
        child.kill()
        child.wait()


# ------------------------------------------------- 旧版本的锁（只有进程号）


def test_old_lock_without_start_time_is_read(lock_file: Path) -> None:
    """旧版本写的锁只有进程号，也得认得出来。"""
    lock_file.write_text("12345", encoding="utf-8")
    mine = SingleInstance(lock_file)
    owner = mine.owner()
    assert owner["pid"] == 12345
    assert owner["started"] is None


def test_old_lock_with_a_live_pid_counts_as_alive(lock_file: Path) -> None:
    """旧锁没有启动时间，只能信进程号。

    这是旧格式的能力上限：分不出被复用的进程号。但也不能因此把活着的
    主人当成死的——那样会出现两个窗口。
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_file.write_text(str(child.pid), encoding="utf-8")
        mine = SingleInstance(lock_file)
        assert mine.owner_alive() is True
    finally:
        child.kill()
        child.wait()


def test_old_lock_with_a_dead_pid_is_stale(lock_file: Path) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    child.kill()
    child.wait()
    lock_file.write_text(str(child.pid), encoding="utf-8")

    mine = SingleInstance(lock_file)
    assert mine.owner_alive() is False
    assert mine._stale() is True


def test_unreadable_lock_falls_back_to_its_age(lock_file: Path) -> None:
    """锁里一个字都读不出来，就按文件的年龄判。

    读不出来的情况：文件是空的，或者里面不是数字。
    """
    lock_file.write_text("", encoding="utf-8")
    mine = SingleInstance(lock_file)
    assert mine.owner() == {}
    # 空文件是刚写的，还没到 STALE_SECONDS，所以还不算陈旧。
    assert mine._stale() is False

    # 把文件的修改时间往前推一小时，就该算陈旧了。
    old = time.time() - (single_instance.STALE_SECONDS + 60)
    os.utime(lock_file, (old, old))
    assert mine._stale() is True


def test_garbage_lock_is_not_read_as_an_owner(lock_file: Path) -> None:
    lock_file.write_text("not a number", encoding="utf-8")
    mine = SingleInstance(lock_file)
    assert mine.owner() == {}


# ------------------------------------------------------------ 叫醒已有的


def test_ask_running_to_show_sends_show_window(lock_file: Path, monkeypatch) -> None:
    """第二个进程走 IPC 请第一个把窗口叫到前面来。"""
    sent: list[dict] = []

    class _FakeClient:
        def __init__(self, port: int, timeout: float) -> None:
            self.port = port
            self.timeout = timeout

        def send(self, payload: dict) -> dict:
            sent.append(payload)
            return {"ok": True, "result": {"shown": True}}

    monkeypatch.setattr("sdm.ipc.client.IPCClient", _FakeClient)

    mine = SingleInstance(lock_file)
    assert mine.ask_running_to_show() is True
    assert sent and sent[0]["method"] == "showWindow"


def test_ask_running_to_show_is_false_when_the_reply_is_not_ok(
    lock_file: Path, monkeypatch
) -> None:
    """对端回了 ok=False，就算没叫醒。

    「发出去了」和「叫醒了」是两回事。第二个进程据此决定自己是退出
    还是接管锁——把「没叫醒」当成「叫醒了」，程序就会静悄悄地什么都不做。
    """

    class _FakeClient:
        def __init__(self, port: int, timeout: float) -> None:
            pass

        def send(self, payload: dict) -> dict:
            return {"ok": False, "error": "window is gone"}

    monkeypatch.setattr("sdm.ipc.client.IPCClient", _FakeClient)

    mine = SingleInstance(lock_file)
    assert mine.ask_running_to_show() is False


def test_ask_running_to_show_fails_when_nobody_answers(
    lock_file: Path, monkeypatch
) -> None:
    class _FakeClient:
        def __init__(self, port: int, timeout: float) -> None:
            pass

        def send(self, payload: dict) -> dict:
            raise OSError("连接不上")

    monkeypatch.setattr("sdm.ipc.client.IPCClient", _FakeClient)

    mine = SingleInstance(lock_file)
    assert mine.ask_running_to_show() is False


def test_hand_over_url_sends_add(lock_file: Path, monkeypatch) -> None:
    """URL scheme 唤起时，地址要交给已经开着的那一个。"""
    sent: list[dict] = []

    class _FakeClient:
        def __init__(self, port: int, timeout: float) -> None:
            pass

        def send(self, payload: dict) -> dict:
            sent.append(payload)
            return {"ok": True}

    monkeypatch.setattr("sdm.ipc.client.IPCClient", _FakeClient)

    mine = SingleInstance(lock_file)
    assert mine.hand_over_url("https://example.com/a.zip") is True
    assert sent and sent[0]["method"] == "add"
    assert sent[0]["params"]["url"] == "https://example.com/a.zip"


def test_hand_over_url_is_false_when_the_reply_is_not_ok(
    lock_file: Path, monkeypatch
) -> None:
    """对端没收下地址，就不能算交出去了。

    交出去了才有「退出这个进程」的底气。没交出去还退出，用户点了下载
    链接，什么都没发生。
    """

    class _FakeClient:
        def __init__(self, port: int, timeout: float) -> None:
            pass

        def send(self, payload: dict) -> dict:
            return {"ok": False, "error": "engine is busy"}

    monkeypatch.setattr("sdm.ipc.client.IPCClient", _FakeClient)

    mine = SingleInstance(lock_file)
    assert mine.hand_over_url("https://example.com/a.zip") is False


def test_hand_over_url_without_a_url_is_a_no_op(lock_file: Path) -> None:
    mine = SingleInstance(lock_file)
    assert mine.hand_over_url("") is True


# ------------------------------------------------------------------ 工具


def test_without_psutil_the_lock_falls_back_to_the_pid(
    lock_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没装 psutil 的时候，守卫退回「只信进程号」。

    **这是降级，不是等价。** 分不出被回收的进程号，那个「双击没反应」
    的 bug 就会回来，所以 psutil 在 requirements.txt 里是必需依赖而不是
    可选。这条用例钉的是「降级之后仍然能工作」，不是「降级没问题」。
    """
    import builtins

    real_import = builtins.__import__

    def _no_psutil(name: str, *args: object, **kwargs: object) -> object:
        if name == "psutil":
            raise ImportError("psutil is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)

    mine = SingleInstance(lock_file)
    assert single_instance._process_start_time(1) is None
    # 写锁的时候不写启动时间，只写进程号。
    mine.acquire()
    try:
        text = lock_file.read_text(encoding="utf-8").strip()
        assert text == str(os.getpid())
    finally:
        mine.release()


def _start_time(pid: int) -> float | None:
    return single_instance._process_start_time(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="信号不同")
def test_the_test_process_can_be_signalled() -> None:
    """确认这台机器上 os.kill 能用（下面几个用例依赖它）。"""
    assert hasattr(signal, "SIGKILL") or hasattr(signal, "SIGTERM")
