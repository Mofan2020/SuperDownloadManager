"""分片「耗时」列的测试。

用户报的第二个问题：**任务详情页每个分片的耗时显示不对**。

根因：那一列标题写「耗时」，显示的却是 `idle_seconds`——「距上次
写入多少秒」。两个完全不同的意思：

- 已完成的分片，`idle_seconds` 会**随时间一直涨**。越早完成的
  分片数字越大，看着像卡住了。
- 排队中的分片，`idle_seconds` 是 null（显示「—」），但它其实
  「已经等了很久」。

真正的「耗时」是**这个分片从开始下载到下完花了多久**。

这里守三件事：
1. 已完成的分片上报的是**固定的**真实耗时，不随时间变。
2. 进行中的分片上报的是「已经花了多久」。
3. 没开始的分片是 None（不是 0——0 会被读成「瞬间下完」）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from sdm.engine.download_flow import DownloadFlow
from sdm.engine.file_assembler import FileAssembler
from sdm.engine.range_planner import RangeStrategy

from .range_server import RangeServer


def _assembler(tmp_path: Path) -> FileAssembler:
    return FileAssembler(destination=tmp_path / "out.bin")


def test_finished_chunk_reports_a_fixed_elapsed(tmp_path: Path) -> None:
    """分片下完之后，它上报的耗时**不再变**。

    这是跟旧行为（idle_seconds）的关键区别：旧的那个会一直涨，
    越老的分片数字越大——用户看到的就是「已完成的分片耗时最大」。
    """
    asm = _assembler(tmp_path)
    asm.open_chunk(0, 0, resume=False, end_offset=1024)
    asm.append_limited(b"x" * 1024, 0, limit=1024)

    # 记下这一刻的耗时
    first = asm.chunk_started_at(0)
    assert first is not None, "写过字节的分片必须有开始时刻"

    # 等一会儿——如果耗时是「距上次写入」，这个数字会变
    time.sleep(0.05)
    elapsed_at_10ms = _elapsed_of(asm, 0)

    time.sleep(0.15)
    elapsed_at_160ms = _elapsed_of(asm, 0)

    assert elapsed_at_10ms == elapsed_at_160ms, (
        "分片完成后耗时还在变——这是「距上次写入」不是「耗时」。"
        f"10ms 时是 {elapsed_at_10ms}，160ms 时是 {elapsed_at_160ms}"
    )


def _elapsed_of(asm: FileAssembler, chunk_id: int) -> float:
    """分片的耗时（已完成 = 固定值；在途 = 已经花的时间）。"""
    finished = asm.chunk_finished_at(chunk_id)
    started = asm.chunk_started_at(chunk_id)
    assert started is not None
    if finished is not None:
        return finished - started
    return time.monotonic() - started


def test_unfinished_chunk_elapsed_grows(tmp_path: Path) -> None:
    """还没下完的分片，耗时是「已经花了多久」——会继续涨。"""
    asm = _assembler(tmp_path)
    asm.open_chunk(0, 0, resume=False, end_offset=1024)
    asm.append_limited(b"x" * 100, 0, limit=1024)

    assert asm.chunk_started_at(0) is not None
    assert asm.chunk_finished_at(0) is None, "没写满就不算下完"

    time.sleep(0.1)
    elapsed = _elapsed_of(asm, 0)
    assert elapsed >= 0.05, f"在途分片的耗时应该反映已经花的时间，实际 {elapsed}"


def test_untouched_chunk_has_no_elapsed(tmp_path: Path) -> None:
    """还没开始的分片：没有开始时刻。

    报 0 会被读成「瞬间下完」，必须报 None。
    """
    asm = _assembler(tmp_path)
    asm.open_chunk(1, 1024, resume=False, end_offset=2048)

    assert asm.chunk_started_at(1) is None
    assert asm.chunk_finished_at(1) is None


def test_flow_reports_elapsed_seconds_for_each_chunk(tmp_path: Path) -> None:
    """整条链路：DownloadFlow 上报的每片都带 `elapsed_seconds`。

    回归防护——GUI 的「耗时」列读的就是这个字段。
    """
    seen: list[dict] = []

    async def _inner() -> None:
        with RangeServer(total_bytes=256 * 1024) as srv:
            flow = DownloadFlow(
                url=srv.url,
                destination=tmp_path / "out.bin",
                strategy=RangeStrategy(),
                chunk_count=2,
            )
            flow.on_chunk_progress = lambda states: seen.extend(states)
            await flow.run()

    asyncio.run(_inner())

    assert seen, "一次都没上报过分片状态"
    for state in seen:
        assert "elapsed_seconds" in state, (
            f"分片状态里没有 elapsed_seconds：{state}"
        )
        # 已完成的分片必须有真实耗时
        if state["bytes_done"] >= state["size"]:
            assert state["elapsed_seconds"] is not None, (
                f"已完成的分片必须有耗时：{state}"
            )
            assert state["elapsed_seconds"] >= 0


def test_gui_uses_elapsed_seconds_for_the_elapsed_column() -> None:
    """GUI 的「耗时」列读 `elapsed_seconds`，不是 `idle_seconds`。

    前端契约测试——读源码里的列渲染代码。

    `idle_seconds` 的语义是「距上次写入多少秒」，用它填「耗时」
    列会让已完成的分片显示一个一直涨的数字。
    """
    app_js = (
        Path(__file__).resolve().parents[1] / "sdm" / "gui" / "assets" / "app.js"
    ).read_text(encoding="utf-8")

    # 「耗时」列那一行必须用 elapsed_seconds
    assert "c.elapsed_seconds" in app_js, "GUI 的「耗时」列还在用别的字段"
    # 不能用 idle_seconds 填耗时列
    assert "fmtChunkElapsed(c.idle_seconds)" not in app_js, (
        "「耗时」列还在用 idle_seconds——那是「距上次写入多少秒」，"
        "已完成的分片会一直涨"
    )
    # 但 idle_seconds 仍然要留着——用它发现卡住的分片
    assert "c.idle_seconds" in app_js, "idle_seconds 还得留着判断卡住"


def test_i18n_has_chunk_rate_label() -> None:
    """新增的「速率」列要有中英文标签。"""
    import json

    root = Path(__file__).resolve().parents[1] / "sdm" / "i18n"
    for name in ("zh-Hans.json", "en.json"):
        data = json.loads((root / name).read_text(encoding="utf-8"))
        assert "taskDetailChunkRate" in data, f"{name} 缺 taskDetailChunkRate"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
