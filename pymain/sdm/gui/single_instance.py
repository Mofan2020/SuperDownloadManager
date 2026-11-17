"""只让一个 GUI 进程在跑。

**为什么需要。** 用户双击 exe 的时候，期望是「把已经开着的那一个叫到
前面来」，不是「再开一个」。开了第二个会怎样：

* 托盘里出现两个图标；
* 两个进程各有一份引擎和一份任务表，加的任务在哪个窗口里看得到，
  下载也可能跑两遍；
* 用户想关掉一个，发现另一个还在，关不掉。

**锁里记两个东西。** 进程号 + 进程启动时间。**只看进程号不够**：Windows
上进程号回收得很快，一个被杀掉的进程留下的锁，过一会儿就指向一个
碰巧活着的无关进程。那时候第二个 GUI 会以为「已经有一个了」，自己
不开窗口——用户看到的现象是「双击没反应」。

**死掉的锁立刻作废。** 锁的主人不在了就直接接管，不等。等待只用在
「锁里的内容根本读不出来」的时候。

**叫不醒就自己开。** 有一种情况是锁的主人还活着，但它的窗口已经
没法显示了（webview 的线程死了、界面卡死）。这时候第二个进程既叫
不醒它、又不能接管锁，用户就彻底打不开这个程序了。所以：叫了没人
应答，而锁的主人也确实还活着，就强制接管并把旧的那个记下来。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

__all__ = ["SingleInstance", "lock_path"]

log = logging.getLogger("sdm.single")

#: 锁的主人不在了的时候，多久之内可以接管。
#:
#: 只在「锁的内容读不出来」时用得上。锁里写着主人的话是立刻判的。
STALE_SECONDS = 60.0

#: 两个进程启动时间差多少就算同一个进程。Windows 上这个值取的是
#: 创建时间，精度够用，差两秒以内当同一个。
_START_TOLERANCE = 2.0

#: 请已经在跑的那个把窗口叫到前面来，最多等多久。
_SHOW_TIMEOUT = 5.0


def lock_path() -> Path:
    """锁文件的位置。"""
    from .. import paths

    return paths.config_dir() / "gui.lock"


def _process_start_time(pid: int) -> float | None:
    """进程什么时候启动的。读不出来就返回 None。

    psutil 是唯一跨平台的办法。没有它就只剩进程号，那个不够用
    （见模块说明）。
    """
    if pid <= 0:
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _process_alive(pid: int) -> bool:
    """这个进程号上还有进程吗。"""
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SingleInstance:
    """「已经有一个 GUI 在跑」的锁。

    当上下文管理器用：

        with SingleInstance() as mine:
            if not mine.is_first:
                # 已经有一个了。
                ...
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or lock_path()
        self._fd: int | None = None
        self._started = _process_start_time(os.getpid())

    @property
    def is_first(self) -> bool:
        """True 表示这个进程是唯一的那个。"""
        return self._fd is not None

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    # -- 锁 -----------------------------------------------------------

    def acquire(self) -> None:
        """抢锁。抢不到就什么都不做（`is_first` 为 False）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not self._stale():
                return
            # 锁的主人没了，接过来。
            try:
                self._path.unlink()
            except OSError:
                return
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return
        except OSError:
            return

        self._fd = fd
        self._write_owner(fd)

    def _write_owner(self, fd: int) -> None:
        """把自己写进锁文件。

        两行：进程号，和这个进程的启动时间。旧版本写的锁只有进程号，
        也认——只是分不出被复用的进程号。
        """
        text = str(os.getpid())
        if self._started is not None:
            text += f"\n{self._started:.3f}"
        try:
            os.write(fd, text.encode("ascii"))
            os.fsync(fd)
        except OSError:
            pass

    def owner(self) -> dict[str, float | int | None]:
        """锁文件里写的：``{"pid": ..., "started": ...}``。

        读不出来就是空字典，意思是「不知道锁是谁的」。
        """
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not text:
            return {}
        parts = text.splitlines()
        try:
            pid = int(parts[0].strip())
        except ValueError:
            return {}
        started: float | None = None
        if len(parts) > 1:
            try:
                started = float(parts[1].strip())
            except ValueError:
                started = None
        return {"pid": pid, "started": started}

    def owner_alive(self) -> bool:
        """锁里写的那个进程还活着吗。

        **进程号被复用不算活着。** 启动时间对不上就是另一个进程了。
        旧版本的锁没有启动时间，那只能信进程号。
        """
        owner = self.owner()
        pid = int(owner.get("pid", 0) or 0)
        if pid <= 0:
            return False
        if not _process_alive(pid):
            return False

        recorded = owner.get("started")
        if not isinstance(recorded, float):
            return True
        actual = _process_start_time(pid)
        if actual is None:
            # 读不到启动时间（没装 psutil，或者没权限）。进程还活着，
            # 这就是能拿到的最好答案。
            return True
        return abs(actual - recorded) <= _START_TOLERANCE

    def _stale(self) -> bool:
        """锁是不是没用了。"""
        if not self._path.exists():
            return True
        if self.owner():
            return not self.owner_alive()
        try:
            age = time.time() - self._path.stat().st_mtime
        except OSError:
            return True
        return age > STALE_SECONDS

    def take_over(self) -> bool:
        """把锁从已经不在的主人手里抢过来。

        **活着的主人不会被抢。** 两个窗口写同一份设置，比一个窗口
        叫不回来更糟。
        """
        if self._fd is not None:
            return True
        if self._path.exists() and self.owner_alive():
            return False
        try:
            self._path.unlink()
        except OSError:
            pass
        self.acquire()
        return self.is_first

    def release(self) -> None:
        """把锁还回去。

        **只有自己还持有的锁才删。** 期间被别的进程接管了的话，删掉
        就等于把第二个窗口送给了下一次双击。
        """
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

        owner_pid = int(self.owner().get("pid", 0) or 0)
        if owner_pid and owner_pid != os.getpid():
            return
        try:
            self._path.unlink()
        except OSError:
            pass

    # -- 叫已经在跑的那个 ---------------------------------------------

    def ask_running_to_show(self) -> bool:
        """请已经在跑的那个 GUI 把窗口叫到前面来。

        走 GUI 自己的 IPC 端点（Windows 上是 127.0.0.1:7957）。
        叫醒了就返回 True，这个进程该退出。

        **端点连不上不等于主人死了**——端点起失败也是连不上。所以
        叫完之后还要看锁：主人确实不在了才接管，主人还在（但叫不醒）
        就强制接管，否则程序会彻底打不开。
        """
        try:
            from ..ipc.client import ClientError, IPCClient
            from ..ipc.protocol import METHOD_SHOW_WINDOW
            from ..version import GUI_CLI_PORT_WINDOWS
        except Exception as exc:
            log.debug("叫不到已经运行的实例: %s", exc)
            return False

        try:
            client = IPCClient(port=GUI_CLI_PORT_WINDOWS, timeout=_SHOW_TIMEOUT)
            reply = client.send({"id": "show", "method": METHOD_SHOW_WINDOW})
        except (ClientError, OSError, ValueError) as exc:
            log.info("已经运行的实例没有应答: %s", exc)
            return False
        except Exception as exc:
            log.info("叫已经运行的实例时出错: %s", exc)
            return False

        return bool(reply.get("ok"))

    def hand_over_url(self, url: str) -> bool:
        """把一个下载地址交给已经在跑的那个 GUI。

        **URL scheme 唤起时会用到。** 用户在浏览器里点一个下载链接，
        系统把 ``superdownloadmanager://add?url=...`` 交给应用——如果
        应用已经开着，来的是第二个进程。这个进程不能自己开窗口（那就
        是两个实例了），也不能把地址丢掉（用户点了链接却什么都没下）。

        所以先把地址交给已有的那个（走 IPC 的 add，跟命令行加任务是
        同一条路，任务立刻出现在那个窗口里），再把窗口叫出来。

        返回 True 表示交出去了。
        """
        if not url:
            return True
        try:
            from ..ipc.client import ClientError, IPCClient
            from ..ipc.protocol import METHOD_ADD
            from ..version import GUI_CLI_PORT_WINDOWS
        except Exception as exc:
            log.warning("把下载地址交给已运行的实例失败: %s", exc)
            return False

        try:
            client = IPCClient(port=GUI_CLI_PORT_WINDOWS, timeout=_SHOW_TIMEOUT)
            reply = client.send(
                {"id": "handover", "method": METHOD_ADD, "params": {"url": url}}
            )
        except (ClientError, OSError, ValueError) as exc:
            log.warning("把下载地址交给已运行的实例失败: %s", exc)
            return False
        except Exception as exc:
            log.warning("把下载地址交给已运行的实例时出错: %s", exc)
            return False

        return bool(reply.get("ok"))
