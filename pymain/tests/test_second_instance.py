"""第二个进程碰到「已经有一个 GUI 在跑」的时候，该做什么。

**用户看到的现象是「菜单栏出现两个图标」或者「双击没反应」。**

前者是第二个进程自己又开了一个窗口，后者是第二个进程以为自己该退出，
结果谁也没开窗口。这两条都在 ``resolve_second_instance`` 里决定，所以
这里逐条钉住：

* 叫得醒 → 第二个进程退出，不开窗口（这是双击 exe 的本意）；
* 叫不醒、主人还在 → **强制接管**，自己开窗口；
* 叫不醒、主人已经不在 → 接管，自己开窗口；
* 有地址要交 → 地址先交出去，再叫窗口。

**「叫不醒的时候强制接管」这条最要紧。** 不接管的话，这个程序就彻底
打不开了：用户双击多少次都没用，只有结束任务管理器里的进程才救得回来。
"""

from __future__ import annotations

from sdm.gui.main import resolve_second_instance


class _FakeInstance:
    """记下被调了哪些方法。"""

    def __init__(
        self,
        *,
        shown: bool = True,
        alive: bool = True,
        handover_ok: bool = True,
    ) -> None:
        self._shown = shown
        self._alive = alive
        self._handover_ok = handover_ok
        self.calls: list[str] = []
        self.handover_urls: list[str] = []
        self.taken_over = False

    def hand_over_url(self, url: str) -> bool:
        self.calls.append("hand_over_url")
        self.handover_urls.append(url)
        return self._handover_ok

    def ask_running_to_show(self) -> bool:
        self.calls.append("ask_running_to_show")
        return self._shown

    def owner_alive(self) -> bool:
        self.calls.append("owner_alive")
        return self._alive

    def take_over(self) -> bool:
        self.calls.append("take_over")
        self.taken_over = True
        return True


def test_the_second_process_exits_when_the_window_came_forward() -> None:
    instance = _FakeInstance(shown=True)
    assert resolve_second_instance(instance, "") == "exit"
    # 叫醒了就不该再碰锁——两个窗口写同一份设置更糟。
    assert "take_over" not in instance.calls


def test_the_url_is_handed_over_before_the_window_is_called() -> None:
    """顺序不能反。

    先叫窗口再交地址的话，用户看到的窗口里没有那个任务——像是链接
    没点成功。
    """
    instance = _FakeInstance(shown=True)
    resolve_second_instance(instance, "https://example.com/a.zip")
    assert instance.calls.index("hand_over_url") < instance.calls.index(
        "ask_running_to_show"
    )
    assert instance.handover_urls == ["https://example.com/a.zip"]


def test_the_second_process_takes_over_when_the_owner_is_alive_but_silent() -> None:
    """叫不醒、主人还活着 → 强制接管。

    **这是「程序彻底打不开」那条路的唯一出口。** 不接管的话，用户只有
    去任务管理器里结束进程才能再打开。
    """
    instance = _FakeInstance(shown=False, alive=True)
    assert resolve_second_instance(instance, "") == "continue"
    assert instance.taken_over is True


def test_the_second_process_takes_over_when_the_owner_is_gone() -> None:
    instance = _FakeInstance(shown=False, alive=False)
    assert resolve_second_instance(instance, "") == "continue"
    assert instance.taken_over is True


def test_no_url_means_no_handover() -> None:
    instance = _FakeInstance(shown=True)
    resolve_second_instance(instance, "")
    assert "hand_over_url" not in instance.calls


def test_the_url_is_handed_over_even_when_the_window_cannot_be_called() -> None:
    """叫不醒的时候，地址照样要交。

    交出去了，已经在跑的那个窗口里会出现这个任务——哪怕第二个进程
    自己不开窗口。
    """
    instance = _FakeInstance(shown=False, alive=True)
    resolve_second_instance(instance, "https://example.com/a.zip")
    assert instance.handover_urls == ["https://example.com/a.zip"]
