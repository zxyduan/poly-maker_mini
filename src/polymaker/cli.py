"""polymaker command-line interface.

  polymaker scan                 sweep Gamma for markets -> SQLite (tags/filters from [scan])
  polymaker markets              rank/browse the catalog
  polymaker markets-add <slug>   append a market to config/markets.toml
  polymaker status               positions / open orders / PnL (reads SQLite)
  polymaker doctor               preflight: wallet auth, balances, WS reachability
  polymaker run [--paper]        start the market maker
  polymaker cancel-all           panic button
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from polymaker import __version__
from polymaker.config import Config

app = typer.Typer(
    name="polymaker",
    help="Maker-only market maker for Polymarket CLOB V2.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the polymaker version."""
    console.print(f"polymaker {__version__}")


@app.command()
def scan(
    config_dir: str = typer.Option("config", help="config directory"),
    min_liquidity: float | None = typer.Option(None, help="override [scan].min_liquidity"),
    tag: list[str] = typer.Option(None, "--tag", help="override categories; repeatable; pass 'all' for full site"),  # noqa: B008
    binary_only: bool | None = typer.Option(None, "--binary-only/--no-binary-only",
                                             help="override [scan].binary_only"),
    all_markets: bool = typer.Option(False, "--all", help="force include non-rewards markets"),
) -> None:
    """Sweep Gamma for markets (tags/filters from [scan] config), score, persist."""
    import dataclasses

    from polymaker.catalog.scanner import ScanResult, export_nonbinary_csv, run_scan
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)

    overrides: dict[str, object] = {}
    if min_liquidity is not None:
        overrides["min_liquidity"] = min_liquidity
    if tag:
        slugs: tuple[str, ...] = () if [t.lower() for t in tag] == ["all"] else tuple(tag)
        overrides["tag_slugs"] = slugs
    if binary_only is not None:
        overrides["binary_only"] = binary_only

    async def _go() -> ScanResult:
        scan_cfg = cfg.scan.to_scan_config(cfg.wallet.gamma_host, cfg.wallet.clob_host, **overrides)
        if all_markets:
            scan_cfg = dataclasses.replace(scan_cfg, rewards_only=False)
        return await run_scan(store, scan_cfg)

    result = asyncio.run(_go())

    # binary markets -> SQLite (upserted inside run_scan) + markets.csv
    csv_path = Path(config_dir).parent / "markets.csv"
    written = store.export_csv(csv_path, limit=cfg.scan.export_limit)

    # non-binary markets -> standalone CSV (never ingested into SQLite)
    nb_path = Path(config_dir).parent / cfg.scan.nonbinary_csv
    nb_written = export_nonbinary_csv(result.nonbinary, nb_path) if result.nonbinary else 0

    console.print(
        f"[green]Scanned and stored {len(result.markets)} binary markets.[/green] "
        f"Wrote [bold]{csv_path}[/bold] ({written} rows)."
    )
    if result.nonbinary:
        console.print(
            f"[yellow]Also found {len(result.nonbinary)} non-binary markets -> "
            f"[bold]{nb_path}[/bold] ({nb_written} rows, not traded by the engine).[/yellow]"
        )
    console.print("Pick markets, then `polymaker markets-add <slug>`.")
    store.close()


@app.command()
def markets(
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(25, help="rows to show"),
) -> None:
    """Show the top scored markets from the catalog."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    rows = store.top(limit)
    if not rows:
        console.print("[yellow]Catalog empty. Run `polymaker scan` first.[/yellow]")
        raise typer.Exit()

    table = Table(title="Markets by score")
    for col in ("score", "reward/day", "rebate/day", "spread", "tick", "neg", "question"):
        table.add_column(col, justify="right" if col != "question" else "left")
    for meta, sc in rows:
        table.add_row(
            f"{sc.score:.2f}", f"{meta.rewards_daily_rate:.0f}", f"{sc.rebate_potential:.0f}",
            f"{sc.spread:.3f}", f"{meta.tick_size:g}", "Y" if meta.neg_risk else "-",
            meta.question[:60],
        )
    console.print(table)
    console.print("\nAdd one with: [bold]polymaker markets-add <slug>[/bold]  (slugs are in the catalog)")


@app.command(name="markets-add")
def markets_add(
    slug: str,
    profile: str = typer.Option("political-longdated", help="strategy profile"),
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """Append a market (by slug) to config/markets.toml."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    meta = store.get_by_slug(slug)
    store.close()
    if meta is None:
        console.print(f"[red]No market with slug {slug!r} in the catalog. Run `polymaker scan`.[/red]")
        raise typer.Exit(1)

    path = Path(config_dir) / "markets.toml"
    block = f'\n[[markets]]\nslug    = "{slug}"\nprofile = "{profile}"\nenabled = true\n'
    with path.open("a") as fh:
        fh.write(block)
    console.print(f"[green]Added[/green] {meta.question[:60]!r} to {path}")


@app.command()
def status(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show positions, open orders, and marks from the local state DB."""
    from polymaker.state.store import StateStore

    cfg = Config.load(config_dir)
    store = StateStore(cfg.paths.db)
    snap = store.snapshot()
    console.print(f"[bold]Open orders:[/bold] {snap['open_orders']}")
    positions: dict[str, Any] = snap["positions"]  # type: ignore[assignment]
    if not positions:
        console.print("[dim]No open positions.[/dim]")
    else:
        table = Table(title="Positions")
        table.add_column("token")
        table.add_column("size", justify="right")
        table.add_column("avg", justify="right")
        for tok, p in positions.items():
            table.add_row(tok[:16] + "…", f"{p['size']:.2f}", f"{p['avg_price']:.3f}")
        console.print(table)
    store.close()


@app.command()
def pnl(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show PnL from the recorded snapshots (equity, daily PnL, fills)."""
    import sqlite3

    cfg = Config.load(config_dir)
    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, equity, net_cash, inventory_value, daily_pnl FROM pnl_snapshots "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchall()
    if not rows:
        console.print("[yellow]No PnL snapshots yet (run the engine first).[/yellow]")
    else:
        r = rows[0]
        color = "green" if r["daily_pnl"] >= 0 else "red"
        console.print(f"[bold]equity:[/bold] {r['equity']:.4f}  "
                      f"[bold]inventory:[/bold] {r['inventory_value']:.4f}  "
                      f"[bold]net cash:[/bold] {r['net_cash']:.4f}")
        console.print(f"[bold]daily PnL:[/bold] [{color}]{r['daily_pnl']:+.4f}[/{color}] pUSD")
    nfills = conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
    console.print(f"[dim]total fills recorded: {nfills}[/dim]")
    conn.close()


@app.command(name="export-csv")
def export_csv(
    config_dir: str = typer.Option("config", help="config directory"),
    out: str = typer.Option("markets.csv", help="output CSV path"),
    limit: int = typer.Option(None, help="max rows (default: [scan].export_limit)"),
) -> None:
    """Export the scored market catalog to a CSV for easy picking."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    n = store.export_csv(out, limit if limit is not None else cfg.scan.export_limit)
    store.close()
    console.print(f"[green]Wrote {n} markets to {out}.[/green]")


@app.command()
def doctor(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Preflight checks: config, wallet auth, balance/allowance, WS reachability."""
    from polymaker.doctor import run_doctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_doctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command()
def run(
    config_dir: str = typer.Option("config", help="config directory"),
    paper: bool = typer.Option(False, "--paper", help="paper mode: full pipeline, no orders posted"),
) -> None:
    """Start the market maker."""
    from polymaker.engine import Engine
    from polymaker.logging import configure

    cfg = Config.load(config_dir)
    configure(json_file=Path(cfg.paths.log_dir) / ("paper.jsonl" if paper else "live.jsonl"))
    if cfg.engine.loop == "uvloop":
        try:
            import uvloop

            uvloop.install()
        except Exception:  # noqa: BLE001
            pass

    engine = Engine(cfg, paper=paper)

    async def _go() -> None:
        try:
            await engine.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await engine.shutdown()

    console.print(f"[bold green]Starting polymaker[/bold green] ({'PAPER' if paper else 'LIVE'})…")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


@app.command()
def livetest(
    config_dir: str = typer.Option("config", help="config directory"),
    notional: float = typer.Option(5.0, help="order notional in USDC"),
) -> None:
    """Live wallet round-trip: place a deep post-only order and cancel it (~$5)."""
    from polymaker.livetest import run_livetest

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_livetest(cfg, console, notional))
    raise typer.Exit(0 if ok else 1)


@app.command()
def moneydoctor(
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """LIVE trading self-test: rest a limit, then market buy + sell (spends a little)."""
    from polymaker.moneydoctor import run_moneydoctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_moneydoctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command(name="cancel-all")
def cancel_all(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Cancel all open orders for the wallet (panic button)."""
    from polymaker.execution.gateway import ExecutionGateway

    cfg = Config.load(config_dir)
    gw = ExecutionGateway(cfg)

    async def _go() -> None:
        await gw.connect()
        await gw.cancel_all()

    asyncio.run(_go())
    console.print("[green]Sent cancel-all.[/green]")

@app.command()
def books(
    config_dir: str = typer.Option("config", help="config directory"),
    out: str = typer.Option("books.csv", help="output CSV path"),
) -> None:
    """Snapshot order books in markets.csv format, append one row per market."""
    import csv
    import os
    from datetime import datetime, timezone
    from polymaker.catalog.store import CatalogStore
    from polymaker.catalog.gamma import GammaClient, fetch_reward_rates, parse_market
    from polymaker.catalog.scoring import score_market
    from polymaker.execution.gateway import ExecutionGateway
    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    fields = [
        "snapshot_ts", "score", "reward_pool_per_day", "rebate_pool_per_day",
        "spread", "best_bid", "best_ask", "tick", "min_size", "neg_risk",
        "taker_fee_pct", "rebate_pct", "rewards_max_spread", "liquidity",
        "volume_24h", "end_date", "question", "slug", "condition_id",
    ]
    ts = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:00")

    async def refresh_one(gamma: GammaClient, slug: str,
                          rates: dict[str, float]) -> object | None:
        """按 slug 本地找 cid，再用 condition_ids 直拉 Gamma 更新单市场。"""
        local = store.get_by_slug(slug)
        if local is None:
            console.print(f"[yellow]skip {slug}: 本地无缓存，先跑一次 run 建库[/yellow]")
            return None
        if not local.condition_id:
            console.print(f"[yellow]skip {slug}: 缓存里没有 condition_id[/yellow]")
            return local
        try:
            raws = await gamma.markets_by_condition([local.condition_id])
            raw = raws.get(local.condition_id)
            if not raw:
                console.print(f"[yellow]warn {slug}: Gamma 没返回 {local.condition_id[:8]}[/yellow]")
                return local
            m = parse_market(raw, rates)
            if m is None:
                console.print(f"[yellow]warn {slug}: parse_market 返回 None[/yellow]")
                return local
            store.upsert_market(m)
            return m
        except Exception as exc:
            console.print(f"[red]error {slug}: {type(exc).__name__}: {exc}[/red]")
            return local

    async def _go() -> int:
        rows = []
        enabled = (cfg.enabled_markets
                   if isinstance(cfg.enabled_markets, list)
                   else cfg.enabled_markets())
        async with GammaClient(cfg.wallet.gamma_host) as gamma:
            rates = await fetch_reward_rates(cfg.wallet.clob_host)
            gw = ExecutionGateway(cfg, journal=None)
            for entry in enabled:
                meta = await refresh_one(gamma, entry.slug, rates)
                if meta is None:
                    continue
                b = await gw.get_book(meta.yes.token_id)
                if not b:
                    console.print(f"[yellow]skip {meta.slug}: 盘口拉不到[/yellow]")
                    continue
                sc = score_market(meta)
                rows.append([
                    ts,
                    f"{sc.score:.3f}", f"{meta.rewards_daily_rate:.2f}",
                    f"{sc.rebate_potential:.2f}",
                    f"{b.get('best_ask', 0) - b.get('best_bid', 0):.4f}",
                    b.get("best_bid"), b.get("best_ask"), f"{meta.tick_size:g}",
                    f"{meta.min_order_size:g}", int(meta.neg_risk),
                    f"{meta.taker_fee_bps / 100:.1f}", f"{meta.rebate_rate * 100:.0f}",
                    meta.rewards_max_spread, f"{meta.liquidity_num:.0f}",
                    f"{meta.volume_24hr:.0f}", meta.end_date_iso or "",
                    meta.question, meta.slug, meta.condition_id,
                ])
        write_header = not os.path.exists(out)
        with open(out, "a", newline="") as fh:
            w = csv.writer(fh)
            if write_header:
                w.writerow(fields)
            w.writerows(rows)
        return len(rows)

    n = asyncio.run(_go())
    console.print(f"[green]Appended {n} rows to {out} (snapshot {ts}).[/green]")

if __name__ == "__main__":
    app()
