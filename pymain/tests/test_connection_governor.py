"""连接名额调度的**不变量**测试。

`ConnectionGovernor` 管着「同时最多几条连接在传输」。它的正确性靠
三条不变量撑着，任何一条破了，下载就会以「下载中」的状态卡住——
用户看到进度条不动，界面也没有任何提示。

1. **等待者之间不会互相取消。** 取消一个 worker 不该把另一个正
   在等名额的 worker 取消掉——那会被引擎当成「用户取消了下载」。
2. **名额放出来之后，所有等待者最终都能拿到。** 一个都不能饿死。
3. **没有等待者时的 `release()` 不能坑掉后来的人。** 「放了名额
   但没人等」是最常见的情况（连接数放开时）。

**实现换过一次写法，这里把背景说清楚，免得后人照着错误的结论改
回去。**

原来 `acquire()` 用 `asyncio.Event`（「检查条件 → `clear()` →
`await wait()`」）。现在自己管 future 列表：唤醒只是「可能有位置了」，
醒来之后**重新检查条件**，不够就再等。

换写法的理由是**让契约显式**，不是修 bug：

- `Event` 是个「粘性的标志位」——`set()` 之后标志一直是 True，
  除非有人 `clear()`。而这里真正需要的语义是「**通知一下，让
  大家重新检查条件**」，两者的语义本来就不一样，中间那层
  `clear()` 全靠调用方记得写——少写一次就是一个「睡死」。
- future 列表把「唤醒 = 可能有位置」这件事写进了代码结构里，
  醒来必须重新检查条件是循环条件的一部分，不用另外记住。
- 顺带不用再 `bind_loop`（`Event` 要绑事件循环，future 不用）。

**曾经怀疑过这里有死锁（Windows CI 挂死 18 分钟那次）。推演下来
不是**：单线程事件循环里，「检查条件」和 `clear()` 之间没有 await，
`release()` 插不进来；`Event.clear()` 也只重置标志，不会取消别的
等待者。所以这里没有丢唤醒窗口。真正的挂死原因还没定位到，见
`docs/notes.md`。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdm.engine.range_planner import ConnectionGovernor


def _run(coro_factory, timeout: float = 15.0):
    async def _wrapped():
        return await asyncio.wait_for(coro_factory(), timeout=timeout)

    return asyncio.run(_wrapped())


def test_two_waiters_do_not_cancel_each_other():
    """**两个等待者不能互相取消。**

    一个等待者被取消（下载被用户停掉）不该把另一个正排队的
    worker 取消掉——那会被引擎当成「用户取消了整个下载」。
    """

    async def _inner():
        governor = ConnectionGovernor(initial=1, maximum=1)
        await governor.acquire()  # 占住唯一的名额

        outcome: list[str] = []

        async def waiter() -> None:
            try:
                await governor.acquire()
                outcome.append("acquired")
                governor.release()
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise

        first = asyncio.create_task(waiter())
        second = asyncio.create_task(waiter())
        # 让两个都进等待状态。
        await asyncio.sleep(0.05)
        assert not first.done(), "第一个等待者不该已经结束了"
        assert not second.done(), "第二个等待者不该已经结束了"

        governor.release()  # 放出一个名额
        await asyncio.sleep(0.05)
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)

        assert "cancelled" not in outcome, (
            f"等待名额的协程互相取消了：{outcome}——"
            "多连接下载会被误判成「用户取消」"
        )

    _run(_inner)


def test_all_waiters_eventually_get_a_slot():
    """**名额放出来之后，所有等待者都要能拿到。**

    20 个 worker 抢 2 个名额，一个都不能饿死——不然下载会在
    「还有 worker 在等」的状态下永远结束不了。
    """

    async def _inner():
        governor = ConnectionGovernor(initial=2, maximum=4)
        acquired: list[int] = []

        async def worker(index: int) -> None:
            await governor.acquire()
            acquired.append(index)
            await asyncio.sleep(0.01)
            governor.release()

        tasks = [asyncio.create_task(worker(i)) for i in range(20)]
        await asyncio.gather(*tasks)

        assert len(acquired) == 20, f"只有 {len(acquired)}/20 个拿到了名额"

    _run(_inner)


def test_limit_can_grow_while_workers_wait():
    """**名额放开之后，排队的人要能醒过来。**

    连接数是动态放开的（`_ramp_connections`）。放开的那一刻，
    所有在等的 worker 都得被唤醒。
    """

    async def _inner():
        governor = ConnectionGovernor(initial=1, maximum=8)
        await governor.acquire()

        got: list[str] = []

        async def waiter(name: str) -> None:
            await governor.acquire()
            got.append(name)
            governor.release()

        tasks = [asyncio.create_task(waiter(f"w{i}")) for i in range(5)]
        await asyncio.sleep(0.05)
        assert not got, "名额还没放开，不该有人拿到"

        # 放开连接数（`_ramp_connections` 干的就是这件事）。
        governor.set_limit(4)
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)

        assert len(got) == 5, f"只有 {len(got)}/5 个被唤醒"

    _run(_inner)


def test_release_without_waiters_is_not_a_lost_wakeup():
    """**没有等待者时的 release 不能把后来的等待者坑掉。**

    「放了名额但没人等」是最常见的情况（连接数放开时）。后来的人
    必须能直接拿到名额，而不是睡死。
    """

    async def _inner():
        governor = ConnectionGovernor(initial=1, maximum=1)
        await governor.acquire()
        governor.release()  # 没人等

        # 后来的人应该立刻拿到，而不是等一个永远不来的信号。
        await asyncio.wait_for(governor.acquire(), timeout=5)
        governor.release()

    _run(_inner)


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
