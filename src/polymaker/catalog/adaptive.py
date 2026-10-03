"""
One-Way 策略动态参数模块（基于 2 小时快照）。

根据最近 24 小时的快照数据，动态调整基础参数。
不维护多套离散 profile，只用一套基础参数 + 动态系数。

设计要点：
  - 两个核心信号：最近 12 桶平均成交量、最近 12 桶波动率状态分布
  - 数据不足 6 桶（12 小时）时走保守模式（新市场保护）
  - 带滞回缓存，避免系数在阈值附近抖动时频繁切换
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from polymaker.catalog.snapshots import Snapshot, SnapshotStore
from polymaker.config import OneWayProfile
from polymaker.logging import get_logger


log = get_logger("catalog.adaptive")


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MarketFactors:
    """一个市场当前的两个系数 + 状态。"""

    vol_factor: float          # 成交量系数（影响仓位大小）
    vol_regime_factor: float   # 波动率系数（影响层间距、恐慌阈值）
    bucket_count: int          # 有几个快照桶数据
    is_conservative: bool      # 是否在保守模式（数据不足）


@dataclass(frozen=True, slots=True)
class AdaptiveResult:
    """自适应计算结果。"""

    profile: OneWayProfile     # 调整后的 profile
    just_switched: bool        # 这次是不是刚切换了模式（保守↔正常）


class AdaptiveCache:
    """带滞回的系数缓存。

    避免系数在阈值附近抖动时频繁变化。
    只有当新系数和当前值差异超过 hysteresis_pct 时才更新。
    """

    def __init__(self, hysteresis_pct: float = 0.2) -> None:
        self._hysteresis = hysteresis_pct
        self._cache: dict[str, MarketFactors] = {}

    def get(self, condition_id: str) -> MarketFactors | None:
        return self._cache.get(condition_id)

    def maybe_update(
        self, condition_id: str, new_factors: MarketFactors
    ) -> tuple[MarketFactors, bool]:
        """判断是否需要更新。返回 (最终使用的 factors, 是否真的切换了)。

        特殊情况：
          - 第一次（缓存里没有）→ 直接写入，返回 switched=True
          - 从正常模式切到保守模式 → 立即切换（安全优先），switched=True
          - 从保守模式切回正常模式 → 也要立即切换，switched=True
          - 变化不大 → 保持旧值，switched=False
        """
        old = self._cache.get(condition_id)
        if old is None:
            self._cache[condition_id] = new_factors
            return new_factors, True

        # 保守模式切换是安全相关的，立即生效
        if old.is_conservative != new_factors.is_conservative:
            self._cache[condition_id] = new_factors
            return new_factors, True

        # 计算两个系数的变化比例
        vol_change = abs(new_factors.vol_factor - old.vol_factor) / old.vol_factor
        regime_change = abs(new_factors.vol_regime_factor - old.vol_regime_factor) / old.vol_regime_factor

        # 任何一个系数变化超过阈值，就整体更新
        if vol_change >= self._hysteresis or regime_change >= self._hysteresis:
            self._cache[condition_id] = new_factors
            return new_factors, True

        # 变化不大，保持旧值
        return old, False


# ── 系数计算函数 ────────────────────────────────────────────────────────────


def calc_vol_factor(
    avg_volume_12buckets: float,
    thresholds: tuple[float, float, float] = (5.0, 20.0, 100.0),
) -> float:
    """根据最近 12 个桶（24 小时）的平均桶成交量算成交量系数。

    thresholds = (低阈值, 中阈值, 高阈值)，单位 USDC/桶
    < 低   → 0.3  死盘，缩到 30%
    低~中  → 0.7  小盘，缩到 70%
    中~高  → 1.0  正常，基线
    > 高   → 1.5  活跃盘，放大 50%
    """
    low, mid, high = thresholds
    if avg_volume_12buckets < low:
        return 0.3
    elif avg_volume_12buckets < mid:
        return 0.7
    elif avg_volume_12buckets < high:
        return 1.0
    else:
        return 1.5


def calc_vol_regime_factor(
    snapshots: list[Snapshot],
    *,
    current_factor: float | None = None,
    hysteresis_pct: float = 0.2,
) -> float:
    """根据最近 N 个快照的波动率状态分布算波动率系数（带滞回缓冲）。

    统计最近 12 个桶里 vol_regime 的分布：
      - 80% 以上是 low   → 0.8x  波动很小，层可以密一点
      - 正常分布          → 1.0x  正常
      - 50% 以上是 high  → 1.5x  波动大，层间距拉大

    滞回缓冲：
      - 上升（normal→high）需要更严格的阈值（60%）
      - 下降（high→normal）需要更宽松的阈值（40%）
      - 这样在中间区间不会来回跳
    """
    if not snapshots:
        return 1.0

    recent = snapshots[:12]  # 最近 12 个桶
    total = len(recent)
    if total == 0:
        return 1.0

    low_count = sum(1 for s in recent if s.vol_regime == "low")
    high_count = sum(1 for s in recent if s.vol_regime == "high")

    low_ratio = low_count / total
    high_ratio = high_count / total

    # 滞回缓冲：上升用严格阈值，下降用宽松阈值
    h = hysteresis_pct
    if current_factor is None:
        # 第一次，用正常阈值
        low_threshold = 0.8
        high_threshold = 0.5
    elif current_factor >= 1.5:
        # 当前在 high，降回 normal 需要更宽松（high 占比 < 40% 就降）
        low_threshold = 0.8
        high_threshold = 0.5 * (1 - h)  # 0.4
    elif current_factor <= 0.8:
        # 当前在 low，升到 normal 需要更宽松（low 占比 < 64% 就升）
        low_threshold = 0.8 * (1 - h)  # 0.64
        high_threshold = 0.5
    else:
        # 当前在 normal，升到 high 需要更严格（high 占比 > 60% 才升）
        low_threshold = 0.8
        high_threshold = 0.5 * (1 + h)  # 0.6

    if low_ratio >= low_threshold:
        return 0.8
    elif high_ratio >= high_threshold:
        return 1.5
    else:
        return 1.0


# ── 主入口：调整参数 ──────────────────────────────────────────────────────


def adapt_profile(
    base: OneWayProfile,
    *,
    vol_factor: float,
    vol_regime_factor: float,
) -> OneWayProfile:
    """用系数调整基础参数，返回新的 OneWayProfile。

    被影响的参数：
      - base_size_usdc         × vol_factor          仓位跟着成交量走
      - q_max_usdc             × vol_factor          最大持仓也跟着走
      - ow_wall_min_gap_ticks  × vol_regime_factor   波动大时层间距拉大
      - ow_panic_toxicity      ÷ vol_regime_factor   波动大时更容易触发恐慌
        （设了下限 0.3，不会除得太低）

    不被影响的参数（策略逻辑层）：
      - ow_side（交易方向）
      - ow_exit_days_before（距到期几天只卖不买）
      - event_cooloff_s（事件冷却）
      - ow_wall_pct_of_24h（墙阈值比例，本身就是比例）
      - ow_pyramid_shares（金字塔权重结构）
      - ow_inv_low / ow_inv_mid（仓位刹车点）
    """
    adjusted_toxicity = base.ow_panic_toxicity / vol_regime_factor
    adjusted_toxicity = max(0.3, adjusted_toxicity)  # 下限保护

    return base.model_copy(update={
        "base_size_usdc": base.base_size_usdc * vol_factor,
        "q_max_usdc": base.q_max_usdc * vol_factor,
        "ow_wall_min_gap_ticks": max(1, int(round(base.ow_wall_min_gap_ticks * vol_regime_factor))),
        "ow_panic_toxicity": adjusted_toxicity,
    })


# ── 高级封装：从数据库读数据 → 算系数 → 调整参数 ──────────────────────────


class AdaptiveEngine:
    """动态参数引擎：封装缓存 + 重算频率 + 数据库查询。

    用法：
        engine = AdaptiveEngine(store, hysteresis_pct=0.2, recalc_interval_s=7200)
        result = engine.get_profile(condition_id, base_profile)
        if result.just_switched:
            # 模式切换了，触发重报价
            ...
    """

    def __init__(
        self,
        store: SnapshotStore,
        *,
        hysteresis_pct: float = 0.2,
        recalc_interval_s: int = 7200,  # 2 小时重算一次
        conservative_vol_factor: float = 0.3,
        conservative_vol_regime_factor: float = 1.5,
        transition_vol_factor: float = 0.5,
        transition_vol_regime_factor: float = 1.2,
        new_market_no_order: bool = False,  # True = 新市场完全不挂买单
        min_buckets_normal: int = 6,       # 最少几个桶才算正常模式
        vol_thresholds: tuple[float, float, float] = (5.0, 20.0, 100.0),
    ) -> None:
        self._store = store
        self._cache = AdaptiveCache(hysteresis_pct)
        self._recalc_interval = recalc_interval_s
        self._conservative_vol = conservative_vol_factor
        self._conservative_regime = conservative_vol_regime_factor
        self._transition_vol = transition_vol_factor
        self._transition_regime = transition_vol_regime_factor
        self._new_market_no_order = new_market_no_order
        self._min_buckets_normal = min_buckets_normal
        self._vol_thresholds = vol_thresholds
        self._last_recalc: dict[str, float] = {}

    def get_profile(self, condition_id: str, base: OneWayProfile) -> AdaptiveResult:
        """获取调整后的 profile。带缓存和重算频率控制。

        返回 AdaptiveResult，包含调整后的 profile 和 just_switched 标记。
        如果 just_switched=True，说明这次调用刚切换了模式，engine 应该触发重报价。
        """
        now = time.time()
        last = self._last_recalc.get(condition_id, 0.0)

        just_switched = False

        # 还没到重算时间，用缓存的系数
        if now - last < self._recalc_interval:
            factors = self._cache.get(condition_id)
            if factors is not None:
                profile = adapt_profile(
                    base,
                    vol_factor=factors.vol_factor,
                    vol_regime_factor=factors.vol_regime_factor,
                )
                return AdaptiveResult(profile=profile, just_switched=False)

        # 重新计算系数
        factors = self._calc_factors(condition_id)
        factors, just_switched = self._cache.maybe_update(condition_id, factors)
        self._last_recalc[condition_id] = now

        # 重算日志
        log.info(
            "adaptive_recalc",
            cid=condition_id[:8],
            buckets=factors.bucket_count,
            vol_factor=round(factors.vol_factor, 2),
            regime_factor=round(factors.vol_regime_factor, 2),
            conservative=factors.is_conservative,
            just_switched=just_switched,
        )

        profile = adapt_profile(
            base,
            vol_factor=factors.vol_factor,
            vol_regime_factor=factors.vol_regime_factor,
        )
        return AdaptiveResult(profile=profile, just_switched=just_switched)

    def _calc_factors(self, condition_id: str) -> MarketFactors:
        """从数据库读快照数据，算两个系数。"""
        # 取最近 12 个快照（24 小时）
        snapshots = self._get_recent_snapshots(condition_id, limit=12)
        bucket_count = len(snapshots)

        # 场景 1：完全没有数据 → 最保守模式
        if bucket_count < 1:
            vol_factor = 0.0 if self._new_market_no_order else self._conservative_vol
            return MarketFactors(
                vol_factor=vol_factor,
                vol_regime_factor=self._conservative_regime,
                bucket_count=bucket_count,
                is_conservative=True,
            )

        # 场景 2：数据不足 min_buckets_normal → 过渡模式
        if bucket_count < self._min_buckets_normal:
            return MarketFactors(
                vol_factor=self._transition_vol,
                vol_regime_factor=self._transition_regime,
                bucket_count=bucket_count,
                is_conservative=True,
            )

        # 场景 3：数据足够 → 正常计算（带滞回缓冲）
        # 异常值过滤：如果采集器挂了恢复后，某个桶的 volume_delta 可能特别大
        # 超过平均值 3 倍的就忽略，避免突然跳变
        volumes = [s.volume_delta for s in snapshots]
        avg_volume = sum(volumes) / len(volumes)
        filtered_volumes = [v for v in volumes if v < avg_volume * 3.0]
        if filtered_volumes:  # 如果过滤后还有数据，用过滤后的
            avg_volume = sum(filtered_volumes) / len(filtered_volumes)

        # 滞回缓冲：根据当前模式调整阈值
        old_factors = self._cache.get(condition_id)
        old_vol_factor = old_factors.vol_factor if old_factors else None
        adjusted_thresholds = self._adjust_thresholds_with_hysteresis(old_vol_factor)

        vol_factor = calc_vol_factor(avg_volume, adjusted_thresholds)

        # 波动率系数也带滞回缓冲
        old_regime_factor = old_factors.vol_regime_factor if old_factors else None
        vol_regime_factor = calc_vol_regime_factor(
            snapshots,
            current_factor=old_regime_factor,
            hysteresis_pct=self._cache._hysteresis,
        )

        return MarketFactors(
            vol_factor=vol_factor,
            vol_regime_factor=vol_regime_factor,
            bucket_count=bucket_count,
            is_conservative=False,
        )

    def _adjust_thresholds_with_hysteresis(
        self, current_vol_factor: float | None
    ) -> tuple[float, float, float]:
        """根据当前模式调整阈值，实现滞回缓冲。

        上升用正常阈值，下降用更严格的阈值（× 0.8），
        这样在两个档位之间有一个缓冲区，不会来回跳。
        """
        low, mid, high = self._vol_thresholds
        h = 1.0 - self._cache._hysteresis  # 比如 0.8

        # 第一次，没有历史模式，用正常阈值
        if current_vol_factor is None:
            return self._vol_thresholds

        # 根据当前模式调整下降阈值（上升阈值不变）
        if current_vol_factor >= 1.5:
            return (low, mid, high * h)   # 当前 1.5x，降到 1.0x 需要更严格
        elif current_vol_factor >= 1.0:
            return (low, mid * h, high)   # 当前 1.0x，降到 0.7x 需要更严格
        elif current_vol_factor >= 0.7:
            return (low * h, mid, high)   # 当前 0.7x，降到 0.3x 需要更严格
        else:
            return self._vol_thresholds   # 当前 0.3x，最低档，不用降了

    def _get_recent_snapshots(self, condition_id: str, limit: int = 12) -> list[Snapshot]:
        """从数据库取最近 N 个快照，新的在前。"""
        conn = self._store._conn
        rows = conn.execute(
            "SELECT * FROM market_snapshots "
            "WHERE condition_id=? ORDER BY bucket_ts DESC LIMIT ?",
            (condition_id, limit),
        ).fetchall()

        from polymaker.catalog.snapshots import _row_to_snapshot
        return [_row_to_snapshot(row) for row in rows]
