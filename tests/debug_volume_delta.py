"""
调试：验证成交量增量计算对不对。
连跑两次，打印每次的 cum_volume、prev_vol、delta。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from polymaker.catalog.daily_stats import DailyStatsCollector, DailyStatsStore

SLUG = "clarity-act-signed-into-law-in-2026"
CONDITION_ID = "0x9cb23d04b2ded06147482076688b69b487a8d982c63ebdda2ab3678cf27cf390"
YES_TOKEN_ID = "35198549486569600595408965368290795524428922595732253580266052028158040373233"


async def main() -> None:
    store = DailyStatsStore("state.db")
    collector = DailyStatsCollector(store)

    # 先查当前数据库里存了什么
    print("=== 跑之前，数据库里的状态 ===")
    prev = store.last_volume_num(CONDITION_ID)
    print(f"数据库里存的 last_volume_num (baseline): {prev}")
    rows = store.history(CONDITION_ID, limit=1)
    if rows:
        print(f"今天的 volume_usdc: {rows[0].volume_usdc}")
    print()

    # 跑第一次
    print("=== 第 1 次 collect() ===")
    await collector.collect(CONDITION_ID, YES_TOKEN_ID)
    prev1 = store.last_volume_num(CONDITION_ID)
    rows1 = store.history(CONDITION_ID, limit=1)
    print(f"collect 后 last_volume_num: {prev1}")
    print(f"collect 后 volume_usdc: {rows1[0].volume_usdc}")
    print()

    # 等 5 秒
    print("等 5 秒...")
    await asyncio.sleep(5)
    print()

    # 跑第二次
    print("=== 第 2 次 collect()（5 秒后）===")
    await collector.collect(CONDITION_ID, YES_TOKEN_ID)
    prev2 = store.last_volume_num(CONDITION_ID)
    rows2 = store.history(CONDITION_ID, limit=1)
    print(f"collect 后 last_volume_num: {prev2}")
    print(f"collect 后 volume_usdc: {rows2[0].volume_usdc}")
    print()

    # 算一下增量
    if prev1 and prev2:
        delta = prev2 - prev1
        print(f"=== 5 秒内的增量验证 ===")
        print(f"baseline 变化: {prev1} -> {prev2}")
        print(f"增量 = {prev2} - {prev1} = ${delta:.2f}")
        print(f"volume_usdc 变化: {rows1[0].volume_usdc} -> {rows2[0].volume_usdc}")
        print(f"累加量 = {rows2[0].volume_usdc - rows1[0].volume_usdc}")
        if abs(delta - (rows2[0].volume_usdc - rows1[0].volume_usdc)) < 0.01:
            print("✓ 增量计算正确")
        else:
            print("✗ 增量计算有问题！")

    await collector.aclose()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
#（注：内容由AI生成）
