"""Strategy layer unified contract.

A strategy is a pure function: ``StrategyInputs -> TargetQuotes``.
All I/O, clock reads, and state are done by the engine and filled into this
structure; strategies MUST NOT reach global state, perform network I/O, or read
the wall clock (``now`` is supplied by the caller).

The input carries the live :class:`OrderBook` (not just a ``BookView``) because
some strategies (e.g. one_way wall-hunting) need the full ladder. Strategies
that only need top-of-book call ``book.view()`` internally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from polymaker.config import StrategyProfile
    from polymaker.domain import MarketMeta, Position, Regime, TargetQuotes
    from polymaker.marketdata.orderbook import OrderBook


@dataclass(frozen=True, slots=True)
class StrategyInputs:
    """Everything a strategy needs to quote one market at one instant.

    Add fields here only when a genuinely new input is needed by more than one
    strategy; strategies read the fields they care about and ignore the rest.
    """

    # static market metadata
    meta: MarketMeta
    # resolved strategy parameters (MakerProfile | OneWayProfile at runtime)
    profile: StrategyProfile
    # live order books (YES-canonical; None = unavailable / not subscribed)
    yes_book: OrderBook | None
    no_book: OrderBook | None
    # clock & valuation (engine has already computed these)
    now: float
    fv: float                       # YES fair value in (0, 1)
    vol_short: float                # short-horizon realized vol
    toxicity: float                 # markout adverse-selection EWMA in [0, 1]
    # positions
    pos_yes: Position
    pos_no: Position
    # risk throttle in [0, 1] (< 1 => RiskManager wants smaller size)
    risk_size_scale: float = 1.0
    # hours to settlement; None = unknown / placeholder past date
    hours_to_end: float | None = None
    # regime decided once by the engine's RegimeMachine; strategies read-only
    regime: Regime | None = None
    # two-sided strategy exit urgency in [0, 1]; one_way ignores
    yes_exit_urgency: float = 0.0
    no_exit_urgency: float = 0.0


class StrategyFn(Protocol):
    """Signature every strategy must satisfy."""

    def __call__(self, inp: StrategyInputs) -> TargetQuotes: ...
