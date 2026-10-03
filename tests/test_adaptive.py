"""
动态参数模块的单元测试（基于 2 小时快照）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from polymaker.catalog.adaptive import (
    AdaptiveCache,
    AdaptiveEngine,
    MarketFactors,
    adapt_profile,
    calc_vol_factor,
    calc_vol_regime_factor,
)
from polymaker.catalog.snapshots import Snapshot, SnapshotStore
from polymaker.config import OneWayProfile


# ── fixture ────────────────────────────────────────────────────────────────


@pytest.fixture
def base_profile() -> OneWayProfile:
    """基础 profile，测试用。"""
    return OneWayProfile(
        ow_side="yes",
        base_size_usdc=30.0,
        q_max_usdc=60.0,
        ow_pyramid_shares=[0.1, 0.2, 0.3, 0.4],
        ow_wall_pct_of_24h=0.005,
        ow_wall_skip_ticks=1,
        ow_wall_min_gap_ticks=5,
        ow_inv_low=0.33,
        ow_inv_mid=0.66,
        ow_panic_toxicity=0.6,
        ow_exit_days_before=30.0,
    )


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    db = tmp_path / "test.db"
    s = SnapshotStore(db)
    yield s
    s.close()


def _make_snapshot(
    condition_id: str,
    bucket_ts: int,
    *,
    volume_delta: float = 10.0,
    vol_regime: str = "normal",
) -> Snapshot:
    """造一条测试快照。"""
    return Snapshot(
        condition_id=condition_id,
        slug="test-slug",
        bucket_ts=bucket_ts,
        price_open=0.1,
        price_close=0.11,
        price_high=0.12,
        price_low=0.09,
        volume_delta=volume_delta,
        trend="up",
        trend_pct=0.05,
        trend_streak=3,
        trend_direction="up",
        drawdown_from_high=0.02,
        price_percentile=0.6,
        bid_depth_top5=100.0,
        ask_depth_top5=80.0,
        bid_wall_count=2,
        bid_depth_change=0.1,
        vol_short=0.02,
        vol_long=0.015,
        vol_regime=vol_regime,
    )


def _insert_snapshot(store: SnapshotStore, snap: Snapshot) -> None:
    """直接往数据库插一条快照。"""
    conn = store._conn
    conn.execute(
        """INSERT INTO market_snapshots (
            condition_id, slug, bucket_ts,
            price_open, price_close, price_high, price_low,
            volume_delta,
            trend, trend_pct, trend_streak,
            trend_direction, drawdown_from_high, price_percentile,
            bid_depth_top5, ask_depth_top5, bid_wall_count, bid_depth_change,
            vol_short, vol_long, vol_regime,
            collected_ts
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            snap.condition_id, snap.slug, snap.bucket_ts,
            snap.price_open, snap.price_close, snap.price_high, snap.price_low,
            snap.volume_delta,
            snap.trend, snap.trend_pct, snap.trend_streak,
            snap.trend_direction, snap.drawdown_from_high, snap.price_percentile,
            snap.bid_depth_top5, snap.ask_depth_top5, snap.bid_wall_count, snap.bid_depth_change,
            snap.vol_short, snap.vol_long, snap.vol_regime,
            1234567890.0,
        ),
    )
    conn.commit()


# ── 系数计算函数测试 ──────────────────────────────────────────────────────


class TestCalcVolFactor:
    """成交量系数计算。"""

    def test_below_low(self) -> None:
        assert calc_vol_factor(2.0) == 0.3  # < $5

    def test_low_to_mid(self) -> None:
        assert calc_vol_factor(10.0) == 0.7  # $5 ~ $20

    def test_mid_to_high(self) -> None:
        assert calc_vol_factor(50.0) == 1.0  # $20 ~ $100

    def test_above_high(self) -> None:
        assert calc_vol_factor(200.0) == 1.5  # > $100

    def test_zero_volume(self) -> None:
        assert calc_vol_factor(0.0) == 0.3


class TestCalcVolRegimeFactor:
    """波动率系数计算。"""

    def test_all_low(self) -> None:
        snaps = [_make_snapshot("0xabc", i, vol_regime="low") for i in range(12)]
        assert calc_vol_regime_factor(snaps) == 0.8

    def test_all_high(self) -> None:
        snaps = [_make_snapshot("0xabc", i, vol_regime="high") for i in range(12)]
        assert calc_vol_regime_factor(snaps) == 1.5

    def test_normal_distribution(self) -> None:
        snaps = [_make_snapshot("0xabc", i, vol_regime="normal") for i in range(12)]
        assert calc_vol_regime_factor(snaps) == 1.0

    def test_empty_list(self) -> None:
        assert calc_vol_regime_factor([]) == 1.0


# ── adapt_profile 测试 ──────────────────────────────────────────────────────


class TestAdaptProfile:
    """参数调整函数。"""

    def test_vol_factor_scales_position(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=2.0, vol_regime_factor=1.0)
        assert adjusted.base_size_usdc == 60.0  # 30 × 2
        assert adjusted.q_max_usdc == 120.0      # 60 × 2

    def test_regime_factor_scales_gap(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=1.0, vol_regime_factor=1.5)
        assert adjusted.ow_wall_min_gap_ticks == 8  # 5 × 1.5 = 7.5 → 四舍五入 8

    def test_regime_factor_divides_panic(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=1.0, vol_regime_factor=1.5)
        assert adjusted.ow_panic_toxicity == pytest.approx(0.6 / 1.5)

    def test_panic_toxicity_has_floor(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=1.0, vol_regime_factor=3.0)
        # 0.6 / 3.0 = 0.2，低于下限 0.3，应该取 0.3
        assert adjusted.ow_panic_toxicity == 0.3

    def test_side_not_affected(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=2.0, vol_regime_factor=2.0)
        assert adjusted.ow_side == "yes"

    def test_exit_days_not_affected(self, base_profile: OneWayProfile) -> None:
        adjusted = adapt_profile(base_profile, vol_factor=2.0, vol_regime_factor=2.0)
        assert adjusted.ow_exit_days_before == 30.0


# ── AdaptiveCache 测试 ──────────────────────────────────────────────────────


class TestAdaptiveCache:
    """带滞回的缓存。"""

    def test_first_write(self) -> None:
        cache = AdaptiveCache(hysteresis_pct=0.2)
        f = MarketFactors(vol_factor=1.0, vol_regime_factor=1.0, bucket_count=5, is_conservative=False)
        factors, switched = cache.maybe_update("0xabc", f)
        assert factors == f
        assert switched is True

    def test_small_change_ignored(self) -> None:
        cache = AdaptiveCache(hysteresis_pct=0.2)
        f1 = MarketFactors(vol_factor=1.0, vol_regime_factor=1.0, bucket_count=5, is_conservative=False)
        cache.maybe_update("0xabc", f1)

        f2 = MarketFactors(vol_factor=1.1, vol_regime_factor=1.0, bucket_count=5, is_conservative=False)
        factors, switched = cache.maybe_update("0xabc", f2)
        assert factors.vol_factor == 1.0  # 没变
        assert switched is False

    def test_big_change_accepted(self) -> None:
        cache = AdaptiveCache(hysteresis_pct=0.2)
        f1 = MarketFactors(vol_factor=1.0, vol_regime_factor=1.0, bucket_count=5, is_conservative=False)
        cache.maybe_update("0xabc", f1)

        f2 = MarketFactors(vol_factor=1.5, vol_regime_factor=1.0, bucket_count=5, is_conservative=False)
        factors, switched = cache.maybe_update("0xabc", f2)
        assert factors.vol_factor == 1.5
        assert switched is True

    def test_conservative_mode_switch_immediate(self) -> None:
        cache = AdaptiveCache(hysteresis_pct=0.2)
        f1 = MarketFactors(vol_factor=1.0, vol_regime_factor=1.0, bucket_count=10, is_conservative=False)
        cache.maybe_update("0xabc", f1)

        f2 = MarketFactors(vol_factor=0.3, vol_regime_factor=1.5, bucket_count=0, is_conservative=True)
        factors, switched = cache.maybe_update("0xabc", f2)
        assert factors.is_conservative is True
        assert switched is True


# ── AdaptiveEngine 集成测试 ───────────────────────────────────────────────


class TestAdaptiveEngine:
    """自适应引擎：从数据库读数据 → 算系数。"""

    def test_no_data_returns_conservative(self, store: SnapshotStore, base_profile: OneWayProfile) -> None:
        """没有数据应该返回保守模式。"""
        engine = AdaptiveEngine(store)
        result = engine.get_profile("0xempty", base_profile)
        # 保守模式下 base_size 应该是 30 × 0.3 = 9
        assert result.profile.base_size_usdc == pytest.approx(9.0)
        assert result.just_switched is True

    def test_no_data_no_order(self, store: SnapshotStore, base_profile: OneWayProfile) -> None:
        """new_market_no_order=True 时，新市场完全不挂买单。"""
        engine = AdaptiveEngine(store, new_market_no_order=True)
        result = engine.get_profile("0xempty", base_profile)
        assert result.profile.base_size_usdc == 0.0

    def test_few_buckets_transition_mode(self, store: SnapshotStore, base_profile: OneWayProfile) -> None:
        """3 个桶（不足 6 个）应该走过渡模式。"""
        # 插 3 个桶
        for i in range(3):
            snap = _make_snapshot("0xabc", bucket_ts=1000 + i * 7200)
            _insert_snapshot(store, snap)

        engine = AdaptiveEngine(store)
        result = engine.get_profile("0xabc", base_profile)
        # 过渡模式 vol_factor = 0.5
        assert result.profile.base_size_usdc == pytest.approx(15.0)  # 30 × 0.5

    def test_enough_buckets_normal_mode(self, store: SnapshotStore, base_profile: OneWayProfile) -> None:
        """10 个桶（超过 6 个）应该正常计算。"""
        # 插 10 个桶，每个桶成交量 $50
        for i in range(10):
            snap = _make_snapshot("0xabc", bucket_ts=1000 + i * 7200, volume_delta=50.0)
            _insert_snapshot(store, snap)

        engine = AdaptiveEngine(store)
        result = engine.get_profile("0xabc", base_profile)
        # avg_volume = $50，落在 mid~high 区间 → vol_factor = 1.0
        assert result.profile.base_size_usdc == pytest.approx(30.0)  # 30 × 1.0

    def test_recalc_interval(self, store: SnapshotStore, base_profile: OneWayProfile) -> None:
        """重算间隔内应该用缓存。"""
        engine = AdaptiveEngine(store, recalc_interval_s=7200)

        # 第一次调用
        result1 = engine.get_profile("0xabc", base_profile)
        vol1 = result1.profile.base_size_usdc

        # 立刻再调用，应该用缓存，结果一样，just_switched=False
        result2 = engine.get_profile("0xabc", base_profile)
        vol2 = result2.profile.base_size_usdc

        assert vol1 == vol2
        assert result2.just_switched is False
