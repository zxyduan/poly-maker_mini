"""The scanner: sweep Gamma for markets, score, persist to SQLite.

Replaces the v1 data_updater (hour-long crawl of every order book, written to
Google Sheets). A tag-filtered sweep here is seconds and one process. Tags are
configurable (``[scan].tag_slugs``); an empty tuple means "scan the whole site".
"""

from __future__ import annotations

from dataclasses import dataclass

from polymaker.catalog.gamma import (
    GammaClient,
    fetch_reward_rates,
    parse_market,
)
from polymaker.catalog.scoring import score_market
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta
from polymaker.logging import get_logger

log = get_logger("catalog.scanner")

# Preserved as the historical default so existing configs keep scanning politics;
# set `tag_slugs = []` explicitly to scan the whole site.
DEFAULT_TAG_SLUGS: tuple[str, ...] = ("politics",)


@dataclass(frozen=True, slots=True)
class ScanConfig:
    tag_slugs: tuple[str, ...] = DEFAULT_TAG_SLUGS  # empty = no tag filter (whole site)
    min_liquidity: float = 1000.0
    min_volume_24hr: float = 0.0
    rewards_only: bool = True  # keep only markets in the liquidity-rewards program
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"


async def run_scan(store: CatalogStore, cfg: ScanConfig) -> list[MarketMeta]:
    """Fetch, parse, filter, score, and persist. Returns the kept markets.

    Iterates every configured tag (deduplicating overlaps by condition_id); an
    empty ``tag_slugs`` is a single unfiltered sweep.
    """
    reward_rates = await fetch_reward_rates(cfg.clob_host)
    log.info("reward_rates_loaded", n=len(reward_rates), tags=list(cfg.tag_slugs))

    kept: list[MarketMeta] = []
    seen_cids: set[str] = set()
    async with GammaClient(cfg.gamma_host) as gamma:
        # Resolve one tag_id per slug (cached in the store), or None for the
        # unfiltered whole-site sweep.
        tag_slugs = cfg.tag_slugs or (None,)
        for slug in tag_slugs:
            tag_id: str | None = None
            if slug is not None:
                tag_id = store.cached_tag(slug) or await gamma.resolve_tag_id(slug)
                if tag_id:
                    store.cache_tag(slug, tag_id)

            seen = 0
            async for raw in gamma.iter_markets(
                tag_id=tag_id,
                min_liquidity=cfg.min_liquidity,
                min_volume_24hr=cfg.min_volume_24hr,
            ):
                seen += 1
                cid = raw.get("conditionId")
                if cid and cid in seen_cids:
                    continue  # overlap across tags — keep the first copy
                meta = parse_market(raw, reward_rates)
                if meta is None:
                    continue
                if cfg.rewards_only and meta.rewards_daily_rate <= 0:
                    continue
                seen_cids.add(meta.condition_id)
                kept.append(meta)
            log.info("scan_tag_done", tag=slug, seen=seen, kept_so_far=len(kept))

    for m in kept:
        store.upsert_market(m, score_market(m))
    log.info("scan_complete", kept=len(kept), tags=list(cfg.tag_slugs))
    return kept
