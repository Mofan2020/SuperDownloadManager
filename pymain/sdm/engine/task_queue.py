"""任务队列：决定并发槽位怎么分，以及全局连接预算。

对应 Swift 版 Engine/TaskQueue.swift。

策略是「专用带宽」：用户通常只想把一个文件快点下完，所以
单个活动任务独占并发预算；第二个任务只有在第一个跑满带宽
或被暂停时才分到槽位。

**这个类以前是死代码**——引擎压根不读它，每个任务自己拿满
`parallelism_cap` 条连接。三个任务就是 96 条连接，五个任务 160 条。
服务器看到这种连接风暴会开始限流（429），然后每个任务都因为
限流而失败。

现在它有两个角色：

1. **槽位分配器**：`available_slots(task_id)` 告诉引擎「这个任务
   现在能分到几个连接」。
2. **共享预算的持有者**：`ConnectionBudget` 让所有任务从**同一个**
   连接池里拿名额。全局上限就是 `parallelism_cap`，不会再因为
   任务变多而线性膨胀。

**为什么需要两个东西（份额 + 硬池）。**

份额解决的是「分得公平」：n 个任务一起跑，每个分到 `cap // n`，
一个任务独占时还是拿满 `cap`（速度不打折）。份额是**软上限**。

硬池解决的是「份额切换的那一瞬间」。第一个任务独占时已经开了
32 条连接，第二个任务进来后它的份额降到 16——但**已经在传的连接
不能砍断**（砍了就白下了）。没有硬池的话，这一瞬间总连接数会
冲到 32 + 16 = 48。硬池保证**同时在传的连接数**永远不超过 `cap`。

两个是协作的：份额定软上限，硬池兜住切换瞬间的溢出。

**份额下限 4 条。** 9 个任务抢 32 条时按比例分只有 3 条多——
再挤一点某个任务就分到 0 条，它会完全卡死。所以每个任务**至少
4 条**（上限仍然是 `cap`）。抢过头的那部分由硬池裁掉。
"""

from __future__ import annotations

import asyncio
import threading

#: 每个任务最少分到几条连接。
#:
#: 低于这个数那个任务基本等于卡死——分到 0 条更是完全不动。
#: 「至少留一点」比「严格按比例」重要。
MIN_SLOTS_PER_TASK = 4


class ConnectionBudget:
    """全局连接池。所有任务共用。

    **跟 `TaskQueue` 的份额不是一回事。** 份额是「这个任务最多
    能开几条」（软上限，任务之间分），硬池是「所有任务加起来
    同时在传的不超过几条」（硬上限）。两者协作才守得住硬上限——
    份额切换的瞬间，已经在传的连接砍不断，只有硬池能兜住那一下
    溢出。

    实现跟 `ConnectionGovernor` 一样是「计数器 + 唤醒 future」，
    不是信号量——信号量不能缩容，而这里 `set_cap` 要能改上限。
    """

    def __init__(self, cap: int) -> None:
        self.cap = max(1, cap)
        self._used = 0
        #: 正在等名额的协程。每个一个 future。
        self._waiters: list[asyncio.Future] = []

    @property
    def used(self) -> int:
        """当前占用的连接数。"""
        return self._used

    @property
    def available(self) -> int:
        """还能再拿几条。"""
        return max(0, self.cap - self._used)

    async def acquire(self) -> None:
        """等一个连接名额。

        **醒来之后一定要重新检查条件。** 唤醒只是「可能有位置了」
        的信号，不是「位置归你了」——几个等待者会同时醒来抢。
        """
        while self._used >= self.cap:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self._waiters.append(fut)
            try:
                await fut
            finally:
                if fut in self._waiters:
                    self._waiters.remove(fut)
        self._used += 1

    def release(self) -> None:
        """归还一个名额。"""
        self._used = max(0, self._used - 1)
        self._wake()

    def set_cap(self, value: int) -> None:
        """改上限。已经占着的连接不受影响（下次申请时按新上限算）。"""
        new = max(1, value)
        if new == self.cap:
            return
        self.cap = new
        self._wake()

    def _wake(self) -> None:
        """唤醒所有等名额的协程。它们醒来后会重新检查条件。"""
        for fut in list(self._waiters):
            if not fut.done():
                try:
                    fut.set_result(None)
                except Exception:
                    pass


class TaskQueue:
    """并发槽位分配 + 共享预算的份额账。"""

    def __init__(self, parallelism_cap: int = 32) -> None:
        self.parallelism_cap = max(1, parallelism_cap)
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}  # task_id -> state
        #: 每个任务当前分到的份额。
        self._per_task: dict[str, int] = {}

    # -- 任务登记 ----------------------------------------------------

    def register(self, task_id: str, state: str) -> None:
        with self._lock:
            self._active[task_id] = state

    def update_state(self, task_id: str, state: str) -> None:
        with self._lock:
            self._active[task_id] = state

    def remove(self, task_id: str) -> None:
        """任务结束。**连带把它占的份额还回池子。**

        不还的话，池子会被死任务占满——任务被取消或失败之后，
        它的份额永远留着，后面的任务再也拿不到连接。
        """
        with self._lock:
            self._active.pop(task_id, None)
            self._per_task.pop(task_id, None)

    # -- 槽位分配 ----------------------------------------------------

    def active_count(self) -> int:
        """正在下载的任务数。"""
        with self._lock:
            return sum(1 for state in self._active.values() if state == "downloading")

    def available_slots(self, task_id: str) -> int:
        """这个任务现在能分到几个并发槽。

        **n 个任务分 `cap // n` 条。** 份额是「上限」，超发就是
        超卖。

        **但每个任务至少 4 条。** 按比例分到底会让某个任务分到
        0 条，它就完全卡死了（「下载中」但一个字节都不动）。
        宁可多分几条、让硬池去裁，也不要有任务完全饿死。

        单独一个任务时拿满整个预算（跟以前一样快）。
        """
        with self._lock:
            downloading = [
                tid for tid, state in self._active.items() if state == "downloading"
            ]
            count = len(downloading)
            if task_id not in downloading:
                # 这个任务马上要登记——算份额时把它算进去。
                count += 1
        if count <= 1:
            return self.parallelism_cap
        share = self.parallelism_cap // count
        return max(MIN_SLOTS_PER_TASK, share)

    def set_parallelism_cap(self, value: int) -> None:
        """改全局上限。已在跑的连接不受影响（下次申请时按新上限算）。"""
        with self._lock:
            self.parallelism_cap = max(1, value)

    # -- 份额账 ------------------------------------------------------

    def reserve(self, task_id: str, slots: int) -> None:
        """记下这个任务分到的份额。

        **这只是账，不是闸门。** 真正限制同时在传的连接数的是
        `ConnectionBudget`——份额加起来可能超过上限（每个任务
        至少 4 条），那部分由硬池裁掉。
        """
        with self._lock:
            self._per_task[task_id] = max(0, int(slots))

    def release(self, task_id: str, slots: int | None = None) -> None:
        """归还份额。

        `slots` 省略 = 全额归还这个任务。给了具体值 = 归还到
        那个数（缩容时用）。
        """
        with self._lock:
            current = self._per_task.get(task_id, 0)
            if slots is None:
                target = 0
            else:
                target = max(0, min(current, slots))
            if current != target:
                self._per_task[task_id] = target
            if target == 0:
                self._per_task.pop(task_id, None)

    def reserved_for(self, task_id: str) -> int:
        """这个任务当前占了多少份额。"""
        with self._lock:
            return self._per_task.get(task_id, 0)

    def used(self) -> int:
        """所有任务分到的份额之和。"""
        with self._lock:
            return sum(self._per_task.values())

    def snapshot(self) -> dict[str, int]:
        """每个任务占的份额。给测试和调试用。"""
        with self._lock:
            return dict(self._per_task)
