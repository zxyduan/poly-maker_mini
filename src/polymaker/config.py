"""Configuration: pydantic models over local TOML files + .env secrets.

Replaces the v1 Google Sheets config entirely. Three files under config/:
  config.toml    engine/wallet/risk/execution settings
  strategy.toml  named parameter profiles
  markets.toml   the trade list (market -> profile + overrides)

Secrets (private key, wallet address) come only from the environment / .env.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScanSettings(BaseModel):
    """The ``[scan]`` section: which Gamma tags the market sweep covers.

    ``tag_slugs`` accepts either a TOML array (``["politics","sports"]``) or a
    comma-separated string (``"politics, sports"``). An empty list means
    "scan the whole site" (no tag filter). The default keeps the historical
    politics-only behavior so existing configs are unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    tag_slugs: tuple[str, ...] = ("politics",)
    min_liquidity: float = 1000.0
    min_volume_24hr: float = 0.0
    rewards_only: bool = True

    @field_validator("tag_slugs", mode="before")
    @classmethod
    def _normalize_tag_slugs(cls, v: Any) -> tuple[str, ...]:
        if v is None:
            return ()
        if isinstance(v, str):
            return tuple(s.strip() for s in v.split(",") if s.strip())
        return tuple(str(s).strip() for s in v if str(s).strip())


class WalletConfig(BaseModel):
    chain_id: int = 137
    signature_type: int = 2
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    polygon_rpc: str = "https://polygon-bor-rpc.publicnode.com"


class EngineConfig(BaseModel):
    debounce_ms: int = 200
    # baseline periodic re-quote (book reactions are event-driven & instant; this
    # is just a slow refresh for cool-off re-entry / exit-urgency updates). A
    # precise wake is also scheduled for the exact moment an EVENT cool-off ends.
    quoter_tick_s: float = 60.0
    reconcile_interval_s: float = 30.0
    catalog_refresh_s: float = 900.0
    heartbeat: bool = True
    heartbeat_interval_s: float = 5.0
    journal: bool = True
    loop: str = "uvloop"


class RiskConfig(BaseModel):
    max_total_exposure_usdc: float = 5000.0
    max_event_group_loss_usdc: float = 1000.0
    max_market_notional_usdc: float = 800.0
    daily_loss_kill_usdc: float = 250.0
    ws_stale_halt_s: float = 10.0
    # user WS down this long -> we can't see our fills -> pull all quotes
    user_ws_blind_halt_s: float = 15.0
    # consecutive heartbeat failures -> exchange is auto-cancelling us -> halt
    heartbeat_halt_failures: int = 3
    max_order_error_rate: float = 0.25


class ExecutionConfig(BaseModel):
    rate_budget_fraction: float = 0.25
    post_only: bool = True
    max_orders_per_batch: int = 15


class PathsConfig(BaseModel):
    db: str = "state.db"
    journal_dir: str = "journal"
    log_dir: str = "logs"


class StrategyProfile(BaseModel):
    """The two-sided maker parameter set.

    Subclassed by :class:`OneWayProfile`; ``type`` is the discriminator the
    strategy registry uses to pick a quoter. Defaults reproduce the historical
    maker behavior exactly.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["maker"] = "maker"

    # fair value
    micro_levels: int = 3
    flow_ewma_halflife_s: float = 120.0
    # spread / skew
    gamma: float = 0.5
    delta_min_ticks: int = 2
    c_vol: float = 1.2
    c_tox: float = 2.0
    # vol horizons
    vol_short_halflife_s: float = 10.0
    vol_long_halflife_s: float = 900.0
    # sizing / inventory
    base_size_usdc: float = 50.0
    q_max_usdc: float = 500.0
    q_soft_frac: float = 0.6
    layers: int = 2
    layer_step_ticks: int = 2
    # multiplier on the market's reward min-size that reward-eligible orders are
    # bumped to (margin above the scoring floor). 1.5 => 100-share min -> 150.
    reward_size_mult: float = 1.0
    # placement / churn
    reprice_ticks: int = 2
    resize_frac: float = 0.15
    min_edge_ticks: int = 1
    # regime
    event_cooloff_s: float = 60.0
    event_jump_ticks: int = 8
    event_sweep_levels: int = 3
    # sweep = a print >= event_sweep_mult order-sizes AND >= event_sweep_frac of
    # the near-touch depth it consumed (both must hold to flag a toxic sweep)
    event_sweep_mult: float = 4.0
    event_sweep_frac: float = 0.8
    trend_flow_z: float = 1.5
    # short/long realized-vol ratio that trips TRENDING (half size). On a thin
    # book microprice jitter inflates this without real trade flow, so raise it
    # for reward-farming markets that trade rarely.
    trend_vol_ratio: float = 2.0
    # lifecycle
    end_date_taper_days: float = 7.0
    reduce_only_hours: float = 24.0
    halt_before_hours: float = 2.0
    # exits
    exit_urgency_s: float = 900.0
    merge_min_size: float = 20.0

    def with_overrides(self, overrides: dict[str, Any]) -> StrategyProfile:
        """Return a copy with per-market override values applied.

        Uses ``model_copy(update=...)`` so the concrete subclass (maker vs
        one_way) is preserved — a ``model_dump()`` round-trip would erase it.
        """
        if not overrides:
            return self
        allowed = {k: v for k, v in overrides.items() if k in self.model_fields}
        return self.model_copy(update=allowed)


class OneWayProfile(StrategyProfile):
    """One-way pyramid accumulator: rest bids under book walls on a single side.

    On EVENT/HALTED all quotes pull; sells rest one tick under the ask (or dump
    to the bid when toxic / near expiry); buys fan out as a weighted pyramid that
    chains down successive walls, throttled by inventory utilization.
    """

    type: Literal["one_way"] = "one_way"  # type: ignore[assignment]  # narrows base discriminator

    ow_side: Literal["yes", "no"] = "yes"

    # ── 合理价值（Fair Value）配置 ──
    # 初始合理价值（比如 0.05 = 5¢）
    fv_initial: float = 0.05
    # 起始日期（ISO 格式，比如 "2026-01-01"）：fv_initial 是这一天的合理价值
    fv_start_date: str = ""
    # 到期日期（ISO 格式，比如 "2026-12-31"）：合理价值按天线性衰减，到期归零
    fv_end_date: str = ""
    # 衰减公式：fv_now = fv_initial * (fv_end_date - now) / (fv_end_date - fv_start_date)
    # fv_start_date 留空则不衰减（fv_now 永远 = fv_initial）

    # ── 分层挂单配置 ──
    # 买几层（第 2、3、4 层都在合理价值以下）
    buy_layers: int = 3
    # 卖几层（都在合理价值以上）
    sell_layers: int = 4
    # 每层间隔的 tick 数（会根据波动率自动调整）
    base_layer_gap_ticks: int = 3

    # pyramid weights summing to 1 across buy layers (fraction of base_size)
    ow_pyramid_shares: list[float] = [0.10, 0.20, 0.30, 0.40]
    # a wall = a bid level whose notional (price*size) >= 24h volume * this
    ow_wall_pct_of_24h: float = 0.005
    # rest this many ticks above the discovered wall
    ow_wall_skip_ticks: int = 1
    # require this many ticks of separation between pyramid layers
    ow_wall_min_gap_ticks: int = 5
    ow_edge_base_ticks: int = 5
    # inventory brakes on held / q_max_shares
    ow_inv_low: float = 0.33  # below: full size
    ow_inv_mid: float = 0.50  # above: stop buying entirely
    # toxicity >= this => exit into the bid instead of resting under the ask
    ow_panic_toxicity: float = 0.6
    # within this many days of end: sell only, no new buys
    ow_exit_days_before: float = 30.0


# Union alias used by the loader + type annotations.
AnyProfile = StrategyProfile | OneWayProfile


class AdaptiveConfig(BaseModel):
    """One-way 动态参数配置。"""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    recalc_interval_s: int = 7200          # 2 小时重算一次系数
    hysteresis_pct: float = 0.2           # 滞回缓冲区 20%
    # 数据不足时的保守系数
    conservative_vol_factor: float = 0.3
    conservative_vol_regime_factor: float = 1.5
    # 过渡模式（数据不足 min_buckets_normal 时）
    transition_vol_factor: float = 0.5
    transition_vol_regime_factor: float = 1.2
    # 新市场策略：True = 完全不挂买单，False = 保守参数试水
    new_market_no_order: bool = False
    # 最少几个桶才算正常模式
    min_buckets_normal: int = 6
    # 系数阈值（可以调）
    vol_thresholds: tuple[float, float, float] = (5.0, 20.0, 100.0)


# Keys allowed on a market entry that are NOT profile overrides.
_MARKET_RESERVED = {"slug", "condition_id", "profile", "enabled"}


class MarketEntry(BaseModel):
    """One line of the trade list. Extra keys are treated as profile overrides."""

    model_config = ConfigDict(extra="allow")

    slug: str | None = None
    condition_id: str | None = None
    profile: str = "political-longdated"
    enabled: bool = True

    @model_validator(mode="after")
    def _need_identifier(self) -> MarketEntry:
        if not self.slug and not self.condition_id:
            raise ValueError("market entry needs a slug or condition_id")
        return self

    @property
    def overrides(self) -> dict[str, Any]:
        extra = self.model_extra or {}
        return {k: v for k, v in extra.items() if k not in _MARKET_RESERVED}

    @property
    def ref(self) -> str:
        return self.slug or self.condition_id or "?"


class Secrets(BaseSettings):
    """Loaded from environment / .env. Never written to disk by us."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    pk: str = Field(default="", alias="PK")
    browser_address: str = Field(default="", alias="BROWSER_ADDRESS")
    polygon_rpc: str | None = Field(default=None, alias="POLYGON_RPC")
    alert_webhook_url: str | None = Field(default=None, alias="ALERT_WEBHOOK_URL")
    # Polymarket builder API creds (self-generated via L2 auth: clob.create_builder_api_key)
    # + relayer URL — needed to merge a V2 DepositWallet (sig_type 1/3), whose execute()
    # only accepts calls from its factory/relayer. See merge.py.
    builder_key: str | None = Field(default=None, alias="POLY_BUILDER_KEY")
    builder_secret: str | None = Field(default=None, alias="POLY_BUILDER_SECRET")
    builder_passphrase: str | None = Field(default=None, alias="POLY_BUILDER_PASSPHRASE")
    relayer_url: str = Field(default="https://relayer-v2.polymarket.com", alias="POLY_RELAYER_URL")

    @property
    def has_wallet(self) -> bool:
        return bool(self.pk and self.browser_address)

    @property
    def has_builder_creds(self) -> bool:
        return bool(self.builder_key and self.builder_secret and self.builder_passphrase)


class Config(BaseModel):
    """Fully-resolved configuration tree."""

    wallet: WalletConfig = WalletConfig()
    engine: EngineConfig = EngineConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paths: PathsConfig = PathsConfig()
    scan: ScanSettings = ScanSettings()
    profiles: dict[str, StrategyProfile] = {}
    markets: list[MarketEntry] = []
    one_way_adaptive: AdaptiveConfig = AdaptiveConfig()
    secrets: Secrets = Field(default_factory=Secrets)
    config_dir: Path = Path("config")

    @property
    def proxy(self) -> str | None:
        # Standard proxy env var; ALL_PROXY lets you route through an SSH tunnel
        # (e.g. simulate colocation during local testing). httpx and web3 honor
        # it automatically once load_dotenv() has run.
        return os.environ.get("ALL_PROXY") or os.environ.get("HTTPS_PROXY")

    @property
    def enabled_markets(self) -> list[MarketEntry]:
        return [m for m in self.markets if m.enabled]

    def profile_for(self, entry: MarketEntry) -> StrategyProfile:
        base = self.profiles.get(entry.profile)
        if base is None:
            raise KeyError(f"unknown strategy profile: {entry.profile!r}")
        return base.with_overrides(entry.overrides)

    @classmethod
    def load(cls, config_dir: str | Path = "config", *, load_env: bool = True) -> Config:
        cdir = Path(config_dir)
        if load_env:
            load_dotenv()
        main = _read_toml(cdir / "config.toml")
        strat = _read_toml(cdir / "strategy.toml")
        mkts = _read_toml(cdir / "markets.toml")

        # A profile with only maker fields -> StrategyProfile (first union member).
        # One with `type="one_way"` / ow_* knobs falls through to OneWayProfile.
        profile_adapter: TypeAdapter[AnyProfile] = TypeAdapter(StrategyProfile | OneWayProfile)
        profiles = {
            name: profile_adapter.validate_python(params)
            for name, params in (strat.get("profiles") or {}).items()
        }
        markets = [MarketEntry(**m) for m in (mkts.get("markets") or [])]

        # 动态参数配置（可选，用默认值）
        adaptive = AdaptiveConfig(**(strat.get("one_way_adaptive") or {}))

        return cls(
            wallet=WalletConfig(**main.get("wallet", {})),
            engine=EngineConfig(**main.get("engine", {})),
            risk=RiskConfig(**main.get("risk", {})),
            execution=ExecutionConfig(**main.get("execution", {})),
            paths=PathsConfig(**main.get("paths", {})),
            scan=ScanSettings(**main.get("scan", {})),
            profiles=profiles,
            markets=markets,
            one_way_adaptive=adaptive,
            secrets=Secrets(),
            config_dir=cdir,
        )

    def reload_markets(self) -> Config:
        """Re-read markets.toml only (used by the hot-reload path)."""
        mkts = _read_toml(self.config_dir / "markets.toml")
        self.markets = [MarketEntry(**m) for m in (mkts.get("markets") or [])]
        return self


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)
