"""Tests for the multi-tag, full-volume scanner (config + gamma + scanner + store)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from polymaker.catalog.gamma import market_outcome_count, parse_json_list
from polymaker.catalog.scanner import (
    NonBinaryRecord,
    ScanConfig,
    export_nonbinary_csv,
    run_scan,
)
from polymaker.catalog.store import CatalogStore
from polymaker.config import ScanSettings

# ── helpers ──────────────────────────────────────────────────────────────────

def _binary_raw(condition_id: str = "0xabc", slug: str = "will-x-win",
                 liquidity: float = 5000.0, accepting: bool = True) -> dict[str, Any]:
    """A minimal binary-market raw dict that parse_market accepts."""
    return {
        "conditionId": condition_id,
        "question": f"Will {slug} happen?",
        "slug": slug,
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "negRisk": False,
        "acceptingOrders": accepting,
        "rewardsMinSize": 10,
        "rewardsMaxSpread": 3.0,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.01, "takerOnly": True, "rebateRate": 0.25},
        "bestBid": 0.48,
        "bestAsk": 0.50,
        "liquidityNum": liquidity,
        "volumeNum": 500000.0,
        "volume24hrClob": 1000.0,
        "endDate": "2028-11-07T00:00:00Z",
        "events": [{"id": 999, "slug": "event-x"}],
    }


def _nonbinary_raw(condition_id: str = "0xmulti", slug: str = "multi-candidate",
                    n_outcomes: int = 3) -> dict[str, Any]:
    """A minimal non-binary market raw dict (n_outcomes outcomes)."""
    tokens = [f"tok-{i}" for i in range(n_outcomes)]
    outcomes = [f"Candidate {i}" for i in range(n_outcomes)]
    return {
        "conditionId": condition_id,
        "question": f"Who will win {slug}?",
        "slug": slug,
        "clobTokenIds": json.dumps(tokens),
        "outcomes": json.dumps(outcomes),
        "orderPriceMinTickSize": 0.01,
        "orderMinSize": 5,
        "negRisk": False,
        "acceptingOrders": True,
        "liquidityNum": 8000.0,
        "volumeNum": 100000.0,
        "volume24hrClob": 500.0,
        "endDate": "2028-11-07T00:00:00Z",
    }


class FakeGammaClient:
    """Drop-in replacement for GammaClient that yields canned markets."""

    def __init__(self, host: str = "https://gamma-api.polymarket.com",
                 timeout: float = 20.0) -> None:
        self.host = host
        self.markets_by_tag: dict[str | None, list[dict[str, Any]]] = {}
        self.tag_ids: dict[str, str] = {}
        self.iter_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeGammaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def aclose(self) -> None:
        pass

    async def resolve_tag_id(self, slug: str) -> str | None:
        return self.tag_ids.get(slug)

    async def iter_markets(self, *, tag_id: str | None = None,
                            related_tags: bool = True, min_liquidity: float = 0.0,
                            min_volume_24hr: float = 0.0, active_only: bool = True,
                            exclude_closed: bool = True, limit: int = 100,
                            max_pages: int = 200) -> Any:
        self.iter_calls.append({
            "tag_id": tag_id, "active_only": active_only,
            "exclude_closed": exclude_closed, "min_liquidity": min_liquidity,
            "min_volume_24hr": min_volume_24hr, "limit": limit, "max_pages": max_pages,
        })
        for m in self.markets_by_tag.get(tag_id, []):
            yield m


@pytest.fixture
def store(tmp_path: Path) -> CatalogStore:
    return CatalogStore(tmp_path / "test.db")


@pytest.fixture
def fake() -> FakeGammaClient:
    f = FakeGammaClient()
    f.tag_ids = {"politics": "tag-1", "sports": "tag-2"}
    return f


@pytest.fixture
def patch_gamma(fake: FakeGammaClient):
    """Patch scanner.GammaClient and fetch_reward_rates with fakes."""
    async def _fake_reward_rates(clob_host: str = "", timeout: float = 20.0) -> dict[str, float]:
        return {}
    with patch("polymaker.catalog.scanner.GammaClient", return_value=fake), \
         patch("polymaker.catalog.scanner.fetch_reward_rates", side_effect=_fake_reward_rates):
        yield


# ── ScanSettings (config layer) ──────────────────────────────────────────────

class TestScanSettings:
    def test_tag_normalization_from_string(self):
        s = ScanSettings(tag_slugs=",politics,,sports, nba ,sports,")
        assert s.tag_slugs == ["politics", "sports", "nba"]

    def test_tag_normalization_from_list(self):
        s = ScanSettings(tag_slugs=["Politics", " sports ", "", "nba"])
        assert s.tag_slugs == ["politics", "sports", "nba"]

    def test_empty_tags_default(self):
        s = ScanSettings()
        assert s.tag_slugs == []

    def test_extra_forbid_raises(self):
        with pytest.raises(ValidationError):
            ScanSettings(not_a_field=True)

    def test_negative_liquidity_rejected(self):
        with pytest.raises(ValidationError):
            ScanSettings(min_liquidity=-1.0)

    def test_to_scan_config_maps_fields(self):
        s = ScanSettings(tag_slugs=["politics", "sports"], rewards_only=False,
                          min_liquidity=100.0, binary_only=False, max_pages=50)
        cfg = s.to_scan_config("https://gamma.example", "https://clob.example")
        assert cfg.tag_slugs == ("politics", "sports")
        assert cfg.rewards_only is False
        assert cfg.min_liquidity == 100.0
        assert cfg.binary_only is False
        assert cfg.max_pages == 50
        assert cfg.gamma_host == "https://gamma.example"
        assert cfg.clob_host == "https://clob.example"

    def test_to_scan_config_overrides(self):
        s = ScanSettings(tag_slugs=["politics"])
        cfg = s.to_scan_config("h", "c", tag_slugs=(), rewards_only=True)
        assert cfg.tag_slugs == ()
        assert cfg.rewards_only is True


# ── gamma helpers ─────────────────────────────────────────────────────────────

class TestGammaHelpers:
    def test_market_outcome_count_binary(self):
        assert market_outcome_count(_binary_raw()) == 2

    def test_market_outcome_count_ternary(self):
        assert market_outcome_count(_nonbinary_raw(n_outcomes=3)) == 3

    def test_market_outcome_count_missing(self):
        assert market_outcome_count({}) is None

    def test_parse_json_list_string(self):
        assert parse_json_list(json.dumps(["a", "b"])) == ["a", "b"]

    def test_parse_json_list_list(self):
        assert parse_json_list(["a"]) == ["a"]

    def test_parse_json_list_none(self):
        assert parse_json_list(None) == []


# ── run_scan core behavior ───────────────────────────────────────────────────

class TestRunScan:
    async def test_empty_tags_full_site(self, store, fake, patch_gamma):
        """tag_slugs=() -> one iter_markets call with tag_id=None."""
        fake.markets_by_tag[None] = [_binary_raw("0x1", "m1")]
        cfg = ScanConfig(tag_slugs=(), binary_only=True)
        result = await run_scan(store, cfg)
        assert len(fake.iter_calls) == 1
        assert fake.iter_calls[0]["tag_id"] is None
        assert len(result.markets) == 1

    async def test_multi_tag_dedup(self, store, fake, patch_gamma):
        """Same condition_id across two tags -> only one ingested market."""
        shared = _binary_raw("0xshared", "shared-market")
        fake.markets_by_tag["tag-1"] = [shared]
        fake.markets_by_tag["tag-2"] = [shared]
        cfg = ScanConfig(tag_slugs=("politics", "sports"), binary_only=True)
        result = await run_scan(store, cfg)
        assert len(result.markets) == 1
        assert store.get("0xshared") is not None
        assert len(fake.iter_calls) == 2

    async def test_unresolvable_tag_skipped(self, store, fake, patch_gamma):
        """A tag that fails to resolve is skipped; other tags still scan."""
        fake.tag_ids = {"politics": "tag-1"}  # "sports" not resolvable
        fake.markets_by_tag["tag-1"] = [_binary_raw("0x1", "m1")]
        cfg = ScanConfig(tag_slugs=("politics", "sports"), binary_only=True)
        result = await run_scan(store, cfg)
        assert len(result.markets) == 1
        assert len(fake.iter_calls) == 1

    async def test_rewards_only_false_keeps_zero(self, store, fake, patch_gamma):
        """rewards_only=false -> market with 0 reward rate is kept."""
        fake.markets_by_tag["tag-1"] = [_binary_raw("0x1", "m1")]
        cfg = ScanConfig(tag_slugs=("politics",), rewards_only=False, binary_only=True)
        result = await run_scan(store, cfg)
        assert len(result.markets) == 1

    async def test_rewards_only_true_drops_zero(self, store, fake, patch_gamma):
        """rewards_only=true -> market with 0 reward rate is dropped."""
        fake.markets_by_tag["tag-1"] = [_binary_raw("0x1", "m1")]
        cfg = ScanConfig(tag_slugs=("politics",), rewards_only=True, binary_only=True)
        result = await run_scan(store, cfg)
        assert len(result.markets) == 0

    async def test_lifecycle_flags_passthrough(self, store, fake, patch_gamma):
        """active_only=false / exclude_closed=false -> params passed to Gamma."""
        fake.markets_by_tag["tag-1"] = [_binary_raw()]
        cfg = ScanConfig(tag_slugs=("politics",), active_only=False,
                         exclude_closed=False, binary_only=True)
        await run_scan(store, cfg)
        call = fake.iter_calls[0]
        assert call["active_only"] is False
        assert call["exclude_closed"] is False

    async def test_all_tags_unresolvable_returns_empty(self, store, fake, patch_gamma):
        """If no tag resolves, scan aborts with empty result."""
        fake.tag_ids = {}
        cfg = ScanConfig(tag_slugs=("politics", "sports"), binary_only=True)
        result = await run_scan(store, cfg)
        assert result.markets == []
        assert result.nonbinary == []
        assert len(fake.iter_calls) == 0

    async def test_min_liquidity_passthrough(self, store, fake, patch_gamma):
        """min_liquidity is passed to iter_markets."""
        fake.markets_by_tag["tag-1"] = [_binary_raw()]
        cfg = ScanConfig(tag_slugs=("politics",), min_liquidity=5000.0, binary_only=True)
        await run_scan(store, cfg)
        assert fake.iter_calls[0]["min_liquidity"] == 5000.0


# ── binary_only routing (requirement 3) ──────────────────────────────────────

class TestBinaryRouting:
    async def test_binary_only_true_skips_nonbinary(self, store, fake, patch_gamma):
        """Default binary_only=true -> non-binary markets are skipped entirely."""
        fake.markets_by_tag["tag-1"] = [_nonbinary_raw()]
        cfg = ScanConfig(tag_slugs=("politics",), binary_only=True)
        result = await run_scan(store, cfg)
        assert result.markets == []
        assert result.nonbinary == []

    async def test_binary_only_false_collects_nonbinary(self, store, fake, patch_gamma):
        """binary_only=false -> non-binary markets go to ScanResult.nonbinary."""
        fake.markets_by_tag["tag-1"] = [_nonbinary_raw("0xmulti", "multi-race")]
        cfg = ScanConfig(tag_slugs=("politics",), binary_only=False)
        result = await run_scan(store, cfg)
        assert result.markets == []
        assert len(result.nonbinary) == 1
        assert result.nonbinary[0].condition_id == "0xmulti"
        assert result.nonbinary[0].num_outcomes == 3
        # NOT ingested into SQLite
        assert store.get("0xmulti") is None

    async def test_binary_only_false_mixed_markets(self, store, fake, patch_gamma):
        """Both binary and non-binary in one sweep -> binary ingested, non-binary to CSV."""
        fake.markets_by_tag["tag-1"] = [
            _binary_raw("0xbin", "binary-market"),
            _nonbinary_raw("0xnb", "nonbinary-market"),
        ]
        cfg = ScanConfig(tag_slugs=("politics",), binary_only=False)
        result = await run_scan(store, cfg)
        assert len(result.markets) == 1
        assert result.markets[0].condition_id == "0xbin"
        assert len(result.nonbinary) == 1
        assert result.nonbinary[0].condition_id == "0xnb"
        assert store.get("0xbin") is not None
        assert store.get("0xnb") is None

    async def test_nonbinary_record_source_tag(self, store, fake, patch_gamma):
        """Non-binary record carries the source_tag of first discovery."""
        fake.markets_by_tag["tag-1"] = [_nonbinary_raw()]
        cfg = ScanConfig(tag_slugs=("politics",), binary_only=False)
        result = await run_scan(store, cfg)
        assert result.nonbinary[0].source_tag == "politics"


# ── NonBinaryRecord.from_raw ─────────────────────────────────────────────────

class TestNonBinaryRecord:
    def test_from_raw_maps_fields(self):
        raw = _nonbinary_raw("0xtest", "test-slug", n_outcomes=4)
        rec = NonBinaryRecord.from_raw(raw, source_tag="sports",
                                        reward_rates={"0xtest": 25.0})
        assert rec.condition_id == "0xtest"
        assert rec.slug == "test-slug"
        assert rec.num_outcomes == 4
        assert rec.source_tag == "sports"
        assert rec.rewards_daily_rate == 25.0
        assert rec.liquidity_num == 8000.0
        assert rec.accepting_orders is True
        parsed = json.loads(rec.outcomes)
        assert len(parsed) == 4

    def test_from_raw_no_reward_rates(self):
        raw = _nonbinary_raw()
        rec = NonBinaryRecord.from_raw(raw, source_tag=None)
        assert rec.rewards_daily_rate == 0.0
        assert rec.source_tag is None


# ── export_nonbinary_csv ─────────────────────────────────────────────────────

class TestExportNonBinaryCsv:
    def test_writes_csv_with_header(self, tmp_path):
        recs = [
            NonBinaryRecord.from_raw(_nonbinary_raw("0x1", "m1"), "politics"),
            NonBinaryRecord.from_raw(_nonbinary_raw("0x2", "m2"), "sports"),
        ]
        path = tmp_path / "nb.csv"
        n = export_nonbinary_csv(recs, path)
        assert n == 2
        content = path.read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 3  # header + 2 rows
        assert "condition_id" in lines[0]
        assert "num_outcomes" in lines[0]
        assert "0x1" in lines[1]
        assert "0x2" in lines[2]

    def test_empty_records_returns_zero(self, tmp_path):
        path = tmp_path / "empty.csv"
        n = export_nonbinary_csv([], path)
        assert n == 0
        assert not path.exists()


# ── CatalogStore.upsert_markets (batch performance) ──────────────────────────

class TestStoreBatchUpsert:
    def test_upsert_markets_inserts_all(self, store):
        metas = [_parse(_binary_raw("0x1", "m1")),
                 _parse(_binary_raw("0x2", "m2")),
                 _parse(_binary_raw("0x3", "m3"))]
        n = store.upsert_markets(metas)
        assert n == 3
        assert store.get("0x1") is not None
        assert store.get("0x2") is not None
        assert store.get("0x3") is not None

    def test_upsert_markets_empty(self, store):
        assert store.upsert_markets([]) == 0

    def test_upsert_markets_idempotent(self, store):
        m = _parse(_binary_raw("0x1", "m1"))
        store.upsert_markets([m])
        store.upsert_markets([m])  # second batch updates, not duplicates
        assert len(store.top(10)) == 1


def _parse(raw: dict[str, Any]):
    """Helper to parse a raw dict into MarketMeta using the real parser."""
    from polymaker.catalog.gamma import parse_market
    m = parse_market(raw, {})
    assert m is not None
    return m
