#!/usr/bin/env python3
"""查询指定市场的完整奖励参数。

用法:
    python query_rewards.py "clarity act"
    python query_rewards.py --condition-id 0x...
    python query_rewards.py --slug clarity-act-hr3633-signed-into-law-in-2026

查询内容:
    1. Gamma API: 市场元数据 (rewardsMinSize, rewardsMaxSpread, feeSchedule)
    2. CLOB API: 市场详细配置 (r 字段 = rewards config, mos, mts)
    3. CLOB /sampling-markets: rewards_daily_rate (每天的奖励速率)
    4. Incentives API: discountFactor, targetSize, rewardPool (新版奖励)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from typing import Any

import httpx

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
INCENTIVES_HOST = "https://api.prod.polymarketexchange.com"
USDC_ADDRESS = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"


def print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_kv(key: str, value: Any, indent: int = 0) -> None:
    prefix = "  " * indent
    if isinstance(value, float):
        print(f"{prefix}{key}: {value:.6f}")
    elif isinstance(value, dict):
        print(f"{prefix}{key}:")
        for k, v in value.items():
            print_kv(str(k), v, indent + 1)
    elif isinstance(value, list):
        print(f"{prefix}{key}: [{len(value)} items]")
        for i, item in enumerate(value[:5]):
            print_kv(f"[{i}]", item, indent + 1)
        if len(value) > 5:
            print(f"{'  '*(indent+1)}... and {len(value)-5} more")
    else:
        print(f"{prefix}{key}: {value}")


async def search_gamma(client: httpx.AsyncClient, query: str) -> list[dict[str, Any]]:
    """通过 Gamma API 搜索市场。"""
    r = await client.get(
        f"{GAMMA_HOST}/markets",
        params={
            "limit": 10,
            "order": "volume24hr",
            "ascending": "false",
            "closed": "false",
        },
    )
    r.raise_for_status()
    markets = r.json()
    # 过滤包含查询关键词的市场
    query_lower = query.lower()
    results = []
    for m in markets:
        question = (m.get("question") or "").lower()
        slug = (m.get("slug") or "").lower()
        if query_lower in question or query_lower in slug:
            results.append(m)
    return results


async def get_gamma_market(client: httpx.AsyncClient, condition_id: str) -> dict[str, Any] | None:
    """通过 Gamma API 获取单个市场的完整信息。"""
    r = await client.get(
        f"{GAMMA_HOST}/markets",
        params={"condition_ids": condition_id, "limit": 5},
    )
    r.raise_for_status()
    markets = r.json()
    for m in markets:
        if m.get("conditionId") == condition_id:
            return m
    return markets[0] if markets else None


async def get_clob_market_info(client: httpx.AsyncClient, condition_id: str) -> dict[str, Any]:
    """通过 CLOB API 获取市场详细配置 (r 字段 = rewards config)。"""
    r = await client.get(f"{CLOB_HOST}/clob/clob-markets/{condition_id}")
    r.raise_for_status()
    return r.json()


async def get_sampling_market(client: httpx.AsyncClient, condition_id: str) -> dict[str, Any] | None:
    """通过 CLOB /sampling-markets 获取 rewards_daily_rate。"""
    cursor = ""
    for _ in range(100):  # 最多翻100页
        r = await client.get(
            f"{CLOB_HOST}/sampling-markets",
            params={"next_cursor": cursor},
        )
        r.raise_for_status()
        data = r.json()
        for m in data.get("data", []):
            if m.get("condition_id") == condition_id:
                return m
        cursor = data.get("next_cursor") or ""
        if not cursor or cursor == "LTE=":
            break
    return None


async def get_incentives(client: httpx.AsyncClient, slug: str) -> dict[str, Any] | None:
    """通过 Incentives API 获取新版奖励参数。"""
    try:
        r = await client.get(
            f"{INCENTIVES_HOST}/v1/incentives",
            params={"symbols": slug},
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        print(f"  [Incentives API 不可用: {exc}]")
        return None


def analyze_rewards(
    gamma: dict[str, Any],
    clob: dict[str, Any],
    sampling: dict[str, Any] | None,
    incentives: dict[str, Any] | None,
) -> None:
    """分析并汇总奖励参数。"""

    print_section("1. Gamma API 市场元数据")
    print_kv("question", gamma.get("question"))
    print_kv("slug", gamma.get("slug"))
    print_kv("conditionId", gamma.get("conditionId"))
    print_kv("endDate", gamma.get("endDate"))
    print_kv("active", gamma.get("active"))
    print_kv("closed", gamma.get("closed"))
    print_kv("acceptingOrders", gamma.get("acceptingOrders"))
    print_kv("volumeNum", gamma.get("volumeNum"))
    print_kv("volume24hr", gamma.get("volume24hr"))
    print_kv("volume24hrClob", gamma.get("volume24hrClob"))
    print_kv("liquidityNum", gamma.get("liquidityNum"))
    print_kv("bestBid", gamma.get("bestBid"))
    print_kv("bestAsk", gamma.get("bestAsk"))
    print_kv("orderPriceMinTickSize", gamma.get("orderPriceMinTickSize"))
    print_kv("orderMinSize", gamma.get("orderMinSize"))
    print_kv("negRisk", gamma.get("negRisk"))
    print_kv("feesEnabled", gamma.get("feesEnabled"))

    print_section("1.1 Gamma 奖励相关字段")
    print_kv("rewardsMinSize", gamma.get("rewardsMinSize"))
    print_kv("rewardsMaxSpread", gamma.get("rewardsMaxSpread"))
    print_kv("rewardsDailyRate (Gamma)", gamma.get("rewardsDailyRate"))
    print_kv("feeSchedule", gamma.get("feeSchedule"))
    print_kv("tags", gamma.get("tags"))
    print_kv("category", gamma.get("category"))
    print_kv("subcategory", gamma.get("subcategory"))

    print_section("2. CLOB API 市场详细配置")
    print_kv("gst (game start time)", clob.get("gst"))
    print_kv("mos (min order size)", clob.get("mos"))
    print_kv("mts (min tick size)", clob.get("mts"))
    print_kv("mbf (maker base fee bps)", clob.get("mbf"))
    print_kv("tbf (taker base fee bps)", clob.get("tbf"))
    print_kv("rfqe (RFQ enabled)", clob.get("rfqe"))
    print_kv("itode (taker order delay enabled)", clob.get("itode"))
    print_kv("ibce (Blockaid check enabled)", clob.get("ibce"))
    print_kv("oas (min order age seconds)", clob.get("oas"))
    print_kv("fd (fee curve params)", clob.get("fd"))

    print_section("2.1 CLOB rewards config (r 字段)")
    r_config = clob.get("r") or {}
    if r_config:
        print_kv("完整 r 字段", r_config)
    else:
        print("  (r 字段为空 - 这个市场可能没有启用 CLOB 级别的奖励配置)")

    print_section("3. CLOB /sampling-markets 奖励速率")
    if sampling:
        print_kv("condition_id", sampling.get("condition_id"))
        print_kv("question", sampling.get("question"))
        rewards = sampling.get("rewards") or {}
        print_kv("rewards", rewards)
        rates = rewards.get("rates") or []
        print_kv("rates", rates)
        for rate in rates:
            asset = rate.get("asset_address", "")
            daily_rate = rate.get("rewards_daily_rate", 0)
            print(f"    - asset: {asset}")
            print(f"      rewards_daily_rate: {daily_rate} USDC/天")
        print_kv("rewards.min_size", rewards.get("min_size"))
        print_kv("rewards.max_spread", rewards.get("max_spread"))
    else:
        print("  (这个市场不在 sampling-markets 列表中 - 可能没有启用每日奖励)")

    print_section("4. Incentives API (新版奖励参数)")
    if incentives:
        programs = incentives.get("programs") or []
        print_kv("programs 数量", len(programs))
        for prog in programs:
            print_kv("marketSlug", prog.get("marketSlug"))
            periods = prog.get("timePeriods") or []
            print_kv("timePeriods 数量", len(periods))
            for period in periods:
                print_kv("  period", period.get("period"))
                print_kv("  programId", period.get("programId"))
                print_kv("  programType", period.get("programType"))
                print_kv("  status", period.get("status"))
                print_kv("  start", period.get("start"))
                print_kv("  end", period.get("end"))
                print_kv("  rewardPool", period.get("rewardPool"))
                print_kv("  discountFactor", period.get("discountFactor"))
                print_kv("  targetSize", period.get("targetSize"))
                print_kv("  createdAt", period.get("createdAt"))
    else:
        print("  (Incentives API 不可用或未返回数据)")

    print_section("5. 奖励参数汇总分析")

    # 收集所有奖励参数
    rewards_min_size = gamma.get("rewardsMinSize") or 0
    rewards_max_spread = gamma.get("rewardsMaxSpread") or 0
    daily_rate = 0.0
    if sampling:
        rewards = sampling.get("rewards") or {}
        for rate in (rewards.get("rates") or []):
            if str(rate.get("asset_address", "")).lower() == USDC_ADDRESS.lower():
                daily_rate = float(rate.get("rewards_daily_rate", 0) or 0)
                break

    print(f"  奖励参数来源:")
    print(f"    - rewardsMinSize: {rewards_min_size} (来自 Gamma)")
    print(f"    - rewardsMaxSpread: {rewards_max_spread} (来自 Gamma)")
    print(f"    - rewards_daily_rate: {daily_rate} USDC/天 (来自 CLOB sampling-markets)")

    if daily_rate > 0:
        print(f"\n  ✅ 这个市场有启用每日奖励: ${daily_rate:.2f}/天")
        end_date = gamma.get("endDate")
        if end_date:
            print(f"     结束日期: {end_date}")
            # 估算总奖励
            try:
                from datetime import datetime
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.now(end_dt.tzinfo)
                days_left = (end_dt - now).days
                if days_left > 0:
                    total_estimated = daily_rate * days_left
                    print(f"     剩余天数: ~{days_left} 天")
                    print(f"     预计剩余总奖励: ${total_estimated:.2f}")
                    print(f"     (如果奖励速率保持不变)")
            except (ValueError, TypeError):
                pass
    else:
        print(f"\n  ⚠️  这个市场的 rewards_daily_rate = 0")
        print(f"     可能原因:")
        print(f"     1. 这个市场没有启用每日奖励")
        print(f"     2. 奖励已经结束")
        print(f"     3. 页面显示的 $2,506 可能是赞助奖励 (Add Rewards)，不是每日速率")

    if rewards_min_size > 0 or rewards_max_spread > 0:
        print(f"\n  挂单约束:")
        print(f"    - 每笔最小挂单量: {rewards_min_size} 份")
        print(f"    - 最大价差: ±{rewards_max_spread} (相对于中间价)")

    # 检查 Incentives API
    if incentives:
        programs = incentives.get("programs") or []
        if programs:
            print(f"\n  新版奖励 (Incentives API):")
            for prog in programs:
                for period in (prog.get("timePeriods") or []):
                    if period.get("status") == "active":
                        print(f"    - rewardPool: ${period.get('rewardPool', 0)}")
                        print(f"    - discountFactor: {period.get('discountFactor')}")
                        print(f"    - targetSize: {period.get('targetSize')}")
                        print(f"    - period: {period.get('period')}")

    print_section("6. 结论")
    print(f"  页面显示的 'Rewards $2,506' 最可能是:")
    if daily_rate > 0 and abs(daily_rate - 2506) < 1:
        print(f"    ✅ 每日奖励速率 (rewards_daily_rate = ${daily_rate:.2f}/天)")
    elif daily_rate > 0:
        print(f"    ⚠️  每日奖励速率是 ${daily_rate:.2f}/天，与页面显示的 $2,506 不一致")
        print(f"       $2,506 可能是:")
        print(f"       - 赞助奖励总额 (通过 Add Rewards 添加)")
        print(f"       - 某个时间段的总奖励池")
        print(f"       - 页面显示口径与 API 不同")
    else:
        print(f"    ⚠️  API 返回 rewards_daily_rate = 0")
        print(f"       $2,506 可能是:")
        print(f"       - 赞助奖励总额 (通过 Add Rewards 添加)")
        print(f"       - 已结束的奖励")
        print(f"       - 新版 Incentives API 的 rewardPool")


async def main() -> None:
    parser = argparse.ArgumentParser(description="查询 Polymarket 市场奖励参数")
    parser.add_argument("query", nargs="?", help="搜索关键词 (市场问题或 slug)")
    parser.add_argument("--condition-id", help="直接指定 condition_id")
    parser.add_argument("--slug", help="直接指定市场 slug")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args()

    if not args.query and not args.condition_id and not args.slug:
        parser.print_help()
        sys.exit(1)

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 第一步: 找到市场
        condition_id = args.condition_id
        slug = args.slug
        gamma_market = None

        if condition_id:
            print(f"正在查询 condition_id: {condition_id} ...")
            gamma_market = await get_gamma_market(client, condition_id)
        elif slug:
            print(f"正在搜索 slug: {slug} ...")
            # 用 slug 搜索
            r = await client.get(
                f"{GAMMA_HOST}/markets",
                params={"slug": slug, "limit": 5},
            )
            r.raise_for_status()
            markets = r.json()
            if markets:
                gamma_market = markets[0]
                condition_id = gamma_market.get("conditionId")
        else:
            print(f"正在搜索: {args.query} ...")
            results = await search_gamma(client, args.query)
            if not results:
                print("未找到匹配的市场")
                sys.exit(1)
            print(f"\n找到 {len(results)} 个匹配市场:")
            for i, m in enumerate(results):
                print(f"  [{i}] {m.get('question')}")
                print(f"      slug: {m.get('slug')}")
                print(f"      volume24hr: ${m.get('volume24hr', 0)}")
            if len(results) == 1:
                gamma_market = results[0]
                condition_id = gamma_market.get("conditionId")
                slug = gamma_market.get("slug")
            else:
                choice = input("\n选择市场编号 (0): ").strip() or "0"
                gamma_market = results[int(choice)]
                condition_id = gamma_market.get("conditionId")
                slug = gamma_market.get("slug")

        if not gamma_market or not condition_id:
            print("无法获取市场信息")
            sys.exit(1)

        slug = slug or gamma_market.get("slug") or ""
        print(f"\n市场: {gamma_market.get('question')}")
        print(f"condition_id: {condition_id}")
        print(f"slug: {slug}")

        # 第二步: 查询各 API
        print("\n正在查询 CLOB API ...")
        try:
            clob_info = await get_clob_market_info(client, condition_id)
        except httpx.HTTPError as exc:
            print(f"  CLOB API 失败: {exc}")
            clob_info = {}

        print("正在查询 CLOB /sampling-markets (可能需要翻页)...")
        sampling = await get_sampling_market(client, condition_id)

        print("正在查询 Incentives API ...")
        incentives = await get_incentives(client, slug)

        # 第三步: 输出原始 JSON (如果需要)
        if args.json:
            print_section("原始 JSON 数据")
            print("\n--- Gamma ---")
            print(json.dumps(gamma_market, indent=2, ensure_ascii=False))
            print("\n--- CLOB ---")
            print(json.dumps(clob_info, indent=2, ensure_ascii=False))
            print("\n--- Sampling ---")
            print(json.dumps(sampling, indent=2, ensure_ascii=False))
            print("\n--- Incentives ---")
            print(json.dumps(incentives, indent=2, ensure_ascii=False))

        # 第四步: 分析并汇总
        analyze_rewards(gamma_market, clob_info, sampling, incentives)


if __name__ == "__main__":
    asyncio.run(main())
#（注：内容由AI生成）
