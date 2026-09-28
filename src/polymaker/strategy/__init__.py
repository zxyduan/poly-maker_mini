"""Strategy registry.

Adding a new strategy = write a function matching ``StrategyFn`` and register it
here. The engine imports only ``get_strategy`` / ``StrategyInputs`` and never
names a concrete strategy module.
"""
from __future__ import annotations

from polymaker.strategy.base import StrategyFn, StrategyInputs
from polymaker.strategy.one_way import construct_one_way_quotes
from polymaker.strategy.quoting import construct_quotes

STRATEGY_REGISTRY: dict[str, StrategyFn] = {
    "maker": construct_quotes,
    "one_way": construct_one_way_quotes,
}


def get_strategy(name: str) -> StrategyFn:
    """Resolve a strategy function by ``profile.type``.

    Unknown names fail fast at engine startup, not on the first quoter tick.
    """
    try:
        return STRATEGY_REGISTRY[name]
    except KeyError as e:
        raise ValueError(
            f"unknown strategy type {name!r}; "
            f"registered: {sorted(STRATEGY_REGISTRY)}"
        ) from e


__all__ = ["StrategyFn", "StrategyInputs", "STRATEGY_REGISTRY", "get_strategy"]
