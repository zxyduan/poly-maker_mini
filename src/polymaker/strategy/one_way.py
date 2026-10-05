"""One-way pyramid accumulator.

Unlike the two-sided maker, this strategy only works a single side (``ow_side``).
It accumulates by resting BUY bids *under* large resting walls in the book, in a
weighted pyramid that chains down successive walls; it exits held inventory with
a SELL resting one tick under the ask (or dumps into the bid when the book is
toxic / near market resolution).

Inventory utilization (held / q_max_shares) throttles adding:
  u <  ow_inv_low  -> full size
  u <  ow_inv_mid  -> half size
  u >= ow_inv_mid  -> no new buys
Within ``ow_exit_days_before`` of end date it is sell-only.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from polymaker.config import OneWayProfile
from polymaker.domain import Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookLevel, OrderBook
from polymaker.strategy.base import StrategyInputs

_EPS = 1e-9


def calc_fair_value(p: OneWayProfile) -> float:
    """计算今天的合理价值（按天线性衰减，到期归零）。

    衰减逻辑：
    - 配了 fv_start_date：从 fv_start_date 到 fv_end_date 线性衰减。
      起始日 fv = fv_initial，到期日 fv = 0。
    - 没配 fv_start_date：不衰减，直接返回 fv_initial（向后兼容）。

    Args:
        p: OneWayProfile 配置

    Returns:
        今天的合理价值
    """
    if not p.fv_end_date:
        return p.fv_initial

    try:
        end_date = datetime.fromisoformat(p.fv_end_date).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_left = (end_date - now).total_seconds() / 86400.0

        if days_left <= 0:
            return 0.0  # 到期了，合理价值归零

        # 没配起始日期：不衰减
        if not p.fv_start_date:
            return p.fv_initial

        start_date = datetime.fromisoformat(p.fv_start_date).replace(tzinfo=timezone.utc)
        total_days = (end_date - start_date).total_seconds() / 86400.0
        if total_days <= 0:
            return p.fv_initial

        if now < start_date:
            return p.fv_initial  # 还没开始衰减

        # 线性衰减：fv_now = fv_initial * days_left / total_days
        return p.fv_initial * max(0.0, days_left) / total_days
    except (ValueError, TypeError):
        return p.fv_initial


def _get_market_scenario(
    trend_streak: int,
    threshold_up: int = 3,
    threshold_down: int = -3,
) -> str:
    """判断市场场景：平静期/趋势向上期/趋势向下期。

    Args:
        trend_streak: 连续同向桶数（正=涨，负=跌）
        threshold_up: 连续涨多少桶算趋势向上
        threshold_down: 连续跌多少桶算趋势向下

    Returns:
        "calm" | "up_trend" | "down_trend"
    """
    if trend_streak >= threshold_up:
        return "up_trend"
    elif trend_streak <= threshold_down:
        return "down_trend"
    else:
        return "calm"


def _adjust_price_for_wall(
    book: OrderBook,
    side: Side,
    target_price: float,
    tick: float,
    our_size: float,
    our_prices: tuple[float, ...] = (),
) -> float:
    """调整挂单价格，避开别人的墙。

    如果我们算出来的价格旁边有别人的墙（挂单量 >= 我们份额的 50%），
    就比墙低 1 tick 挂单，抢在墙前面成交。
    自己的挂单不用避。

    Args:
        book: 订单簿
        side: 买还是卖
        target_price: 我们算出来的目标价格
        tick: 最小价格单位
        our_size: 我们这层要挂的份额
        our_prices: 我们自己挂单的价格列表

    Returns:
        调整后的价格
    """
    if book is None:
        return target_price

    # 墙的阈值：别人的挂单量 >= 我们份额的 50%
    wall_threshold = our_size * 0.5

    # 看 target_price 左右 1 tick 的位置有没有墙
    levels_to_check = [
        target_price - tick,
        target_price,
        target_price + tick,
    ]

    book_levels = book.bids if side == Side.BUY else book.asks

    for level_price in levels_to_check:
        # 如果这个价位是我们自己的挂单，跳过
        is_our_order = any(abs(p - level_price) < tick / 2 for p in our_prices)
        if is_our_order:
            continue

        # 找到这个价位的挂单量
        size_at_level = 0.0
        for p, s in book_levels.items():
            if abs(p - level_price) < tick / 2:
                size_at_level = s
                break

        # 如果这个价位有墙（别人的）
        if size_at_level >= wall_threshold:
            # 比墙低 1 tick 挂单
            adjusted = level_price - tick if side == Side.SELL else level_price + tick
            return round(adjusted, 6)

    return target_price


def construct_one_way_quotes(inp: StrategyInputs) -> TargetQuotes:
    m = inp.meta
    if not isinstance(inp.profile, OneWayProfile):
        raise TypeError(f"one_way quoter needs OneWayProfile, got {type(inp.profile).__name__}")
    p = inp.profile
    tick = m.tick_size
    dec = m.price_decimals
    cid = m.condition_id

    # hard stop: event / halt -> cancel everything, quote nothing
    if inp.regime in (Regime.EVENT, Regime.HALTED):
        return TargetQuotes(cid, inp.regime, ())

    # resolve the traded side and its book/position
    if p.ow_side == "yes":
        tok = m.yes.token_id
        book: OrderBook | None = inp.yes_book
        pos = inp.pos_yes
    else:
        tok = m.no.token_id
        book = inp.no_book
        pos = inp.pos_no

    quotes: list[Quote] = []
    held = pos.size
    q_max_shares = p.q_max_usdc / max(inp.fv, tick)
    util = (held / q_max_shares) if q_max_shares > 0 else 0.0

    # ── 计算今天的合理价值 ──
    fv = calc_fair_value(p)

    # ── 判断市场场景 ──
    scenario = _get_market_scenario(inp.trend_streak)

    # ─️ 根据场景调整挂单层数 ──
    effective_buy_layers = p.buy_layers
    effective_sell_layers = p.sell_layers

    if scenario == "up_trend":
        # 趋势向上：少挂 1 层买单，别追高
        effective_buy_layers = max(1, p.buy_layers - 1)
    elif scenario == "down_trend":
        # 趋势向下：正常挂买单，逢低吸筹
        pass

    # ── 计算每层间隔（根据波动率调整）──
    # 简化：用 base_layer_gap_ticks，后续可以根据 adaptive 的 vol_regime_factor 调整
    layer_gap_ticks = p.base_layer_gap_ticks

    near_end = inp.hours_to_end is not None and inp.hours_to_end <= p.ow_exit_days_before * 24.0
    panic = inp.toxicity >= p.ow_panic_toxicity

    bb: BookLevel | None = book.best_bid() if book is not None else None
    ba: BookLevel | None = book.best_ask() if book is not None else None
    best_bid: float | None = bb.price if bb else None
    best_ask: float | None = ba.price if ba else None

    # ── 1. 挂买单（围绕合理价值，在合理价值以下）──
    can_buy = (
        not near_end
        and util < p.ow_inv_mid
        and inp.regime != Regime.REDUCE_ONLY
        and book is not None
        and not book.is_empty
    )
    if can_buy and book is not None:
        scale = 1.0 if util < p.ow_inv_low else 0.5
        scale *= max(0.0, min(1.0, inp.risk_size_scale))

        # 先计算第一层买单价格
        # 1. 先比较 inp.fv 和我们的合理价值 * 1.2，谁小取谁
        # 2. 再和盘口 best_bid 比较，谁小取谁
        first_buy_candidate = min(inp.fv, fv * 1.2)
        if best_bid is not None:
            first_buy_price = min(first_buy_candidate, best_bid)
        else:
            first_buy_price = first_buy_candidate

        # 根据波动率计算每层间隔
        vol = inp.vol_short
        if vol > 0.05:  # 高波动
            vol_gap = 5
        elif vol > 0.02:  # 正常波动
            vol_gap = 3
        else:  # 低波动
            vol_gap = 1

        # 第 1 层：参考盘口 best_bid，确保是挂单
        first_buy_candidate = min(inp.fv, fv * 1.2)
        if best_bid is not None:
            first_buy_price = min(first_buy_candidate, best_bid)
        else:
            first_buy_price = first_buy_candidate

        # 挂 effective_buy_layers 层买单
        prev_price = first_buy_price
        for i in range(effective_buy_layers):
            layer_num = i + 1
            if layer_num == 1:
                price = first_buy_price
            else:
                # 第 2、3、4 层：围绕 our_fv 往下挂
                price = fv - (layer_num - 1) * vol_gap * tick

            # 保护：如果价格 >= 上一层，就用 上一层 - 1 tick，确保不重复
            if layer_num > 1 and price >= prev_price:
                price = prev_price - tick

            prev_price = price

            # 确保价格大于 0
            if price <= 0:
                break

            # 确保价格大于 0
            if price <= 0:
                break

            # 每层的份额（金字塔：越远越少）
            # 保护：如果 layers 比 pyramid_shares 长，就用最后一个权重
            share_index = min(i, len(p.ow_pyramid_shares) - 1)
            usdc = p.base_size_usdc * p.ow_pyramid_shares[share_index] * scale
            shares = math.floor((usdc / max(price, tick)) * 100) / 100

            if shares >= m.min_order_size:
                quotes.append(Quote(tok, Side.BUY, round(price, dec), shares))

    # ── 2. 挂卖单（围绕合理价值，在合理价值以上）──
    if held >= m.min_order_size:
        # 判断成本 vs 合理价值
        cost_per_share = pos.avg_price if hasattr(pos, 'avg_price') else 0.0
        cost_above_fv = cost_per_share > fv  # 买贵了

        # 判断价格在合理价值上方还是下方
        price_above_fv = (best_ask is not None and best_ask > fv)

        # 决定挂几层卖单
        if panic:
            # 恐慌不卖（用户要求）：toxicity 高时观望，不挂卖单
            sell_layer_count = 0
        elif near_end:
            # 接近到期：加速卖，挂全部层
            sell_layer_count = effective_sell_layers
        elif cost_above_fv and not price_above_fv:
            # 买贵了 + 价格在合理价值下方：只挂 1 层在盘口，加速跑
            sell_layer_count = 1
        elif not cost_above_fv and not price_above_fv:
            # 买便宜了 + 价格在合理价值下方：挂多层
            # 第 1 层轻仓挂在盘口 best_ask，其他层参考合理价值往上挂
            sell_layer_count = effective_sell_layers
        else:
            # 正常情况（价格在合理价值上方）：挂全部层
            sell_layer_count = effective_sell_layers

        # 挂卖单
        # 先计算第一层卖单价格
        # 1. 先比较 inp.fv 和 our_fv * 0.8，谁小取谁（D-1: 0.9 -> 0.8，对齐设计文档 13.2）
        # 2. 再和盘口 best_ask 比较，谁大取谁
        first_sell_candidate = min(inp.fv, fv * 0.8)
        if best_ask is not None:
            first_sell_price = max(first_sell_candidate, best_ask)
        else:
            first_sell_price = first_sell_candidate

        # 根据波动率计算每层间隔（和买单一样）
        vol = inp.vol_short
        if vol > 0.05:  # 高波动
            sell_vol_gap = 5
        elif vol > 0.02:  # 正常波动
            sell_vol_gap = 3
        else:  # 低波动
            sell_vol_gap = 1

        prev_price = 0.0
        for i in range(sell_layer_count):
            layer_num = i + 1
            if not price_above_fv and layer_num == 1:
                # 价格在合理价值下方：第 1 层挂在最新卖单位置（盘口）
                # 无论 sell_layer_count 是 1 还是多层，第 1 层都走这里
                if best_ask is not None:
                    price = best_ask
                else:
                    break
            else:
                if layer_num == 1:
                    price = first_sell_price
                else:
                    # D-2：第 2/3/4 层从 our_fv 起算（对齐设计文档 13.2），
                    # 而不是从第 1 层往上叠。
                    price = fv + (layer_num - 1) * sell_vol_gap * tick
                    # 保护：卖单必须严格高于上一层。
                    # 当 best_ask 把第 1 层抬高到 our_fv 之上时，按 our_fv 起算的
                    # 第 2 层可能 <= 第 1 层，会倒挂（卖得比自己第 1 层还便宜）。
                    if price <= prev_price:
                        price = prev_price + sell_vol_gap * tick

            # 确保价格小于 1
            if price >= 1.0:
                break

            # post-only SELL must rest strictly above the best bid
            if best_bid is not None:
                price = max(price, best_bid + tick)

            size = math.floor(held * 100 / sell_layer_count) / 100

            # 墙位置调整：比别人的墙低 1 tick
            if book is not None and size >= m.min_order_size:
                price = _adjust_price_for_wall(book, Side.SELL, price, tick, size, inp.our_asks)

            if size >= m.min_order_size:
                quotes.append(Quote(tok, Side.SELL, round(price, dec), size))

    return TargetQuotes(cid, inp.regime, tuple(quotes))


def _find_wall(book: OrderBook, *, below: float, min_notional: float) -> float | None:
    """First bid level (walking down from the touch) with notional >= min_notional.

    Levels at or above ``below`` are skipped (they'd be too close to our prior
    layer / the touch). Returns the wall's price, or None if no wall qualifies.
    """
    if not book.bids:
        return None
    for price, size in reversed(book.bids.items()):  # high -> low
        if price >= below - _EPS:
            continue
        if price * size >= min_notional:
            return float(price)
    return None


__all__ = ["construct_one_way_quotes"]
