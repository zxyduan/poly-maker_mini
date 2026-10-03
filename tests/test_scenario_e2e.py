"""
端到端场景模拟测试：验证动态参数模块在真实场景下是否可靠。

模拟场景：
  1. 新市场刚加入（数据库里没数据）→ 应该走保守模式
  2. 数据慢慢攒（1桶→3桶→6桶）→ 过渡模式 → 正常模式
  3. 死盘市场（成交量很低）→ vol_factor 应该是 0.3
  4. 活跃盘市场（成交量很高）→ vol_factor 应该是 1.5
  5. 波动率高的市场 → vol_regime_factor 应该是 1.5
  6. 模式切换后，estimators 会不会重新初始化？
  7. 重算间隔内，重复调用会不会用缓存？
  8. 滞回缓冲：系数在阈值附近抖动时，会不会频繁切换？
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from polymaker.catalog.adaptive import AdaptiveEngine
from polymaker.catalog.snapshots import SnapshotStore, _row_to_snapshot
from polymaker.config import OneWayProfile


def make_base_profile() -> OneWayProfile:
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


def insert_snapshot(
    store: SnapshotStore,
    condition_id: str,
    bucket_ts: int,
    *,
    volume_delta: float = 10.0,
    vol_regime: str = "normal",
) -> None:
    """直接往数据库插一条快照。"""
    conn = store._conn
    conn.execute(
        """INSERT OR REPLACE INTO market_snapshots (
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
            condition_id, "test-slug", bucket_ts,
            0.1, 0.11, 0.12, 0.09,  # OHLC
            volume_delta,
            "up", 0.05, 3,  # trend
            "up", 0.02, 0.6,  # trend_direction, drawdown, percentile
            100.0, 80.0, 2, 0.1,  # depth, walls
            0.02, 0.015, vol_regime,  # vol_short, vol_long, vol_regime
            time.time(),
        ),
    )
    conn.commit()


def scenario_1_new_market_no_data():
    """场景 1：新市场刚加入，数据库里没数据。
    预期：走保守模式，base_size 缩到 30%。
    """
    print("=" * 60)
    print("场景 1：新市场刚加入（无数据）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        engine = AdaptiveEngine(store)
        base = make_base_profile()

        result = engine.get_profile("0xnew_market", base)
        p = result.profile

        print(f"  base_size_usdc: {base.base_size_usdc:.2f} → {p.base_size_usdc:.2f}")
        print(f"  q_max_usdc: {base.q_max_usdc:.2f} → {p.q_max_usdc:.2f}")
        print(f"  ow_wall_min_gap_ticks: {base.ow_wall_min_gap_ticks} → {p.ow_wall_min_gap_ticks}")
        print(f"  ow_panic_toxicity: {base.ow_panic_toxicity:.2f} → {p.ow_panic_toxicity:.2f}")
        print(f"  just_switched: {result.just_switched}")

        # 验证：保守模式 vol_factor = 0.3
        assert p.base_size_usdc == 30.0 * 0.3, f"预期 9.0，实际 {p.base_size_usdc}"
        # 保守模式 vol_regime_factor = 1.5
        assert p.ow_wall_min_gap_ticks == 8, f"预期 8，实际 {p.ow_wall_min_gap_ticks}"  # 5 × 1.5 = 7.5 → 8
        assert p.ow_panic_toxicity == 0.6 / 1.5, f"预期 0.4，实际 {p.ow_panic_toxicity}"
        # 第一次调用，just_switched 应该是 True
        assert result.just_switched is True, "第一次调用应该 just_switched=True"

        print("  ✓ 验证通过")
        store.close()


def scenario_2_data_grows():
    """场景 2：数据慢慢攒（1桶 → 3桶 → 6桶）。
    预期：过渡模式（vol_factor=0.5）→ 正常模式。
    """
    print("\n" + "=" * 60)
    print("场景 2：数据慢慢攒（1桶 → 3桶 → 6桶）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        engine = AdaptiveEngine(store, recalc_interval_s=1)  # 1 秒就重算，方便测试
        base = make_base_profile()
        cid = "0xabc"
        now = int(time.time())

        # 第 1 桶（2 小时前）
        insert_snapshot(store, cid, now - 7200, volume_delta=50.0)
        result = engine.get_profile(cid, base)
        print(f"  1 个桶: base_size={result.profile.base_size_usdc:.2f} (过渡模式)")
        assert result.profile.base_size_usdc == 30.0 * 0.5, "过渡模式应该是 15.0"

        time.sleep(1.1)  # 等重算间隔过

        # 第 3 桶
        for i in range(2):
            insert_snapshot(store, cid, now - 7200 * (2 + i), volume_delta=50.0)
        result = engine.get_profile(cid, base)
        print(f"  3 个桶: base_size={result.profile.base_size_usdc:.2f} (过渡模式)")
        assert result.profile.base_size_usdc == 30.0 * 0.5, "过渡模式应该是 15.0"

        time.sleep(1.1)  # 等重算间隔过

        # 第 6 桶（数据够了）
        for i in range(3):
            insert_snapshot(store, cid, now - 7200 * (4 + i), volume_delta=50.0)
        result = engine.get_profile(cid, base)
        print(f"  6 个桶: base_size={result.profile.base_size_usdc:.2f} (正常模式)")
        # avg_volume = $50，落在 mid~high → vol_factor = 1.0
        assert result.profile.base_size_usdc == 30.0 * 1.0, "正常模式应该是 30.0"

        print("  ✓ 验证通过")
        store.close()


def scenario_3_dead_market():
    """场景 3：死盘市场（24h 平均桶成交量 $2）。
    预期：vol_factor = 0.3（死盘缩仓）。
    """
    print("\n" + "=" * 60)
    print("场景 3：死盘市场（成交量 $2/桶）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        engine = AdaptiveEngine(store)
        base = make_base_profile()
        cid = "0xdead"
        now = int(time.time())

        # 插 12 个桶，每个桶成交量 $2
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=2.0, vol_regime="low")

        result = engine.get_profile(cid, base)
        p = result.profile

        print(f"  12 个桶，每桶 $2:")
        print(f"    base_size: {base.base_size_usdc:.2f} → {p.base_size_usdc:.2f}")
        print(f"    ow_wall_min_gap_ticks: {base.ow_wall_min_gap_ticks} → {p.ow_wall_min_gap_ticks}")
        print(f"    ow_panic_toxicity: {base.ow_panic_toxicity:.2f} → {p.ow_panic_toxicity:.2f}")

        # avg_volume = $2 < $5 → vol_factor = 0.3
        assert p.base_size_usdc == 30.0 * 0.3, f"预期 9.0，实际 {p.base_size_usdc}"
        # 全部是 low 波动 → vol_regime_factor = 0.8
        assert p.ow_wall_min_gap_ticks == 4, f"预期 4，实际 {p.ow_wall_min_gap_ticks}"  # 5 × 0.8 = 4
        assert p.ow_panic_toxicity == 0.6 / 0.8, f"预期 0.75，实际 {p.ow_panic_toxicity}"

        print("  ✓ 验证通过")
        store.close()


def scenario_4_hot_market():
    """场景 4：活跃盘市场（24h 平均桶成交量 $200）。
    预期：vol_factor = 1.5（活跃盘放大）。
    """
    print("\n" + "=" * 60)
    print("场景 4：活跃盘市场（成交量 $200/桶）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        engine = AdaptiveEngine(store)
        base = make_base_profile()
        cid = "0xhot"
        now = int(time.time())

        # 插 12 个桶，每个桶成交量 $200
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=200.0, vol_regime="high")

        result = engine.get_profile(cid, base)
        p = result.profile

        print(f"  12 个桶，每桶 $200:")
        print(f"    base_size: {base.base_size_usdc:.2f} → {p.base_size_usdc:.2f}")
        print(f"    q_max_usdc: {base.q_max_usdc:.2f} → {p.q_max_usdc:.2f}")
        print(f"    ow_wall_min_gap_ticks: {base.ow_wall_min_gap_ticks} → {p.ow_wall_min_gap_ticks}")
        print(f"    ow_panic_toxicity: {base.ow_panic_toxicity:.2f} → {p.ow_panic_toxicity:.2f}")

        # avg_volume = $200 > $100 → vol_factor = 1.5
        assert p.base_size_usdc == 30.0 * 1.5, f"预期 45.0，实际 {p.base_size_usdc}"
        assert p.q_max_usdc == 60.0 * 1.5, f"预期 90.0，实际 {p.q_max_usdc}"
        # 全部是 high 波动 → vol_regime_factor = 1.5
        assert p.ow_wall_min_gap_ticks == 8, f"预期 8，实际 {p.ow_wall_min_gap_ticks}"  # 5 × 1.5 = 7.5 → 8
        # 0.6 / 1.5 = 0.4，高于下限 0.3
        assert p.ow_panic_toxicity == 0.6 / 1.5, f"预期 0.4，实际 {p.ow_panic_toxicity}"

        print("  ✓ 验证通过")
        store.close()


def scenario_5_caching():
    """场景 5：重算间隔内，重复调用应该用缓存。
    预期：刚调用完，立刻再调用，结果一样，just_switched=False。
    """
    print("\n" + "=" * 60)
    print("场景 5：重算间隔内用缓存")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        engine = AdaptiveEngine(store, recalc_interval_s=7200)  # 2 小时才重算
        base = make_base_profile()
        cid = "0xcache"
        now = int(time.time())

        # 插 12 个桶
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=50.0)

        # 第一次调用
        result1 = engine.get_profile(cid, base)
        print(f"  第 1 次调用: base_size={result1.profile.base_size_usdc:.2f}, just_switched={result1.just_switched}")

        # 第二次调用（立刻）
        result2 = engine.get_profile(cid, base)
        print(f"  第 2 次调用: base_size={result2.profile.base_size_usdc:.2f}, just_switched={result2.just_switched}")

        assert result1.profile.base_size_usdc == result2.profile.base_size_usdc, "两次调用结果应该一样"
        assert result2.just_switched is False, "第二次调用应该 just_switched=False（用缓存）"

        print("  ✓ 验证通过")
        store.close()


def scenario_6_hysteresis():
    """场景 6：滞回缓冲。
    预期：系数在阈值附近抖动时，不会频繁切换。
    """
    print("\n" + "=" * 60)
    print("场景 6：滞回缓冲（阈值附近抖动）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        store = SnapshotStore(db_path)
        # 把重算间隔设短一点，方便测试
        engine = AdaptiveEngine(store, recalc_interval_s=0.1, hysteresis_pct=0.2)
        base = make_base_profile()
        cid = "0xhyst"
        now = int(time.time())

        # 先插 12 个桶，每个 $15（落在 0.7x 区间）
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=15.0)
        result1 = engine.get_profile(cid, base)
        print(f"  初始（$15/桶）: vol_factor={result1.profile.base_size_usdc / 30.0:.2f}")

        time.sleep(0.2)

        # 改成 $22（刚超过中阈值 $20）→ 应该切到 1.0x
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=22.0)
        result2 = engine.get_profile(cid, base)
        print(f"  变成 $22/桶: vol_factor={result2.profile.base_size_usdc / 30.0:.2f}, switched={result2.just_switched}")
        assert result2.profile.base_size_usdc / 30.0 == 1.0, "应该切到 1.0x"

        time.sleep(0.2)

        # 改回 $19（刚低于中阈值 $20）→ 滞回缓冲 20%，应该还保持 1.0x
        # （因为 20 × 0.8 = 16，$19 > $16，还在缓冲区里）
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=19.0)
        result3 = engine.get_profile(cid, base)
        print(f"  变回 $19/桶: vol_factor={result3.profile.base_size_usdc / 30.0:.2f}, switched={result3.just_switched}")
        # 应该还保持 1.0x（滞回缓冲）
        assert result3.profile.base_size_usdc / 30.0 == 1.0, "滞回缓冲应该保持 1.0x"

        time.sleep(0.2)

        # 改成 $15（低于 $16 的滞回线）→ 应该切回 0.7x
        for i in range(12):
            insert_snapshot(store, cid, now - 7200 * i, volume_delta=15.0)
        result4 = engine.get_profile(cid, base)
        print(f"  变成 $15/桶: vol_factor={result4.profile.base_size_usdc / 30.0:.2f}, switched={result4.just_switched}")
        # 应该切回 0.7x
        assert result4.profile.base_size_usdc / 30.0 == 0.7, "应该切回 0.7x"

        print("  ✓ 验证通过")
        store.close()


def main():
    print("\n" + "=" * 60)
    print("端到端场景模拟测试")
    print("=" * 60)

    scenario_1_new_market_no_data()
    scenario_2_data_grows()
    scenario_3_dead_market()
    scenario_4_hot_market()
    scenario_5_caching()
    scenario_6_hysteresis()

    print("\n" + "=" * 60)
    print("全部场景测试通过！")
    print("=" * 60)


if __name__ == "__main__":
    main()
