"""polymaker command-line interface.

  polymaker scan                 sweep Gamma for political markets -> SQLite
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
    min_liquidity: float = typer.Option(1000.0, help="minimum market liquidity (USDC)"),
    all_markets: bool = typer.Option(False, "--all", help="include non-rewards markets"),
) -> None:
    """Sweep Gamma for political markets, score, and persist to SQLite."""
    from polymaker.catalog.scanner import ScanConfig, run_scan
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)

    async def _go() -> int:
        scan_cfg = ScanConfig(
            tag_slugs=cfg.scan.tag_slugs,
            min_liquidity=min_liquidity,
            min_volume_24hr=cfg.scan.min_volume_24hr,
            rewards_only=not all_markets,
            gamma_host=cfg.wallet.gamma_host,
            clob_host=cfg.wallet.clob_host,
        )
        metas = await run_scan(store, scan_cfg)
        return len(metas)

    n = asyncio.run(_go())
    csv_path = Path(config_dir).parent / "markets.csv"
    written = store.export_csv(csv_path)
    console.print(f"[green]Scanned and stored {n} markets.[/green] "
                  f"Wrote [bold]{csv_path}[/bold] ({written} rows) — open it, pick markets, "
                  f"then `polymaker markets-add <slug>`.")
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

    table = Table(title="Political markets by score")
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
    limit: int = typer.Option(500, help="max rows"),
) -> None:
    """Export the scored market catalog to a CSV for easy picking."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    n = store.export_csv(out, limit)
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


# ── Snapshot collector ────────────────────────────────────────────────────


async def _resolve_market_ids(slug: str, gamma_host: str) -> tuple[str, str] | None:
    """从 Gamma 按 slug 查 condition_id 和 yes_token_id。"""
    import json as _json
    import httpx

    async with httpx.AsyncClient(base_url=gamma_host, timeout=15.0) as client:
        r = await client.get("/markets", params={"slug": slug, "limit": 1})
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        m = data[0]
        condition_id = m["conditionId"]
        yes_token_id = _json.loads(m["clobTokenIds"])[0]
        return condition_id, yes_token_id


@app.command(name="snapshot-collector")
def snapshot_collector(
    config_dir: str = typer.Option("config", help="config directory"),
    interval: int = typer.Option(7200, help="采集间隔秒数（默认 7200 = 2 小时）"),
    once: bool = typer.Option(False, "--once", help="只跑一次不循环"),
) -> None:
    """启动2小时快照采集器（独立后台进程）。"""
    from polymaker.catalog.snapshots import SnapshotCollector, SnapshotStore
    from polymaker.logging import configure
    import signal

    cfg = Config.load(config_dir)
    configure(json_file=Path(cfg.paths.log_dir) / "snapshot-collector.jsonl")

    store = SnapshotStore(cfg.paths.db)
    collector = SnapshotCollector(store)

    async def _run_cycle() -> None:
        """跑一轮所有 enabled 市场的采集。"""
        markets = cfg.enabled_markets
        if not markets:
            console.print("[yellow]没有 enabled 的市场，退出。[/yellow]")
            return

        console.print(f"[dim]开始采集 {len(markets)} 个市场…[/dim]")
        for m in markets:
            slug = m.slug or m.condition_id or "?"
            try:
                if m.condition_id:
                    condition_id = m.condition_id
                    ids = await _resolve_market_ids(slug, cfg.wallet.gamma_host)
                    if ids is None:
                        console.print(f"[red]找不到市场 {slug}[/red]")
                        continue
                    _, yes_token_id = ids
                else:
                    ids = await _resolve_market_ids(slug, cfg.wallet.gamma_host)
                    if ids is None:
                        console.print(f"[red]找不到市场 {slug}[/red]")
                        continue
                    condition_id, yes_token_id = ids

                await collector.collect(condition_id, yes_token_id)

                # 打印简洁日志
                rows = store.history(condition_id, limit=1)
                if rows:
                    r = rows[0]
                    trend_icon = {"up": "📈", "down": "📉", "flat": "➡️"}.get(r.trend, "⚪")
                    dd_str = f"{r.drawdown_from_high*100:.1f}%" if r.drawdown_from_high != 0 else "-"
                    console.print(
                        f"  {trend_icon} {slug[:36]:<36} "
                        f"close={r.price_close:.4f} "
                        f"dd={dd_str} "
                        f"depth={r.bid_depth_top5:>8,.0f}"
                    )
            except Exception as exc:
                console.print(f"  [red]✗[/red] {slug[:36]:<36} {exc}")

    async def _loop() -> None:
        """主循环。"""
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        cycle = 0
        while not stop.is_set():
            cycle += 1
            now = datetime.now().strftime("%H:%M:%S")
            console.print(f"\n[bold cyan]=== 快照采集 第 {cycle} 轮 {now} ===[/bold cyan]")
            await _run_cycle()

            if once:
                break

            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

        await collector.aclose()
        store.close()
        console.print("\n[yellow]快照采集器已停止。[/yellow]")

    from datetime import datetime
    console.print(f"[bold green]启动 snapshot-collector[/bold green] 间隔={interval}s")
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        console.print("\n[yellow]手动停止。[/yellow]")


@app.command(name="snapshots")
def snapshots(
    slug: str = typer.Argument(..., help="市场 slug"),
    config_dir: str = typer.Option("config", help="config directory"),
    buckets: int = typer.Option(24, help="显示最近 N 个桶"),
) -> None:
    """查某个市场的2小时快照数据。"""
    from polymaker.catalog.snapshots import SnapshotStore

    cfg = Config.load(config_dir)
    store = SnapshotStore(cfg.paths.db)

    # 先解析 condition_id
    async def _resolve() -> str | None:
        ids = await _resolve_market_ids(slug, cfg.wallet.gamma_host)
        return ids[0] if ids else None

    condition_id = asyncio.run(_resolve())
    if condition_id is None:
        console.print(f"[red]找不到市场 {slug}[/red]")
        raise typer.Exit(1)

    rows = store.history(condition_id, limit=buckets)
    if not rows:
        console.print("[yellow]还没有快照数据，先跑一次 snapshot-collector。[/yellow]")
        raise typer.Exit()

    table = Table(title=f"2小时快照: {slug}")
    table.add_column("时间桶")
    table.add_column("收盘", justify="right")
    table.add_column("趋势", justify="center")
    table.add_column("连向", justify="right")
    table.add_column("回撤", justify="right")
    table.add_column("分位", justify="right")
    table.add_column("买盘深度", justify="right")
    table.add_column("深度变化", justify="right")
    table.add_column("波动", justify="center")

    for r in rows:
        dt = datetime.fromtimestamp(r.bucket_ts, tz=timezone.utc).strftime("%m-%d %H:00")
        trend_icon = {"up": "📈", "down": "📉", "flat": "➡️"}.get(r.trend, "?")
        dd = f"{r.drawdown_from_high*100:.1f}%" if r.drawdown_from_high != 0 else "-"
        pct = f"{r.price_percentile*100:.0f}%" if r.price_percentile != 0.5 else "-"
        depth_chg = f"{r.bid_depth_change*100:+.0f}%" if r.bid_depth_change != 0 else "-"

        table.add_row(
            dt,
            f"{r.price_close:.4f}",
            trend_icon,
            f"{r.trend_streak:+d}",
            dd,
            pct,
            f"${r.bid_depth_top5:,.0f}",
            depth_chg,
            r.vol_regime or "-",
        )

    console.print(table)
    store.close()


if __name__ == "__main__":
    app()
#（注：内容由AI生成）
