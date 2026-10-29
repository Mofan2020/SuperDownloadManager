"""异步测试的看门狗：**挂死必须变成失败，而且要能看出卡在哪。**

没有看门狗的异步测试一旦挂住，整个 CI 作业会一直等到被作业超时
杀掉（20 分钟），日志里只有一行点号，根本看不出是哪个用例。

看门狗做两件事：

1. **超时 → 判失败。** 至少能报出是哪个用例。
2. **超时 → 把所有线程的栈转储到 stderr。** 光知道「这个用例超时
   了」不够，要知道它卡在哪一行。`faulthandler` 能做到。

**为什么在独立线程里跑协程。** 这不是洁癖，是被 Windows CI 的
挂死逼出来的：

`asyncio.run` 在退出时要等默认线程池里的线程结束
（`loop.shutdown_default_executor()`）。**Python 3.11 那个调用没有
超时**——只要有一个线程卡住（`asyncio.to_thread` 里的文件操作、
HTTP 收尾、数据库写盘……），`asyncio.run` 就永远不返回。

而这种情况下协程**早就结束了**——挂在协程之外。任何基于
`asyncio.wait_for` 的看门狗都抓不到它：外层的 `wait_for` 等的是
一个已经返回的协程，测试早就"通过"了，真正卡住的是
`asyncio.run` 的收尾。

放在独立线程里就绕开了这个坑：主线程 `join` 有上限，超时了就把
栈转储出来判失败，泄漏的那个线程是 daemon，不挡进程退出。

`coro_factory` 是个**工厂**（不是协程对象）——协程对象在创建时
就绑定了创建它的循环，跨 `asyncio.run` 用会出错。
"""

from __future__ import annotations

import asyncio
import faulthandler
import threading
from collections.abc import Callable
from typing import Any

#: 单个异步测试的默认上限（秒）。
#:
#: 本地跑完整套要 2 分半，慢一点的 CI 机器可能要翻倍。120 秒对
#: 单个用例来说非常宽松了——真的超过这么久，那就是卡住了。
DEFAULT_TIMEOUT = 120.0

#: 超时之后额外给协程收尾的时间（秒）。
#:
#: 超时不是立刻杀死协程——先给它一点时间把 `finally` 跑完（关句柄、
#: 删临时文件）。收完了就是干净的失败，收不完就带着栈判失败。
GRACE_SECONDS = 5.0


def run_with_timeout(
    coro_factory: Callable[[], Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    """跑一个协程，超时就转储栈并失败。

    超时抛 `TimeoutError`；协程自己抛的异常原样往外抛（traceback
    保留）。
    """

    box: dict[str, Any] = {}

    def _runner() -> None:
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as err:
            # **原样转交调用方**——协程自己抛的异常（连同 traceback）
            # 不能在这里被换成别的错误。
            box["error"] = err

    thread = threading.Thread(target=_runner, name="sdm-async-guard", daemon=True)
    thread.start()
    thread.join(timeout + GRACE_SECONDS)

    if thread.is_alive():
        # **先转储再判失败。** 不知道卡在哪一行的挂死没法查——
        # 转储里能看到所有线程的栈，包括卡住的那个。
        faulthandler.dump_traceback()
        raise TimeoutError(
            f"异步测试超过 {timeout:.0f} 秒还没结束（协程或收尾卡住了）"
        )

    error = box.get("error")
    if error is not None:
        raise error
    return box.get("value")
