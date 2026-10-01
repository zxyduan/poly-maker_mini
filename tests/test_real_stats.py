"""
一次性验证脚本：拉真实市场数据，看看 daily_stats 采集器输出对不对。

跑法：
  uv run python scripts/test_real_stats.py

不需要钱包、不需要连订单簿、纯公开 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# 确保能 import 项目模块
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

from polymaker.catalog.daily_stats import DailyStatsCollector, DailyStatsStore


# ── 改成你要测的市场 slug ─────────────────────────────────────────────────
SLUG = "clarity-act-signed-into-law-in-2026"
DB_PATH = "state.db"  # 直接用项目的 state.db，验证完数据也留着


async def main() -> None:
    # 第 1 步：从 Gamma 按 slug 查市场，拿 condition_id 和 yes_token_id
    print(f"=== 查市场: {SLUG} ===")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": SLUG, "limit": 1},
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            print("找不到这个市场")
            return
        m = data[0]

    condition_id = m["conditionId"]
    # clobTokenIds 是字符串化的 JSON 数组，要 json.loads
    token_ids = json.loads(m["clobTokenIds"])
    yes_token_id = token_ids[0]

    print(f"condition_id: {condition_id}")
    print(f"yes_token_id: {yes_token_id}")
    print(f"问题: {m.get('question', '')}")
    print(f"当前累计成交量 volumeNum: ${m.get('volumeNum', 0):,.2f}")
    print(f"24h 成交量 volume24hr: ${m.get('volume24hr', 0):,.2f}")
    print()

    # 第 2 步：跑一次采集
    print("=== 跑一次采集 ===")
    store = DailyStatsStore(DB_PATH)
    collector = DailyStatsCollector(store)

    try:
        await collector.collect(condition_id, yes_token_id)
        print("采集完成\n")
    finally:
        await collector.aclose()

    # 第 3 步：打印结果
    print("=== 采集结果（最近 10 天）===")
    rows = store.history(condition_id, limit=10)
    if not rows:
        print("没拿到数据")
        return

    print(f"{'日期':<14} {'成交量':>12} {'开盘':>8} {'最高':>8} {'最低':>8} {'收盘':>8} {'振幅':>8} {'冻结':>4}")
    print("-" * 80)
    for r in rows:
        vol = f"${r.volume_usdc:,.0f}" if r.volume_usdc > 0 else "-"
        o = f"{r.price_open:.4f}" if r.price_open else "-"
        h = f"{r.price_high:.4f}" if r.price_high else "-"
        lo = f"{r.price_low:.4f}" if r.price_low else "-"
        c = f"{r.price_close:.4f}" if r.price_close else "-"
        rng = f"{r.range_pct*100:.1f}%" if r.range_pct else "-"
        frz = "✓" if r.frozen else "今天"
        print(f"{r.date:<14} {vol:>12} {o:>8} {h:>8} {lo:>8} {c:>8} {rng:>8} {frz:>4}")

    # 第 4 步：打印 recent_avg（路由模块会用到的值）
    print()
    print("=== recent_avg（路由判断用的 3 日均值）===")
    avg_vol, avg_range = store.recent_avg(condition_id, days=3)
    print(f"3 日均成交量: ${avg_vol:,.2f}")
    print(f"3 日均振幅: {avg_range*100:.2f}%")

    store.close()


if __name__ == "__main__":
    asyncio.run(main())
#（注：内容由AI生成）
