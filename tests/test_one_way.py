"""Unit tests for the one-way pyramid accumulator."""

from __future__ import annotations

import pytest

from polymaker.config import OneWayProfile, StrategyProfile
from polymaker.domain import MarketMeta, Position, Regime, Side, TokenMeta
from polymaker.marketdata.orderbook import OrderBook
from polymaker.strategy import get_strategy
from polymaker.strategy.base import StrategyInputs


def _book(bids: list[tuple[float, float]], asks: list[tuple[float, float]],
          tick: float = 0.01) -> OrderBook:
    ob = OrderBook(tick_size=tick)
    ob.apply_snapshot(bids, asks, ts=0.0)
    return ob


@pytest.fixture
def meta() -> MarketMeta:
    return MarketMeta(
        condition_id="0xow",
        question="Will O happen?",
        slug="will-o-happen",
        tokens=(TokenMeta("yes-token", "Yes"), TokenMeta("no-token", "No")),
        tick_size=0.01,
        neg_risk=False,
        min_order_size=5.0,
        rewards_min_size=10.0,
        rewards_max_spread=3.0,
        rewards_daily_rate=50.0,
        maker_fee_bps=0,
        taker_fee_bps=100,
        fees_enabled=True,
        end_date_iso="2028-11-07T00:00:00Z",
        event_id="evt-ow",
        volume_24hr=100_000.0,
    )


def _inputs(meta, profile, *, yes_book=None, no_book=None, pos_yes=None,
            pos_no=None, regime=Regime.QUIET, hours_to_end=None,
            toxicity=0.0, fv=0.5) -> StrategyInputs:
    return StrategyInputs(
        meta=meta, profile=profile, yes_book=yes_book, no_book=no_book,
        now=1000.0, fv=fv, vol_short=0.0, toxicity=toxicity,
        pos_yes=pos_yes or Position("yes-token"),
        pos_no=pos_no or Position("no-token"),
        regime=regime, hours_to_end=hours_to_end,
    )


def test_registry_resolves_one_way_and_rejects_unknown():
    assert get_strategy("one_way").__name__ == "construct_one_way_quotes"
    assert get_strategy("maker").__name__ == "construct_maker_quotes"
    with pytest.raises(ValueError):
        get_strategy("does-not-exist")


def test_event_and_halted_pull_all(meta):
    wall = _book([(0.49, 100), (0.45, 5000)], [(0.51, 500)])
    for regime in (Regime.EVENT, Regime.HALTED):
        tq = get_strategy("one_way")(_inputs(
            meta, OneWayProfile(), yes_book=wall, regime=regime,
        ))
        assert tq.is_empty


def test_flat_accumulator_posts_buy_under_wall(meta):
    # touch bid 0.49 (thin), wall at 0.45 (5000 => notional 2250 > 500)
    book = _book([(0.49, 100), (0.45, 5000)], [(0.51, 500)])
    tq = get_strategy("one_way")(_inputs(meta, OneWayProfile(), yes_book=book))
    buys = [q for q in tq.quotes if q.side == Side.BUY and q.token_id == "yes-token"]
    assert buys, "expected at least one pyramid buy resting under the wall"
    # rests one tick above the 0.45 wall
    assert max(q.price for q in buys) <= 0.46 + 1e-9


def test_holding_emits_sell_under_ask(meta):
    book = _book([(0.49, 100), (0.45, 5000)], [(0.51, 500)])
    tq = get_strategy("one_way")(_inputs(
        meta, OneWayProfile(), yes_book=book,
        pos_yes=Position("yes-token", 100, 0.40),
    ))
    sells = [q for q in tq.quotes if q.side == Side.SELL and q.token_id == "yes-token"]
    assert sells
    # passive exit one tick under the ask (0.51 -> 0.50), never crosses
    assert sells[0].price == pytest.approx(0.50)


def test_high_inventory_stops_buying(meta):
    book = _book([(0.49, 100), (0.45, 5000)], [(0.51, 500)])
    # q_max_shares = 500/0.5 = 1000; hold 800 => util 0.8 >= inv_mid
    tq = get_strategy("one_way")(_inputs(
        meta, OneWayProfile(), yes_book=book,
        pos_yes=Position("yes-token", 800, 0.40),
    ))
    assert not [q for q in tq.quotes if q.side == Side.BUY]
    assert [q for q in tq.quotes if q.side == Side.SELL]


def test_near_end_is_sell_only(meta):
    book = _book([(0.49, 100), (0.45, 5000)], [(0.51, 500)])
    tq = get_strategy("one_way")(_inputs(
        meta, OneWayProfile(), yes_book=book,
        pos_yes=Position("yes-token", 100, 0.40),
        hours_to_end=10.0,  # 10h to end, well inside the 30d window
    ))
    assert not [q for q in tq.quotes if q.side == Side.BUY]
    assert [q for q in tq.quotes if q.side == Side.SELL]


def test_profile_type_discriminator():
    assert StrategyProfile().type == "maker"
    assert OneWayProfile().type == "one_way"


def test_exit_sell_never_crosses_one_tick_spread(meta):
    # spread == 1 tick: best_ask - tick == best_bid; the sell must floor at
    # best_bid + tick (= best_ask) instead of landing on the bid and crossing.
    book = _book([(0.033, 200)], [(0.034, 200)])
    tq = get_strategy("one_way")(_inputs(
        meta, OneWayProfile(), yes_book=book,
        pos_yes=Position("yes-token", 100, 0.03),
    ))
    sells = [q for q in tq.quotes if q.side == Side.SELL]
    assert sells, "expected a sell exit"
    # post-only sell must be strictly above best_bid (0.033)
    assert sells[0].price > 0.033 + 1e-9
