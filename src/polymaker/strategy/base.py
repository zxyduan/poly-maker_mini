"""Strategy-agnostic input/output contract.

Every quoter (two-sided maker, one-way pyramid, ...) is a pure function:

    (market state, inventory, book ladder, params) -> TargetQuotes

``StrategyInputs`` carries the *full* L2 book (not just a top-of-book view)
because one-way needs to walk the whole bid ladder looking for walls; the
two-sided maker derives its own ``BookView`` from that. The engine dispatches to
the right function via the registry in ``polymaker.strategy`` — it never branches
on the profile type itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from polymaker.domain import MarketMeta, Position, Regime, TargetQuotes

if TYPE_CHECKING:
    from polymaker.config import AnyProfile
    from polymaker.marketdata.orderbook import OrderBook


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """Everything a quoter needs, assembled once per recompute cycle."""

    meta: MarketMeta
    profile: AnyProfile
    yes_book: OrderBook | None
    no_book: OrderBook | None
    now: float
    fv: float  # YES fair value in (0,1)
    vol_short: float
    toxicity: float
    pos_yes: Position
    pos_no: Position
    regime: Regime
    risk_size_scale: float = 1.0  # RiskManager throttle in [0,1]
    yes_exit_urgency: float = 0.0
    no_exit_urgency: float = 0.0
    hours_to_end: float | None = None
    trend_streak: int = 0  # 连续同向桶数（正=涨，负=跌），来自采集器
    our_bids: tuple[float, ...] = ()  # 我们自己挂的买单价格列表
    our_asks: tuple[float, ...] = ()  # 我们自己挂的卖单价格列表


class StrategyFn(Protocol):
    """A quoter: pure, no I/O, returns the desired resting-order set."""

    def __call__(self, inp: StrategyInputs) -> TargetQuotes: ...


__all__ = ["StrategyInputs", "StrategyFn"]
