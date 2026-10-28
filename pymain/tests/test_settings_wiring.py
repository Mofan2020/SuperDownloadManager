"""设置页 → 引擎的接线测试。

**为什么单独开一个文件。** 设置页有好几个旋钮，界面上能改，引擎
压根不读——用户调完发现什么都没变，只能得出「这程序没有动态分段
能力」的结论。**「存着但不生效」是最坑的形态**：没有报错，界面上
还显示保存成功了。

以前「初始连接数」和「分片上限」就是这样的死设置。这里盯死它们
真的接到了引擎上。
"""

from __future__ import annotations

from sdm.engine.download_engine import DownloadEngine
from sdm.engine.range_planner import RangeStrategy


class TestChunkLimitsReachTheEngine:
    def test_set_chunk_limits_applies_to_the_strategy(self) -> None:
        engine = DownloadEngine()
        engine.set_chunk_limits(4, 64)
        assert engine.strategy.initial_chunks == 4
        assert engine._user_max_chunks == 64

    def test_chunk_cap_is_never_below_four_times_the_connections(self) -> None:
        """分片上限必须 ≥ 连接数 × 4。

        **不然长尾立刻回来。** 分片数被卡到跟连接数一样大的时候，
        空闲连接没活干，下载末尾就只剩一两条连接在爬。
        """
        engine = DownloadEngine()
        engine.set_max_threads(8)
        # 用户把上限设成比连接数还小——按 4 倍兜底。
        engine.set_chunk_limits(2, 4)
        assert engine.strategy.max_chunks >= engine.parallelism_cap * 4
        assert engine.strategy.max_chunks >= 32

    def test_user_limit_is_respected_when_it_is_large_enough(self) -> None:
        """用户设的大上限要真的生效。"""
        engine = DownloadEngine()
        engine.set_max_threads(4)
        engine.set_chunk_limits(2, 200)
        assert engine.strategy.max_chunks == 200

    def test_initial_connections_are_clamped_to_a_sane_range(self) -> None:
        engine = DownloadEngine()
        engine.set_chunk_limits(999, 32)
        assert engine.strategy.initial_chunks == 32
        engine.set_chunk_limits(0, 32)
        assert engine.strategy.initial_chunks == 1

    def test_max_chunks_are_clamped_to_a_sane_range(self) -> None:
        engine = DownloadEngine()
        engine.set_chunk_limits(2, 99999)
        assert engine._user_max_chunks == 512

    def test_setting_the_thread_count_keeps_the_chunk_cap_sane(self) -> None:
        """改连接数的时候分片上限要跟着重算。"""
        engine = DownloadEngine()
        engine.set_chunk_limits(2, 32)
        engine.set_max_threads(16)
        assert engine.strategy.max_chunks >= 16 * 4


class TestBridgePlumbing:
    def test_bridge_pushes_the_settings_to_the_engine(self) -> None:
        """设置页存的值要真的进到引擎里。"""
        from sdm.gui.bridge import GuiBridge

        bridge = GuiBridge.__new__(GuiBridge)
        bridge.engine = DownloadEngine()
        bridge._apply_engine_setting = GuiBridge._apply_engine_setting.__get__(bridge)

        bridge._apply_engine_setting("set_chunk_limits", 3, 96)
        assert bridge.engine.strategy.initial_chunks == 3
        assert bridge.engine._user_max_chunks == 96


class TestDefaultStrategy:
    def test_default_max_chunks_leaves_room_for_idle_connections(self) -> None:
        """默认策略的分片上限得给空闲连接留活干。"""
        strategy = RangeStrategy()
        assert strategy.max_chunks >= 32
