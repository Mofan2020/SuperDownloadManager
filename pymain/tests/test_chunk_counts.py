"""分片计数的回归测试。

详情面板那行「已完成分片/总片数」读的是 TaskSummary 里的
chunk_count / completed_chunks，不是 _chunk_states。这两个字段
曾经从来没被赋值过，永远是 0 —— 面板上一直显示 (0/0)。

顺带盯住两个相关的行为：
- 任务结束后分片明细要还能查到（详情面板能查已完成任务）
- 分片明细的保留有上限，不会无限占内存
"""

from __future__ import annotations

import asyncio

import pytest

from sdm.engine.download_engine import DownloadEngine, TaskState


def make_states(n: int, done: int = 0) -> list[dict]:
    """造 n 个分片状态，前 done 个已完成。"""
    return [
        {
            "id": i,
            "start": i * 100,
            "end": (i + 1) * 100,
            "size": 100,
            "bytes_done": 100 if i < done else 0,
            "idle_seconds": 0.0,
        }
        for i in range(n)
    ]


@pytest.fixture
def engine() -> DownloadEngine:
    eng = DownloadEngine()
    return eng


def test_chunk_count_updates_from_chunk_states(engine):
    """分片状态更新后，摘要里的计数要跟着变。"""
    from sdm.engine.download_engine import TaskSummary

    task_id = "t1"
    summary = TaskSummary(id=task_id, url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks[task_id] = summary

    engine.update_chunk_states(task_id, make_states(32, done=10))

    assert summary.chunk_count == 32
    assert summary.completed_chunks == 10


def test_chunk_count_reaches_full_when_all_chunks_done(engine):
    """全部分片完成时，计数是 N/N。"""
    from sdm.engine.download_engine import TaskSummary

    task_id = "t2"
    summary = TaskSummary(id=task_id, url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks[task_id] = summary

    engine.update_chunk_states(task_id, make_states(16, done=16))

    assert summary.chunk_count == 16
    assert summary.completed_chunks == 16


def test_completed_chunks_counts_partial_progress(engine):
    """部分完成的分片不能算完成。"""
    from sdm.engine.download_engine import TaskSummary

    task_id = "t3"
    summary = TaskSummary(id=task_id, url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks[task_id] = summary

    states = make_states(4, done=2)
    states[2]["bytes_done"] = 50  # 半个，不算完成

    engine.update_chunk_states(task_id, states)

    assert summary.completed_chunks == 2


def test_chunk_states_kept_after_task_ends(engine):
    """任务结束后分片明细要留着，详情面板查已完成任务才有东西看。"""
    from sdm.engine.download_engine import TaskSummary

    task_id = "t4"
    summary = TaskSummary(id=task_id, url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks[task_id] = summary
    engine.update_chunk_states(task_id, make_states(8, done=8))

    engine._retire_chunk_states(task_id)

    assert engine.get_chunks(task_id), "任务结束后分片明细被清空了"
    assert len(engine.get_chunks(task_id)) == 8


def test_chunk_states_retention_is_bounded(engine):
    """保留上限是硬的，不能无限占内存。"""
    from sdm.engine.download_engine import TaskSummary

    for i in range(engine.CHUNK_STATE_RETENTION + 10):
        tid = f"task-{i}"
        summary = TaskSummary(id=tid, url="http://x/y", state=TaskState.DOWNLOADING.value)
        engine.tasks[tid] = summary
        engine.update_chunk_states(tid, make_states(4, done=4))
        engine._retire_chunk_states(tid)

    total = sum(len(v) for v in engine._chunk_states.values())
    limit = engine.CHUNK_STATE_RETENTION * 4
    assert total <= limit, f"分片明细攒了 {total} 条，超上限 {limit}"
    # 最老的那个应该已经被丢掉了
    assert "task-0" not in engine._chunk_states


def test_remove_task_drops_chunk_states(engine):
    """移除任务时分片明细也要清。"""
    from sdm.engine.download_engine import TaskSummary

    task_id = "t5"
    summary = TaskSummary(id=task_id, url="http://x/y", state=TaskState.DOWNLOADING.value)
    engine.tasks[task_id] = summary
    engine.update_chunk_states(task_id, make_states(4, done=4))

    asyncio.run(engine.remove(task_id))

    assert task_id not in engine._chunk_states
    assert task_id not in engine._chunk_state_order
