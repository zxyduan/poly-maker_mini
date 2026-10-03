"""Unit tests for soft-error identification in the execution gateway.

Covers ``ExecutionGateway._is_state_lag_rejection`` (per-order error strings)
and ``ExecutionGateway._is_transient_place_error`` (transport-level SDK
exceptions) — the two gates that decide whether a CLOB failure is a
recoverable state-lag / transient condition (resync, no quarantine) or a hard
failure (quarantine / circuit-break).
"""

from __future__ import annotations

import pytest

from polymaker.domain import Side
from polymaker.execution.gateway import ExecutionGateway

# ── 规则 1：sum of matched orders ──────────────────────────────────────────

@pytest.mark.parametrize(
    "err",
    [
        "sum of matched orders: 123",
        "Sum of Matched Orders: 1",
        "error: sum of matched orders: 42 > 0, please try again",
    ],
)
def test_matched_orders_locked_is_soft(err: str) -> None:
    assert ExecutionGateway._is_state_lag_rejection(err, Side.SELL) is True
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is True


def test_matched_orders_zero_is_hard() -> None:
    # N=0 means nothing is locked — not a state-lag rejection.
    assert ExecutionGateway._is_state_lag_rejection(
        "sum of matched orders: 0", Side.SELL
    ) is False


# ── 规则 2：not enough balance — side-dependent ────────────────────────────

def test_sell_not_enough_balance_is_soft() -> None:
    """卖单报余额不足 = 持仓数据滞后（真实 bug 场景）。"""
    err = ("not enough balance / allowance: the balance is not enough "
           "-> balance: 14626, order amount: 56010000")
    assert ExecutionGateway._is_state_lag_rejection(err, Side.SELL) is True


def test_buy_not_enough_balance_is_hard() -> None:
    """买单报余额不足 = 真的没有 pUSD，resync 解决不了。"""
    err = "not enough balance / allowance: pUSD balance too low"
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is False


# ── 规则 3：crosses the book ───────────────────────────────────────────────

def test_crosses_book_is_soft() -> None:
    err = "invalid post-only order: order crosses book"
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is True
    assert ExecutionGateway._is_state_lag_rejection(err, Side.SELL) is True


# ── 规则 4：duplicated ────────────────────────────────────────────────────

def test_duplicated_order_is_soft() -> None:
    err = "order abc123 is invalid. Duplicated."
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is True


# ── 规则 5：order canceled in CTF exchange contract ────────────────────────

def test_order_canceled_in_ctf_is_soft() -> None:
    err = "order abc is canceled in the CTF exchange contract"
    assert ExecutionGateway._is_state_lag_rejection(err, Side.SELL) is True


# ── 规则 6：match delayed due to market conditions ─────────────────────────

def test_match_delayed_is_soft() -> None:
    err = "order match delayed due to market conditions"
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is True


# ── 规则 7：market not yet ready ───────────────────────────────────────────

def test_market_not_ready_is_soft() -> None:
    err = "the market is not yet ready to process new orders"
    assert ExecutionGateway._is_state_lag_rejection(err, Side.BUY) is True


# ── 硬错误：不应被误判为软错误 ──────────────────────────────────────────────

@pytest.mark.parametrize(
    "err,side",
    [
        ("order size 0.3 lower than the minimum: 5", Side.BUY),
        ("order price breaks minimum tick size rule: 0.01", Side.SELL),
        ("invalid expiration", Side.BUY),
        ("unauthorized: invalid api key", Side.BUY),
        ("the order owner has to be the owner of the API KEY", Side.BUY),
        ("address banned", Side.SELL),
        ("FOK orders are filled or killed", Side.BUY),
        ("Too Many Requests", Side.BUY),  # 429 handled by retries, not state-lag
        ("order timed out", Side.BUY),
    ],
)
def test_hard_errors_are_not_soft(err: str, side: Side) -> None:
    assert ExecutionGateway._is_state_lag_rejection(err, side) is False


# ── 传输层瞬时异常：_is_transient_place_error ───────────────────────────────


def test_transient_rate_limit_error() -> None:
    from polymarket.errors import RateLimitError

    exc = RateLimitError("rate limited", retry_after=5.0)
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_transient_restarting_425() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError("engine restarting", status=425, restriction="restarting")
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_transient_cancel_only_503() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError(
        "trading is cancel-only", status=503, restriction="cancel_only"
    )
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_transient_post_only_503() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError(
        "post-only mode", status=503, code="post_only_mode",
        restriction="post_only", retry_after=79.0,
    )
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_transient_server_error_500() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError("internal server error", status=500)
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_transient_network_error() -> None:
    from polymarket.errors import TransportError

    exc = TransportError("connection reset")
    assert ExecutionGateway._is_transient_place_error(exc) is True


def test_non_transient_auth_error_is_hard() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError("unauthorized", status=401)
    assert ExecutionGateway._is_transient_place_error(exc) is False


def test_non_transient_bad_request_is_hard() -> None:
    from polymarket.errors import RequestRejectedError

    exc = RequestRejectedError("invalid order payload", status=400)
    assert ExecutionGateway._is_transient_place_error(exc) is False


def test_plain_exception_is_not_transient() -> None:
    assert ExecutionGateway._is_transient_place_error(RuntimeError("boom")) is False
