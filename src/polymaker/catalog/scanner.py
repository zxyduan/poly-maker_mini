"""The scanner: sweep Gamma for markets, score, persist to SQLite.

Replaces the v1 data_updater (hour-long crawl of every order book, written to
Google Sheets). A tag-filtered sweep here is seconds and one process. Multiple
tags are supported; empty tag list = full-site sweep. Non-binary markets are
never ingested (the engine only trades binary YES/NO) but can be exported to a
separate CSV when binary_only=false.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from polymaker.catalog.gamma import (
    GammaClient,
    fetch_reward_rates,
    market_outcome_count,
    parse_json_list,
    parse_market,
)
from polymaker.catalog.store import CatalogStore
from polymaker.domain import MarketMeta
from polymaker.logging import get_logger

log = get_logger("catalog.scanner")


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """All scan-time parameters. Built from ScanSettings.to_scan_config()."""

    # categories: empty tuple = no tag filter = full-site sweep
    tag_slugs: tuple[str, ...] = ()
    related_tags: bool = True
    # filters / thresholds
    rewards_only: bool = False
    min_liquidity: float = 0.0
    min_volume_24hr: float = 0.0
    require_accepting_orders: bool = True
    active_only: bool = True
    exclude_closed: bool = True
    dedup: bool = True
    # binary switch
    binary_only: bool = True
    nonbinary_csv: str = "markets_nonbinary.csv"
    # pagination
    page_size: int = 100
    max_pages: int = 200
    # hosts
    gamma_host: str = "https://gamma-api.polymarket.com"
    clob_host: str = "https://clob.polymarket.com"


@dataclass(frozen=True, slots=True)
class NonBinaryRecord:
    """Lightweight record for non-binary markets (NOT ingested / traded).

    Fields are taken directly from the Gamma raw dict, no binary modeling.
    source_tag marks the first category that discovered this market (dedup keeps
    the first occurrence). Only exported to a separate CSV.
    """

    condition_id: str
    question: str
    slug: str
    num_outcomes: int
    outcomes: str  # JSON array string, e.g. '["Biden","Trump","West"]'
    token_ids: str  # JSON array string
    source_tag: str | None  # first category slug; None for full-site sweep
    accepting_orders: bool
    active: bool
    closed: bool
    neg_risk: bool
    liquidity_num: float
    volume_num: float
    volume_24hr: float
    rewards_daily_rate: float  # from CLOB sampling-markets (0 if not in rewards program)
    rewards_min_size: float
    rewards_max_spread: float
    fees_enabled: bool
    taker_fee_rate: float
    rebate_rate: float
    end_date_iso: str | None
    tick_size: float
    min_order_size: float
    best_bid: float
    best_ask: float

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        source_tag: str | None,
        reward_rates: dict[str, float] | None = None,
    ) -> NonBinaryRecord:
        """Build a NonBinaryRecord from a Gamma raw market dict."""
        fee = raw.get("feeSchedule") or {}
        condition_id = str(raw.get("conditionId", ""))
        rate_map = reward_rates or {}
        return cls(
            condition_id=condition_id,
            question=str(raw.get("question", "")),
            slug=str(raw.get("slug", "")),
            num_outcomes=len(parse_json_list(raw.get("outcomes")) or []),
            outcomes=json.dumps(parse_json_list(raw.get("outcomes")) or []),
            token_ids=json.dumps(parse_json_list(raw.get("clobTokenIds")) or []),
            source_tag=source_tag,
            accepting_orders=bool(raw.get("acceptingOrders", False)),
            active=bool(raw.get("active", False)),
            closed=bool(raw.get("closed", False)),
            neg_risk=bool(raw.get("negRisk", False)),
            liquidity_num=float(raw.get("liquidityNum", 0) or 0),
            volume_num=float(raw.get("volumeNum", 0) or 0),
            volume_24hr=float(raw.get("volume24hrClob") or raw.get("volume24hr") or 0),
            rewards_daily_rate=float(rate_map.get(condition_id, 0.0)),
            rewards_min_size=float(raw.get("rewardsMinSize", 0) or 0),
            rewards_max_spread=float(raw.get("rewardsMaxSpread", 0) or 0),
            fees_enabled=bool(raw.get("feesEnabled", False)),
            taker_fee_rate=float(fee.get("rate", 0.0) or 0.0),
            rebate_rate=float(fee.get("rebateRate", 0.0) or 0.0),
            end_date_iso=raw.get("endDate"),
            tick_size=float(raw.get("orderPriceMinTickSize", 0.001) or 0.001),
            min_order_size=float(raw.get("orderMinSize", 5) or 5),
            best_bid=float(raw.get("bestBid", 0) or 0),
            best_ask=float(raw.get("bestAsk", 0) or 0),
        )


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Result of run_scan: binary markets (ingested) + non-binary records (CSV only)."""

    markets: list[MarketMeta]
    nonbinary: list[NonBinaryRecord]


def export_nonbinary_csv(records: list[NonBinaryRecord], path: str | Path) -> int:
    """Write non-binary market records to a standalone CSV. Returns row count."""
    if not records:
        return 0
    fields = list(asdict(records[0]).keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in records:
            writer.writerow(asdict(r))
    return len(records)


async def _resolve_scopes(
    store: CatalogStore, gamma: GammaClient, slugs: tuple[str, ...]
) -> list[tuple[str | None, str | None]]:
    """Return [(slug, tag_id), ...]; empty slugs -> [(None, None)] (full-site, no tag filter)."""
    if not slugs:
        return [(None, None)]
    scopes: list[tuple[str | None, str | None]] = []
    for slug in slugs:
        tag_id = store.cached_tag(slug) or await gamma.resolve_tag_id(slug)
        if not tag_id:
            log.warning("scan_tag_skipped", slug=slug, reason="resolve_failed")
            continue
        store.cache_tag(slug, tag_id)
        scopes.append((slug, tag_id))
    return scopes


async def run_scan(store: CatalogStore, cfg: ScanConfig) -> ScanResult:
    """Fetch, parse, filter, score, and persist. Returns binary markets + non-binary records."""
    reward_rates = await fetch_reward_rates(cfg.clob_host)
    log.info("reward_rates_loaded", n=len(reward_rates))

    markets: list[MarketMeta] = []
    nonbinary: list[NonBinaryRecord] = []
    seen_cids: set[str] = set()
    seen_total = 0

    async with GammaClient(cfg.gamma_host) as gamma:
        scopes = await _resolve_scopes(store, gamma, cfg.tag_slugs)
        if not scopes:
            log.warning("scan_aborted", reason="no_resolvable_tags", slugs=list(cfg.tag_slugs))
            return ScanResult(markets=[], nonbinary=[])

        for source_slug, tag_id in scopes:
            scope_seen = 0
            async for raw in gamma.iter_markets(
                tag_id=tag_id,
                related_tags=cfg.related_tags,
                min_liquidity=cfg.min_liquidity,
                min_volume_24hr=cfg.min_volume_24hr,
                active_only=cfg.active_only,
                exclude_closed=cfg.exclude_closed,
                limit=cfg.page_size,
                max_pages=cfg.max_pages,
            ):
                scope_seen += 1
                cid = raw.get("conditionId")
                # dedupe overlapping tags (e.g. nba ⊂ sports) by condition_id
                if cfg.dedup and cid and cid in seen_cids:
                    continue
                if cid:
                    seen_cids.add(cid)
                seen_total += 1

                # binary routing: non-binary markets go to separate CSV, never ingested
                n_out = market_outcome_count(raw)
                if n_out is not None and n_out != 2:
                    if not cfg.binary_only:
                        nonbinary.append(NonBinaryRecord.from_raw(raw, source_slug, reward_rates))
                    # binary_only=true: skip (same as legacy behavior)
                    continue

                meta = parse_market(
                    raw,
                    reward_rates,
                    require_accepting=cfg.require_accepting_orders,
                )
                if meta is None:
                    continue
                if cfg.rewards_only and meta.rewards_daily_rate <= 0:
                    continue
                markets.append(meta)

            # warn if we hit the page cap — results may be truncated, not naturally exhausted
            if scope_seen >= cfg.page_size * cfg.max_pages:
                log.warning(
                    "scan_scope_truncated",
                    tag=source_slug,
                    tag_id=tag_id,
                    fetched=scope_seen,
                    hint="raise [scan].max_pages",
                )
            log.info(
                "scan_scope_done",
                tag=source_slug,
                tag_id=tag_id,
                seen=scope_seen,
                markets_so_far=len(markets),
                nonbinary_so_far=len(nonbinary),
            )

    # batch upsert in a single transaction (critical for full-site sweeps)
    store.upsert_markets(markets)
    log.info(
        "scan_complete",
        seen=seen_total,
        markets=len(markets),
        nonbinary=len(nonbinary),
        tags=list(cfg.tag_slugs) or "ALL",
    )
    return ScanResult(markets=markets, nonbinary=nonbinary)
