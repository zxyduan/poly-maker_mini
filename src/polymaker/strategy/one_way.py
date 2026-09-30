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

from polymaker.config import OneWayProfile
from polymaker.domain import Quote, Regime, Side, TargetQuotes
from polymaker.marketdata.orderbook import BookLevel, OrderBook
from polymaker.strategy.base import StrategyInputs

_EPS = 1e-9


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

    near_end = inp.hours_to_end is not None and inp.hours_to_end <= p.ow_exit_days_before * 24.0
    panic = inp.toxicity >= p.ow_panic_toxicity

    bb: BookLevel | None = book.best_bid() if book is not None else None
    ba: BookLevel | None = book.best_ask() if book is not None else None
    best_bid: float | None = bb.price if bb else None
    best_ask: float | None = ba.price if ba else None

    # ── exits: SELL held inventory, passive one tick under the ask ───────
    if held >= m.min_order_size:
        if near_end or panic:
            # into the bid (post just above best_bid = maker near the touch)
            price = (best_bid + tick) if best_bid is not None else (
                (best_ask - tick) if best_ask is not None else None
            )
        else:
            price = (best_ask - tick) if best_ask is not None else None
        # post-only SELL must rest strictly above the best bid. When the spread
        # is a single tick, best_ask - tick == best_bid and would cross through
        # it — floor at best_bid + tick (joins the ask instead of sweeping it).
        if price is not None and best_bid is not None:
            price = max(price, best_bid + tick)
        if price is not None and 0.0 < price < 1.0:
            size = math.floor(held * 100) / 100
            if size >= m.min_order_size:
                quotes.append(Quote(tok, Side.SELL, round(price, dec), size))

    # ── buys: weighted pyramid chained under successive walls ───────────
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
        wall_notional = max(m.volume_24hr * p.ow_wall_pct_of_24h, m.min_order_size * tick)
        # first layer sits below the current touch; each subsequent layer must be
        # strictly cheaper than the previous by >= min_gap_ticks.
        ceiling = (best_bid - p.ow_wall_skip_ticks * tick) if best_bid is not None else 1.0
        prev_price = 1.0
        for w in p.ow_pyramid_shares:
            wall_price = _find_wall(book, below=ceiling, min_notional=wall_notional)
            if wall_price is None:
                break
            price = round(wall_price + p.ow_wall_skip_ticks * tick, dec)
            # monotonic: each layer strictly below the last by the min gap
            if price >= prev_price - p.ow_wall_min_gap_ticks * tick + _EPS:
                break
            usdc = p.base_size_usdc * w * scale
            shares = math.floor((usdc / max(price, tick)) * 100) / 100
            if shares >= m.min_order_size:
                quotes.append(Quote(tok, Side.BUY, price, shares))
            prev_price = price
            ceiling = price - p.ow_wall_min_gap_ticks * tick

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
