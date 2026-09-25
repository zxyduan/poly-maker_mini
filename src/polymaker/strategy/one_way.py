"""单边方向性做市（one_way_mm v0.5）。

只做 BUY YES（小概率长尾方）：贴 touch 极小试探、越往便宜深处量越大的金字塔加仓，
每层锚定大单墙后方便宜 1 tick；成交后以动态 edge 挂 SELL 出货。下行有限，突发事件
YES 暴涨是利好。防阴跌用库存三级刹车，防砸盘用毒性阈值秒撤买单，到期前 N 天只卖不买。

纯函数、无 I/O：所有状态由调用方（engine）传入，直接被单测覆盖。
"""
from __future__ import annotations

import math

from polymaker.config import StrategyProfile
from polymaker.domain import MarketMeta, Position, Quote, Regime, Side, TargetQuotes
from polymaker.logging import get_logger
from polymaker.marketdata.orderbook import OrderBook
from polymaker.strategy.quoting import round_to_tick

log = get_logger("strategy.one_way")

_EPS = 1e-9


def construct_one_way_quotes(
    meta: MarketMeta,
    profile: StrategyProfile,
    yes_book: OrderBook | None,
    no_book: OrderBook | None,  # 保留签名对称；v0.5 只做 YES，不读
    now: float,
    *,
    fv: float,
    vol_short: float,
    toxicity: float,
    pos_yes: Position,
    risk_size_scale: float = 1.0,
    hours_to_end: float | None = None,
) -> TargetQuotes:
    """Produce the BUY-YES pyramid + SELL exit for one market at a point in time."""
    p = profile
    tick = meta.tick_size
    dec = meta.price_decimals
    cid = meta.condition_id
    token_id = meta.yes.token_id  # v0.5 固定只做 YES 小概率方

    # ── 模式判定：到期前 ow_exit_days_before 天 → 只卖不买 ───────────────
    exit_only = (
        hours_to_end is not None
        and hours_to_end <= p.ow_exit_days_before * 24.0
    )

    quotes: list[Quote] = []
    held = pos_yes.size
    wall_notional = meta.volume_24hr * p.ow_wall_pct_of_24h

    # ── 卖单：按 ask 侧墙 + 自己持仓相对大小定位置 ──────────────────────
    #  · ask 墙上量 > 我的持仓 → 小虾米，挂墙后方 1 tick 排队搭便车
    #  · ask 墙上量 ≤ 我的持仓 → 我也是墙，挂 touch 抢跑先出
    #  · ask 侧没墙（盘口稀薄）→ 持仓分 N 档价位出
    #  · exit_only / 砸盘 → 全部贴 touch 紧急出货
    if held >= meta.min_order_size and yes_book is not None and not yes_book.is_empty:
        bb = yes_book.best_bid()
        ba = yes_book.best_ask()
        touch = ba.price if ba is not None else (bb.price + tick if bb else None)
        wall = _find_ask_wall(yes_book, wall_notional)

        if touch is not None:
            floor_price = pos_yes.avg_price + p.ow_edge_base_ticks * tick
            if exit_only or toxicity > p.ow_panic_toxicity:
                # 剧烈波动/到期：紧急出货，贴 best_bid 直接砸出去
                panic_price = bb.price if bb is not None else touch
                prices, fracs = [panic_price], [1.0]
                sell_mode = "panic_sell_bid"
            else:
                # 盘口平静：挂 best_ask - 1tick（比现有卖盘便宜一档，抢跑先成交）
                calm_price = touch - tick
                prices, fracs = [calm_price], [1.0]
                sell_mode = "caml_under_ask"

            for pr, fr in zip(prices, fracs):
                pr = max(pr, floor_price)
                pr = round_to_tick(pr, tick, dec, up=True)
                if bb is not None:
                    pr = max(pr, bb.price + tick)
                sz = math.floor(held * fr * 100) / 100
                if 0 < pr < 1 and sz >= meta.min_order_size:
                    quotes.append(Quote(token_id, Side.SELL, pr, sz))

            log.info("ow_sell", cid=cid[:8], mode=sell_mode, held=held,
                     prices=[round(q.price, 4) for q in quotes if q.side is Side.SELL],
                     sizes=[q.size for q in quotes if q.side is Side.SELL],
                     wall=(wall[0] if wall else None))

    if exit_only:
        log.info("ow_reduce_only", cid=cid[:8], reason="expiry_window", held=held)
        return TargetQuotes(cid, Regime.REDUCE_ONLY, tuple(quotes))

    # ── 买单：金字塔分层（受库存分级 + 砸盘冷静抑制）──────────────────────
    q_max_shares = p.q_max_usdc / max(fv, tick)
    u = (held / q_max_shares) if q_max_shares > 0 else 0.0

    if u >= 1.0:
        buy_scale = 0.0          # 触顶：只减仓
    elif u >= p.ow_inv_mid:
        buy_scale = 0.0          # 收网：不挂新买单
    elif u >= p.ow_inv_low:
        buy_scale = 0.5          # 减速：买单减半
    else:
        buy_scale = 1.0
    if toxicity > p.ow_panic_toxicity:
        buy_scale = 0.0          # 砸盘冷静：不接飞刀
        log.warning("ow_panic", cid=cid[:8], toxicity=round(toxicity, 3),
                    panic=p.ow_panic_toxicity, action="pull_buys")
    buy_scale *= _clamp(risk_size_scale, 0.0, 1.0)

    if buy_scale > 0.0 and yes_book is not None and not yes_book.is_empty:
        bb_level = yes_book.best_bid()
        if bb_level is not None:
            best_bid = bb_level.price
            total_usdc = p.base_size_usdc * buy_scale
            weights = _normalize(p.ow_pyramid_shares)
            # 链式找墙：每层从"上一层墙再往下 min_gap"继续，墙之间保持最小间隔，
            # 避免 4 层全吸到同一堵墙后面。
            search_cap = best_bid
            last_price: float | None = None
            for i, w in enumerate(weights):
                # 第 0 层永远贴 best_bid（最优买价，最容易成交），不找墙；
                # 更深层才锚定墙（墙上方 1 tick 抢跑）。
                # 第1层成交后，第2-4层锁在成交价(avg)下方，不随 best_bid 上移（不追高）
                ref = pos_yes.avg_price if held >= meta.min_order_size else best_bid
                base_price = ref - i * p.layer_step_ticks * tick
                if i == 0:
                    # 空仓挂 best_bid 抢成交；已成交则不再追高，跳过第1层，
                    # 等卖单成交库存回低位再补。
                    if held >= meta.min_order_size:
                        last_price = pos_yes.avg_price # 第1层跳过也要设 last_price=avg，否则第2层没有单调递减约束，会追高挂 best_bid
                        continue
                    price = best_bid
                else:
                    wall = _find_wall_below(yes_book, search_cap, wall_notional)
                    if wall is not None:
                        wp = wall[0]
                        price = min(wp + p.ow_wall_skip_ticks * tick, search_cap)
                        search_cap = wp - p.ow_wall_min_gap_ticks * tick
                    else:
                        price = base_price
                # 单调递减：保证每层一定比上一层便宜（挂买不越挂越贵）
                if last_price is not None:
                    price = min(price, last_price - p.layer_step_ticks * tick)
                price = round_to_tick(price, tick, dec, up=False)
                # 打到价格地板（tick）就停：round 后没真正更低 = 与上一层同价，跳过
                if last_price is not None and price >= last_price - _EPS:
                    continue
                if not (0.0 < price < 1.0):
                    continue
                last_price = price
                layer_usdc = total_usdc * w
                size = layer_usdc / price
                if size < meta.min_order_size:
                    continue
                quotes.append(Quote(token_id, Side.BUY, price, round(size, 2)))

    buys = [q for q in quotes if q.side is Side.BUY]
    # 盘口快照：best_bid/ask + 前 3 档 + 识别到的买侧墙，便于人工核对挂单位置
    bb_lv = yes_book.best_bid() if yes_book is not None else None
    ba_lv = yes_book.best_ask() if yes_book is not None else None
    bid_top3 = list(yes_book.bids.items())[-3:][::-1] if yes_book is not None else []
    ask_top3 = list(yes_book.asks.items())[:3] if yes_book is not None else []
    _bw = (_find_wall_below(yes_book, bb_lv.price, wall_notional)
           if (yes_book and bb_lv is not None) else None)
    bid_wall = _bw[0] if _bw else None
    log.info("ow_quote", cid=cid[:8], held=round(held, 2), inv_util=round(u, 2),
             buy_scale=round(buy_scale, 2),
             bb=bb_lv.price if bb_lv else None,
             ba=ba_lv.price if ba_lv else None,
             bid_top=[(round(p, 4), s) for p, s in bid_top3],
             ask_top=[(round(p, 4), s) for p, s in ask_top3],
             bid_wall=bid_wall,
             wall_notional=round(wall_notional, 2),
             buy_n=len(buys),
             buy_prices=[round(q.price, 4) for q in buys],
             buy_sizes=[q.size for q in buys],
             sell_n=len([q for q in quotes if q.side is Side.SELL]))

    regime_out = Regime.REDUCE_ONLY if u >= 1.0 else Regime.QUIET
    return TargetQuotes(cid, regime_out, tuple(quotes))


# ── helpers ──────────────────────────────────────────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _normalize(weights: list[float]) -> list[float]:
    """Normalize pyramid shares to sum to 1 (defensive; 1:2:3:4 already sums 10)."""
    total = sum(weights)
    if total <= _EPS:
        return [1.0]
    return [w / total for w in weights]


def _find_wall_below(
    book: OrderBook, from_price: float, wall_notional: float
) -> tuple[float, float] | None:
    """Highest bid level at or below `from_price` whose notional (price×size)
    clears `wall_notional`. Returns (price, size) or None."""
    if wall_notional <= 0:
        return None
    for price in reversed(book.bids):
        if price > from_price + _EPS:
            continue
        size = book.bids[price]
        if price * size >= wall_notional:
            return price, size
    return None


def _find_ask_wall(book: OrderBook, min_notional: float) -> tuple[float, float] | None:
    """First ask level (ascending price) whose notional clears min_notional.
    Returns (price, size); None when the ask side is thin."""
    if min_notional <= 0:
        return None
    for price, size in book.asks.items():  # ascending
        if price * size >= min_notional:
            return price, size
    return None
