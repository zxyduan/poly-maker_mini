"""ExecutionGateway: the only component that sends actions to the CLOB.

Wraps the unified async SDK (``polymarket-client`` — AsyncSecureClient) which
owns the hard V2 EIP-712 signing, pUSD balance adjustment, and tick/fee
resolution, and exposes the async methods directly on the event loop (no more
thread-pool offload: the unified client is natively async). Every quote goes
out **post-only** (the maker-only mandate, enforced at the exchange).

A `paper=True` gateway shares the same path but fabricates order ids instead of
posting — so paper mode exercises the full pipeline.

Migrated from py-clob-client-v2 / py-builder-relayer-client per the official
"从旧版 SDK 迁移到统一 SDK" guide (docs/polymarket-docs).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any, TypeVar

import httpx

from polymaker.config import Config
from polymaker.domain import MarketMeta, OpenOrder, OrderState, Quote, Side
from polymaker.execution.ratelimit import TokenBucket
from polymaker.journal import Journal
from polymaker.l2auth import l2_headers
from polymaker.logging import get_logger

log = get_logger("execution.gateway")

_T = TypeVar("_T")


class ExecutionGateway:
    def __init__(
        self,
        cfg: Config,
        journal: Journal | None = None,
        *,
        paper: bool = False,
    ) -> None:
        self._cfg = cfg
        self._paper = paper
        self._journal = journal
        self._client: Any = None  # polymarket.AsyncSecureClient (lazy)
        self._creds: Any = None  # ApiKeyCreds (key/secret/passphrase)
        self._address: str = ""  # signer EOA
        self._funder: str = ""  # funds/positions live here (proxy/deposit wallet)
        self._data_host = cfg.wallet.data_api_host
        # rate budgets: fraction of documented POST/DELETE ceilings (per second)
        f = cfg.execution.rate_budget_fraction
        self._order_bucket = TokenBucket(rate_per_s=200.0 * f, burst=500.0 * f)
        self._cancel_bucket = TokenBucket(rate_per_s=200.0 * f, burst=500.0 * f)
        self._paper_ids = itertools.count(1)
        self._hb_failures: int = 0
        # dedicated, bounded pool for blocking order/HTTP calls so a burst of
        # requotes across many markets can't starve the default executor
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="clob-io")

    @property
    def paper(self) -> bool:
        return self._paper

    @property
    def order_pressure(self) -> float:
        """0 = plenty of order-post budget, 1 = about to queue (shed load)."""
        return self._order_bucket.pressure

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Close the unified-SDK client (async) and the web3 thread pool."""
        self.close()
        client = self._client
        self._client = None
        self._creds = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def _io(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking client call on the dedicated pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn, *args)

    @property
    def creds(self) -> Any:
        return self._creds

    @property
    def address(self) -> str:
        """The signing EOA address."""
        return self._address

    @property
    def funder(self) -> str:
        """The address holding funds/positions (proxy/deposit wallet, or the EOA)."""
        return self._funder or self._address

    # ── lifecycle ───────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Build the unified SDK client and bootstrap L2 creds (network).

        ``AsyncSecureClient.create`` derives/creates the CLOB API key, resolves
        the account wallet (EOA / Gnosis Safe / deposit wallet — the SDK
        classifies on-chain from ``wallet``), and configures signing. No-op
        fields in paper mode.
        """
        sec = self._cfg.secrets
        if self._paper and not sec.has_wallet:
            # paper mode runs the full pipeline without a wallet (no orders posted)
            self._address = sec.browser_address or "0xPAPER"
            self._funder = sec.browser_address or self._address
            log.info("gateway_connected", address=self._address[:10], paper=True)
            return
        if not sec.has_wallet:
            raise RuntimeError("no wallet configured (set PK and BROWSER_ADDRESS in .env)")

        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        from polymarket import AsyncSecureClient

        def _on_rate_limit(update: Any) -> None:
            """Per-signer rate-limit state echoed by order/cancel responses."""
            remaining = getattr(update, "remaining", None)
            warning = bool(getattr(update, "warning", False))
            if warning or (remaining is not None and remaining < 20):
                log.warning(
                    "rate_limit_warning",
                    remaining=remaining,
                    reset=getattr(update, "reset", None),
                    tier=getattr(update, "tier", None),
                    warning=warning,
                    note="approaching/enforcing order-rate cap — shed quote load",
                )
            else:
                log.info(
                    "rate_limit_update",
                    remaining=remaining,
                    tier=getattr(update, "tier", None),
                )

        try:
            client = await AsyncSecureClient.create(
                private_key=sec.pk,
                # wallet = the funds/positions address (deposit wallet, safe, or EOA);
                # the SDK resolves wallet type on-chain and signs accordingly.
                wallet=sec.browser_address,
                on_rate_limit_update=_on_rate_limit,
            )
        except Exception as exc:  # noqa: BLE001 - fatal but keep context for live ops
            log.error(
                "client_bootstrap_failed",
                err=str(exc),
                host=self._cfg.wallet.clob_host,
                chain_id=self._cfg.wallet.chain_id,
                note="AsyncSecureClient.create (cred derivation / wallet classify / RPC)",
            )
            raise
        self._client = client
        self._creds = client.credentials
        self._address = str(client.signer)
        await self._check_clock_drift()
        # funds/positions live on the funder (proxy/deposit wallet); fall back to EOA
        self._funder = sec.browser_address or self._address
        try:
            sdk_version = _pkg_version("polymarket-client")
        except PackageNotFoundError:
            sdk_version = "unknown"
        log.info(
            "gateway_connected",
            signer=self._address[:10],
            funder=self._funder[:10],
            wallet_type=getattr(client, "wallet_type", None),
            creds_ready=bool(getattr(client.credentials, "key", "")),
            sdk_version=sdk_version,
            paper=self._paper,
        )

    async def _check_clock_drift(self) -> None:
        """Warn once if the local clock is skewed vs the exchange (affects L2 auth)."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/time")
                server = float(r.text.strip().strip('"'))
            drift = abs(time.time() - server)
            if drift > 5.0:
                log.warning(
                    "clock_drift",
                    drift_s=round(drift, 1),
                    note="sync system clock (NTP) — large skew can fail order auth",
                )
            else:
                log.info("clock_ok", drift_s=round(drift, 1))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("clock_check_failed", err=str(exc))

    # ── placement ───────────────────────────────────────────────────────
    async def place(self, quotes: list[Quote], meta: MarketMeta) -> list[OpenOrder]:
        if not quotes:
            return []
        await self._order_bucket.acquire(len(quotes))
        ts = time.time()
        self._journal_write("orders_out", [asdict(q) for q in quotes], ts)

        if self._paper:
            return [self._paper_order(q) for q in quotes]

        # The unified SDK resolves tick size / neg-risk / fees / signing per
        # order; post-only is a per-order flag (maker-only mandate).
        try:
            signed = [
                await self._client.create_limit_order(
                    token_id=q.token_id,
                    price=q.price,
                    size=q.size,
                    side=q.side.value,
                    post_only=self._cfg.execution.post_only,
                )
                for q in quotes
            ]
            resp = await self._client.post_orders(signed)
            return self._parse_place_response(resp, quotes)
        except Exception as exc:  # noqa: BLE001 - surface + continue; engine handles error rate
            log.error(
                "place_failed",
                err=str(exc),
                n=len(quotes),
                token_ids=[q.token_id[:12] for q in quotes],
                post_only=self._cfg.execution.post_only,
            )
            return []

    def _paper_order(self, q: Quote) -> OpenOrder:
        oid = f"paper-{next(self._paper_ids)}"
        return OpenOrder(oid, q.token_id, q.side, q.price, q.size, OrderState.LIVE)

    def _parse_place_response(self, resp: Any, quotes: list[Quote]) -> list[OpenOrder]:
        """Map a batch post response to OpenOrders. Tolerant of shape variants;
        the user-WS order events + REST snapshot reconcile anything we miss."""
        log.info(
            "place_resp",
            resp=str(resp)[:500],
            n_quotes=len(quotes),
            sides=[q.side.value for q in quotes],
            prices=[q.price for q in quotes],
            sizes=[q.size for q in quotes],
            post_only=self._cfg.execution.post_only,
        )
        # unified SDK: tuple of AcceptedOrder / RejectedOrder models
        if isinstance(resp, dict):
            items: Any = resp.get("orders", resp.get("data", []))
        elif isinstance(resp, (list, tuple)):
            items = resp
        else:
            items = []
        out: list[OpenOrder] = []
        for q, item in zip(quotes, items, strict=False):
            if isinstance(item, dict):
                oid = _first(item, "orderID", "orderId", "order_id", "id", "hash")
                err = str(item.get("message", item.get("error", "")))
            else:
                oid = getattr(item, "order_id", None)
                ok = getattr(item, "ok", True)
                err = str(getattr(item, "message", getattr(item, "code", "")))
                if not ok:
                    err = err or getattr(item, "code", "")
            if not oid:
                log.warning(
                    "place_response_missing_id",
                    item=str(item)[:200],
                    side=q.side.value,
                    price=q.price,
                    size=q.size,
                )
                continue
            if err and err not in ("", "None"):
                log.warning(
                    "order_rejected",
                    oid=str(oid)[:16],
                    err=err[:200],
                    side=q.side.value,
                    price=q.price,
                    size=q.size,
                )
                continue
            out.append(OpenOrder(str(oid), q.token_id, q.side, q.price, q.size, OrderState.LIVE))
        return out

    # ── cancellation ────────────────────────────────────────────────────
    async def cancel(self, order_ids: list[str]) -> bool:
        """Cancel by id. Returns True on success — callers must NOT drop the
        orders from local state on failure (they may still be live)."""
        if not order_ids or self._paper:
            return True
        await self._cancel_bucket.acquire(1)

        try:
            await self._client.cancel_orders(order_ids=order_ids)
            log.info("cancel_sent", n=len(order_ids), ids=[o[:16] for o in order_ids[:20]])
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_failed", err=str(exc), n=len(order_ids))
            return False

    async def cancel_asset(self, asset_id: str) -> bool:
        """Cancel every order on one token (idempotent quarantine primitive)."""
        if self._paper:
            return True

        try:
            await self._client.cancel_market_orders(asset_id=asset_id)
            log.info("cancel_asset_sent", token=asset_id[:12])
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("cancel_asset_failed", err=str(exc), token=asset_id[:12])
            return False

    async def cancel_all(self) -> None:
        if self._paper or self._client is None:
            return
        await self._client.cancel_all()
        log.info("cancel_all_sent")

    # ── market (taker) orders — used by moneydoctor, NOT the maker strategy ──
    async def market_order(
        self,
        token_id: str,
        side: Side,
        amount: float,
        meta: MarketMeta,
        *,
        fak: bool = True,
    ) -> Any:
        """Place a marketable order. amount = USD for BUY, shares for SELL.

        This is a TAKER order (crosses the spread) — only the moneydoctor live
        self-test uses it; the maker strategy never does. Returns the unified
        SDK's AcceptedOrder/RejectedOrder (or a dict on failure).
        """
        if self._paper or self._client is None:
            return {"paper": True}

        try:
            order_type = "FAK" if fak else "FOK"
            if side is Side.BUY:
                return await self._client.place_market_order(
                    token_id=token_id,
                    side="BUY",
                    amount=amount,
                    order_type=order_type,
                )
            return await self._client.place_market_order(
                token_id=token_id,
                side="SELL",
                shares=amount,
                order_type=order_type,
            )
        except Exception as exc:  # noqa: BLE001 - surface as data, never crash the caller
            return {"status": "failed", "error": str(exc)}

    async def get_book(self, token_id: str) -> dict[str, float]:
        """Live best bid/ask + touch depth for one token (public REST)."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/book", params={"token_id": token_id})
                r.raise_for_status()
                b = r.json()
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                best_bid = max(bids)[0] if bids else 0.0
                best_ask = min(asks)[0] if asks else 1.0
                ask_depth = sum(s for p, s in asks if p <= best_ask + 1e-9)
                bid_depth = sum(s for p, s in bids if p >= best_bid - 1e-9)
                return {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "ask_depth": ask_depth,
                    "bid_depth": bid_depth,
                }
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("get_book_failed", err=str(exc))
            return {}

    async def get_full_book(
        self, token_id: str
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]], str | None] | None:
        """Full L2 book (bids, asks, hash) via public REST — for periodic
        integrity refresh against the WS book."""
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self._cfg.wallet.clob_host}/book", params={"token_id": token_id})
                r.raise_for_status()
                b = r.json()
                bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
                asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
                return bids, asks, b.get("hash")
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("get_full_book_failed", err=str(exc))
            return None

    async def token_balance(self, token_id: str) -> float:
        """Exact on-chain conditional-token balance (shares) held by the funder.

        Returns None on total RPC failure so callers can distinguish "0 shares"
        from "couldn't read".
        """
        bal = await self._token_balance_opt(token_id)
        return bal if bal is not None else 0.0

    async def _token_balance_opt(self, token_id: str) -> float | None:
        def _read() -> float | None:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            configured = self._cfg.secrets.polygon_rpc or self._cfg.wallet.polygon_rpc
            rpcs = [
                configured,
                "https://polygon-bor-rpc.publicnode.com",
                "https://polygon.llamarpc.com",
                "https://rpc.ankr.com/polygon",
            ]
            abi = [
                {
                    "name": "balanceOf",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [{"name": "a", "type": "address"}, {"name": "id", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                }
            ]
            for rpc in dict.fromkeys(rpcs):  # dedupe, keep order
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    ctf = w3.eth.contract(
                        address=Web3.to_checksum_address(
                            "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
                        ),
                        abi=abi,
                    )
                    raw = ctf.functions.balanceOf(
                        Web3.to_checksum_address(self.funder), int(token_id)
                    ).call()
                    return float(raw) / 1e6
                except Exception:  # noqa: BLE001, PERF203 - try next RPC
                    continue
            return None

        try:
            return await self._io(_read)
        except Exception as exc:  # noqa: BLE001
            log.warning("token_balance_failed", err=str(exc))
            return None

    async def token_balances(self, token_ids: list[str]) -> dict[str, float] | None:
        """Batch on-chain balances for several tokens in one RPC session.

        Used by the position-divergence monitor. Returns None on RPC failure.
        """
        if not token_ids:
            return {}

        def _read() -> dict[str, float] | None:
            from web3 import Web3
            from web3.middleware import ExtraDataToPOAMiddleware

            configured = self._cfg.secrets.polygon_rpc or self._cfg.wallet.polygon_rpc
            rpcs = [
                configured,
                "https://polygon-bor-rpc.publicnode.com",
                "https://polygon.llamarpc.com",
                "https://rpc.ankr.com/polygon",
            ]
            abi = [
                {
                    "name": "balanceOf",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [{"name": "a", "type": "address"}, {"name": "id", "type": "uint256"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                }
            ]
            funder = None
            for rpc in dict.fromkeys(rpcs):
                try:
                    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                    ctf = w3.eth.contract(
                        address=Web3.to_checksum_address(
                            "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
                        ),
                        abi=abi,
                    )
                    funder = Web3.to_checksum_address(self.funder)
                    out: dict[str, float] = {}
                    for tid in token_ids:
                        raw = ctf.functions.balanceOf(funder, int(tid)).call()
                        out[tid] = float(raw) / 1e6
                    return out
                except Exception:  # noqa: BLE001, PERF203
                    continue
            return None

        try:
            return await self._io(_read)
        except Exception as exc:  # noqa: BLE001
            log.warning("token_balances_failed", err=str(exc))
            return None

    async def collateral_balance(self) -> float:
        """pUSD balance (float) on the funder."""
        ba = await self.balance_allowance()
        for k in ("balance", "collateral", "amount"):
            if isinstance(ba, dict) and k in ba:
                try:
                    v = float(ba[k])
                    return v / 1e6 if v > 1e6 else v
                except (ValueError, TypeError):
                    return 0.0
        # unified SDK: BalanceAllowance model with raw-unit int balance
        bal = getattr(ba, "balance", None)
        if bal is not None:
            try:
                v = float(bal)
                return v / 1e6 if v > 1e6 else v
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    # ── heartbeat (dead-man switch) ─────────────────────────────────────
    async def heartbeat(self) -> bool:
        """Send one heartbeat tick. Returns True on success.

        Current exchange contract: ``POST {host}/heartbeats`` with L2
        headers only and NO body; any HTTP 200 is the ack. The unified SDK
        does not cover this endpoint, so we sign the L2 headers ourselves via
        ``polymaker.l2auth`` (byte-identical to the SDK's own header scheme).
        Consecutive failures are tracked in `heartbeat_failures`; after
        `heartbeat_halt_failures` misses the engine stops quoting and resyncs
        once this recovers.
        """
        if self._paper or self._client is None:
            return True

        def _beat() -> bool:
            # New path first; fall back to the legacy path only if 404.
            for path in ("/heartbeats", "/v1/heartbeats"):
                headers = l2_headers(self._client, "POST", path)
                r = httpx.post(
                    f"{self._cfg.wallet.clob_host}{path}",
                    headers=headers,
                    timeout=10.0,
                )
                if r.status_code == 200:
                    return True
                if r.status_code != 404:
                    break
            return False

        try:
            ok = await self._io(_beat)
            if not ok:
                raise RuntimeError("heartbeat not acknowledged by exchange")
            if self._hb_failures:
                log.info("heartbeat_recovered", after_failures=self._hb_failures)
            self._hb_failures = 0
            return True
        except Exception as exc:  # noqa: BLE001
            self._hb_failures += 1
            log.warning("heartbeat_failed", err=str(exc), consecutive=self._hb_failures)
            return False

    @property
    def heartbeat_failures(self) -> int:
        return self._hb_failures

    # ── reads ───────────────────────────────────────────────────────────
    async def open_orders(self) -> list[OpenOrder]:
        if self._paper or self._client is None:
            return []

        try:
            pages = self._client.list_open_orders()
            out: list[OpenOrder] = []
            async for r in pages.iter_items():
                try:
                    side = Side(str(r.side).upper())
                    remaining = float(r.original_size) - float(r.size_matched)
                    out.append(
                        OpenOrder(
                            str(r.id),
                            str(r.asset_id),
                            side,
                            float(r.price),
                            remaining,
                            OrderState.LIVE,
                        )
                    )
                except (ValueError, TypeError):
                    continue
            log.debug("open_orders_read", n=len(out), first=[o.order_id[:12] for o in out[:5]])
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("open_orders_failed", err=str(exc))
            return []

    async def positions(self) -> dict[str, tuple[float, float]]:
        """{token_id: (size, avg_price)} from the Data API v2 (reconcile use).

        Queries the FUNDER (where positions live), not the signer EOA. Uses the
        unified SDK's ``list_positions`` (GET /v2/positions — Data API v1 was
        deprecated 2026-10-24).
        """
        if self._paper or self._client is None:
            return {}
        user = self.funder
        if not user or not user.startswith("0x") or user == "0xPAPER":
            return {}
        try:
            pages = self._client.list_positions(user=user)
            out: dict[str, tuple[float, float]] = {}
            async for p in pages.iter_items():
                size = float(p.current_size)
                if size > 0:
                    out[str(p.asset_id)] = (size, float(p.avg_price or 0))
            log.debug("positions_read", n=len(out), tokens=[k[:12] for k in out][:10])
            return out
        except Exception as exc:  # noqa: BLE001
            log.warning("positions_failed", err=str(exc))
            return {}

    async def balance_allowance(self) -> Any:
        """Collateral balance/allowance snapshot (for `doctor`)."""
        if self._client is None:
            return {}
        try:
            return await self._client.get_balance_allowance(asset_type="COLLATERAL")
        except Exception as exc:  # noqa: BLE001
            log.warning("balance_allowance_failed", err=str(exc))
            return {}

    def _journal_write(self, kind: str, payload: Any, ts: float) -> None:
        if self._journal is not None:
            self._journal.write(kind, payload, ts)


def _first(d: Any, *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None


# （注：内容由AI生成）
