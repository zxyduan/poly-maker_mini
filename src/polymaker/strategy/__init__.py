"""Strategy registry: name -> pure quoter function.

The engine resolves a profile's ``type`` once at market start and calls the
mapped quoter on every recompute — no ``if p.type ==`` branches in the hot path,
no lazy imports. Add a new strategy by writing a ``construct_*_quotes`` function
matching :class:`polymaker.strategy.base.StrategyFn` and registering it here.
"""

from __future__ import annotations

from polymaker.strategy.base import StrategyFn, StrategyInputs
from polymaker.strategy.one_way import construct_one_way_quotes
from polymaker.strategy.quoting import construct_maker_quotes

STRATEGY_REGISTRY: dict[str, StrategyFn] = {
    "maker": construct_maker_quotes,
    "one_way": construct_one_way_quotes,
}


def get_strategy(name: str) -> StrategyFn:
    """Look up a quoter by profile ``type``. Raises ValueError if unknown."""
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown strategy type: {name!r} (have: {sorted(STRATEGY_REGISTRY)})"
        ) from None


__all__ = [
    "STRATEGY_REGISTRY",
    "StrategyFn",
    "StrategyInputs",
    "get_strategy",
]
