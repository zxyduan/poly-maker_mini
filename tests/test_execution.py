"""Tests for the rate budgeter and the paper-mode gateway."""

from __future__ import annotations

import asyncio
import time

import pytest

from polymaker.config import Config
from polymaker.domain import MarketMeta, Quote, Side
from polymaker.execution.gateway import ExecutionGateway, _reject_category
from polymaker.execution.ratelimit import TokenBucket


async def test_token_bucket_limits_rate():
    bucket = TokenBucket(rate_per_s=100.0, burst=5.0)
    start = time.monotonic()
    # burst of 5 is instant; the next 5 must wait ~ (5/100)s = 50ms
    for _ in range(10):
        await bucket.acquire(1)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04  # had to wait for refill


async def test_token_bucket_pressure_rises_when_drained():
    bucket = TokenBucket(rate_per_s=10.0, burst=10.0)
    assert bucket.pressure == pytest.approx(0.0, abs=0.01)
    for _ in range(10):
        await bucket.acquire(1)
    assert bucket.pressure > 0.8


async def test_paper_gateway_places_and_cancels_without_wallet(meta):
    cfg = Config()  # defaults, no secrets
    gw = ExecutionGateway(cfg, paper=True)
    quotes = [
        Quote(meta.yes.token_id, Side.BUY, 0.49, 100),
        Quote(meta.no.token_id, Side.BUY, 0.48, 100),
    ]
    placed, rejected = await gw.place(quotes, meta)
    assert len(placed) == 2
    assert rejected == []
    assert all(o.order_id.startswith("paper-") for o in placed)
    # cancel is a no-op in paper mode but must not raise
    await gw.cancel([o.order_id for o in placed])
    assert await gw.open_orders() == []


async def test_paper_gateway_heartbeat_and_cancel_all_noop():
    gw = ExecutionGateway(Config(), paper=True)
    assert await gw.heartbeat() is True  # paper: healthy no-op
    assert gw.heartbeat_failures == 0
    await gw.cancel_all()  # no client, must not raise


def test_gateway_requires_wallet_for_live_connect():
    from polymaker.config import Secrets

    # explicitly-empty secrets (don't read a real .env that may exist on disk)
    cfg = Config(secrets=Secrets(_env_file=None))
    gw = ExecutionGateway(cfg, paper=False)
    with pytest.raises(RuntimeError, match="no wallet"):
        asyncio.run(gw.connect())


# ── place-response rejection classification ────────────────────────────────


def _quote(price: float = 0.5, size: float = 10.0) -> Quote:
    return Quote("tok", Side.BUY, price, size)


def test_reject_category_classifier():
    assert _reject_category("not enough balance / allowance: the balance is not enough -> balance: 0") == "balance"
    assert _reject_category("price must conform to tick size 0.01 with at most 2 decimal places.") == "price"
    assert _reject_category("order price 0.123 is not on the tick grid") == "price"
    assert _reject_category("rate limit exceeded for POST /orders") == "rate"
    assert _reject_category("request throttled, retry later") == "rate"
    assert _reject_category("some unknown server error") == "other"


def test_parse_balance_rejection_returns_category(meta: MarketMeta):
    # The real-world shape seen live: success=true but errorMsg set, no orderID.
    resp = [{"success": True, "errorMsg": "not enough balance / allowance: the balance is not enough -> balance: 0, order amount: 10390000", "orderID": ""}]
    gw = ExecutionGateway(Config(), paper=True)
    placed, rejected = gw._parse_place_response(resp, [_quote()])
    assert placed == []
    assert len(rejected) == 1
    assert rejected[0].category == "balance"
    assert rejected[0].side is Side.BUY


def test_parse_tick_rejection_returns_price(meta: MarketMeta):
    resp = [{"success": False, "errorMsg": "price must conform to tick size 0.01 with at most 2 decimal places.", "orderID": ""}]
    gw = ExecutionGateway(Config(), paper=True)
    placed, rejected = gw._parse_place_response(resp, [_quote()])
    assert placed == []
    assert rejected[0].category == "price"


def test_parse_accepted_and_rejected_mixed(meta: MarketMeta):
    resp = [
        {"success": True, "orderID": "0xabc", "errorMsg": ""},
        {"success": True, "errorMsg": "not enough balance", "orderID": ""},
    ]
    gw = ExecutionGateway(Config(), paper=True)
    placed, rejected = gw._parse_place_response(resp, [_quote(0.5), _quote(0.51)])
    assert len(placed) == 1
    assert placed[0].order_id == "0xabc"
    assert len(rejected) == 1
    assert rejected[0].category == "balance"


def test_parse_missing_id_is_conservative_other(meta: MarketMeta):
    # No id AND no error — treat as 'other' so the engine quarantines+resyncs
    # rather than tracking a phantom order.
    resp = [{"success": True}]
    gw = ExecutionGateway(Config(), paper=True)
    placed, rejected = gw._parse_place_response(resp, [_quote()])
    assert placed == []
    assert rejected[0].category == "other"
