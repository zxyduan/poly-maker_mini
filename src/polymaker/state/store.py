"""StateStore: the single owner of positions and open orders.

Replaces v1's module-level global dicts + the `performing`/`last_trade_update`
races. Three inputs, one arbitration rule (the README):

  * WS fill events apply immediately (optimistic),
  * REST reconciliation corrects drift ONLY for tokens with no in-flight trades,
  * on-chain balances are consulted only by the merger.

In-memory + typed, mirrored to SQLite on change so a crash-restart resumes.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from polymaker.domain import Fill, OpenOrder, OrderState, Position, Side
from polymaker.logging import get_logger

log = get_logger("state.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    token_id  TEXT PRIMARY KEY,
    size      REAL NOT NULL,
    avg_price REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    trade_id  TEXT PRIMARY KEY,
    token_id  TEXT, side TEXT, price REAL, size REAL, is_maker INT, ts REAL
);
CREATE TABLE IF NOT EXISTS order_log (
    order_id  TEXT PRIMARY KEY,
    token_id  TEXT, side TEXT, price REAL, size REAL, state TEXT, ts REAL
);
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    ts        REAL PRIMARY KEY,
    equity    REAL, net_cash REAL, inventory_value REAL, daily_pnl REAL
);

-- 下单时的决策环境快照：事后分析"这笔单挂得合不合理"
CREATE TABLE IF NOT EXISTS order_context (
    order_id         TEXT PRIMARY KEY,
    condition_id     TEXT NOT NULL,
    token_id         TEXT NOT NULL,
    side             TEXT NOT NULL,          -- BUY / SELL
    price            REAL NOT NULL,
    size             REAL NOT NULL,

    -- 决策时的市场环境
    regime           TEXT NOT NULL,          -- QUIET / TRENDING / EVENT / ...
    fv               REAL NOT NULL,          -- 当时 YES 的公允价值
    vol_short        REAL,
    vol_ratio        REAL,
    toxicity         REAL,
    flow_z           REAL,
    inventory_util   REAL,
    risk_size_scale  REAL,

    -- 盘口状态（YES 侧）
    yes_best_bid     REAL,
    yes_best_ask     REAL,
    our_offset_ticks REAL,                 -- 本单价格离 best_bid 几跳（买单=负方向=压价多少）

    -- 库存快照
    pos_yes_size     REAL,
    pos_no_size      REAL,

    -- 策略参数快照（事后能复算为什么挂这个价/量）
    strategy_type    TEXT,                  -- maker / one_way
    base_size_usdc   REAL,
    q_max_usdc       REAL,
    vol_factor       REAL,                  -- adaptive 系数
    vol_regime_factor REAL,

    placed_ts        REAL NOT NULL,

    -- 结果（后续回填）
    fill_price       REAL,
    fill_size        REAL,
    fill_ts          REAL,
    canceled_ts      REAL
);
CREATE INDEX IF NOT EXISTS idx_orderctx_cid_time ON order_context(condition_id, placed_ts DESC);
CREATE INDEX IF NOT EXISTS idx_orderctx_side ON order_context(condition_id, side);
CREATE INDEX IF NOT EXISTS idx_orderctx_filled ON order_context(fill_ts) WHERE fill_ts IS NOT NULL;
"""


class StateStore:
    """Owns positions + open orders + a per-token in-flight guard."""

    def __init__(self, db_path: str | Path = "state.db") -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        self.positions: dict[str, Position] = {}
        # order_id -> OpenOrder
        self.orders: dict[str, OpenOrder] = {}
        # token_id -> count of in-flight (MATCHED-not-CONFIRMED) trades; guards reconcile
        self._inflight: dict[str, int] = {}
        self._inflight_ts: dict[str, float] = {}  # oldest in-flight mark, for expiry
        self._last_fill_ts: dict[str, float] = {}
        self._load()

    def close(self) -> None:
        self._conn.close()

    # ── positions ───────────────────────────────────────────────────────
    def position(self, token_id: str) -> Position:
        return self.positions.get(token_id, Position(token_id))

    def apply_fill(self, fill: Fill) -> bool:
        """Apply a fill optimistically to inventory + avg price.

        IDEMPOTENT: the SQLite fills table is the dedupe gate (trade_id is the
        primary key). A replayed fill — WS redelivery after reconnect, a MATCHED
        arriving again after CONFIRMED, or a replay across process restarts —
        is detected by INSERT OR IGNORE and NOT applied twice. Returns False
        for duplicates so callers can skip their side effects too.
        """
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO fills(trade_id,token_id,side,price,size,is_maker,ts) VALUES(?,?,?,?,?,?,?)",
            (fill.trade_id, fill.token_id, fill.side.value, fill.price, fill.size,
             int(fill.is_maker), fill.ts),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            log.warning("duplicate_fill_ignored", trade_id=fill.trade_id,
                        token=fill.token_id[:12], side=fill.side.value, size=fill.size)
            return False

        pos = self.positions.setdefault(fill.token_id, Position(fill.token_id))
        signed = fill.size if fill.side is Side.BUY else -fill.size
        new_size = pos.size + signed
        if fill.side is Side.BUY:
            if pos.size <= 0:
                pos.avg_price = fill.price
            else:
                pos.avg_price = (pos.avg_price * pos.size + fill.price * fill.size) / (
                    pos.size + fill.size
                )
        # selling leaves avg_price unchanged
        pos.size = max(0.0, new_size)
        if pos.size <= 0:
            pos.avg_price = 0.0
        self._last_fill_ts[fill.token_id] = fill.ts
        self._persist_position(pos)
        log.info("fill", token=fill.token_id[:12], side=fill.side.value,
                 price=fill.price, size=fill.size, pos=round(pos.size, 2))
        return True

    def set_position(self, token_id: str, size: float, avg_price: float) -> None:
        pos = Position(token_id, max(0.0, size), avg_price if size > 0 else 0.0)
        self.positions[token_id] = pos
        self._persist_position(pos)

    def reconcile_positions(self, api_positions: dict[str, tuple[float, float]]) -> None:
        """Overwrite sizes from REST, skipping tokens with in-flight trades or
        a very recent fill (the optimistic value is more current there)."""
        now = time.time()
        for token_id, (size, avg) in api_positions.items():
            if self._inflight.get(token_id, 0) > 0:
                continue
            if now - self._last_fill_ts.get(token_id, 0.0) < 5.0:
                continue
            self.set_position(token_id, size, avg)

    # ── in-flight guard ─────────────────────────────────────────────────
    def mark_inflight(self, token_id: str) -> None:
        self._inflight[token_id] = self._inflight.get(token_id, 0) + 1
        self._inflight_ts.setdefault(token_id, time.time())

    def clear_inflight(self, token_id: str) -> None:
        if self._inflight.get(token_id, 0) > 0:
            self._inflight[token_id] -= 1
        if self._inflight.get(token_id, 0) == 0:
            self._inflight_ts.pop(token_id, None)

    def inflight(self, token_id: str) -> int:
        return self._inflight.get(token_id, 0)

    def expire_inflight(self, max_age_s: float) -> list[str]:
        """Force-clear in-flight guards older than max_age_s.

        A MATCHED whose CONFIRMED/FAILED never arrives (dropped WS event) would
        otherwise block reconciliation for that token forever. Returns the
        tokens cleared so the engine can force an authoritative REST reconcile.
        """
        now = time.time()
        stale = [t for t, ts in self._inflight_ts.items() if now - ts > max_age_s]
        for t in stale:
            age = round(now - self._inflight_ts[t])
            self._inflight[t] = 0
            self._inflight_ts.pop(t, None)
            log.warning("inflight_expired", token=t[:12], age_s=age)
        return stale

    # ── orders ──────────────────────────────────────────────────────────
    def orders_for(self, token_id: str) -> list[OpenOrder]:
        return [o for o in self.orders.values() if o.token_id == token_id]

    def upsert_order(self, order: OpenOrder) -> None:
        if order.state in (OrderState.CANCELED, OrderState.DONE, OrderState.REJECTED):
            self.orders.pop(order.order_id, None)
        else:
            self.orders[order.order_id] = order
        self._persist_order(order)

    def remove_order(self, order_id: str) -> None:
        self.orders.pop(order_id, None)

    def replace_open_orders(
        self, token_id: str, live: list[OpenOrder], *, grace_s: float = 10.0
    ) -> None:
        """Replace our view of a token's open orders from a REST snapshot.

        DOUBLE-ORDER GUARD: a REST snapshot can lag a placement by seconds. If we
        dropped a just-placed order because the snapshot didn't include it yet,
        the reconciler would immediately re-place it -> duplicate live orders.
        So local orders younger than `grace_s` survive even when absent from the
        snapshot (pass grace_s=0 to force an authoritative wipe, e.g. after the
        exchange auto-cancelled everything on a heartbeat gap).
        """
        now = time.time()
        live_ids = {o.order_id for o in live}
        for o in [o for o in self.orders.values() if o.token_id == token_id]:
            if o.order_id in live_ids:
                continue
            if now - o.created_ts < grace_s:
                continue  # too young to trust its absence from the snapshot
            self.orders.pop(o.order_id, None)
        for o in live:
            self.orders[o.order_id] = o

    def clear_orders(self) -> None:
        """Forget all local open orders (e.g. after a confirmed server-side wipe)."""
        self.orders.clear()

    # ── persistence ─────────────────────────────────────────────────────
    def _persist_position(self, pos: Position) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO positions(token_id,size,avg_price,updated_ts) VALUES(?,?,?,?)",
            (pos.token_id, pos.size, pos.avg_price, time.time()),
        )
        self._conn.commit()

    def _persist_order(self, o: OpenOrder) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO order_log(order_id,token_id,side,price,size,state,ts) VALUES(?,?,?,?,?,?,?)",
            (o.order_id, o.token_id, o.side.value, o.price, o.size, o.state.value, time.time()),
        )
        self._conn.commit()

    def _load(self) -> None:
        for row in self._conn.execute("SELECT token_id,size,avg_price FROM positions"):
            if row["size"] > 0:
                self.positions[row["token_id"]] = Position(
                    row["token_id"], row["size"], row["avg_price"]
                )

    def drop_untracked_positions(self, tracked: set[str]) -> list[str]:
        """Remove positions for tokens we don't trade (e.g. the operator's manual
        UI bets that leaked in via an earlier unscoped reconcile). They must not
        count toward exposure caps or PnL. Returns the dropped token ids."""
        dropped = [t for t in self.positions if t not in tracked]
        for t in dropped:
            self.positions.pop(t, None)
            with contextlib.suppress(sqlite3.Error):
                self._conn.execute("DELETE FROM positions WHERE token_id=?", (t,))
        if dropped:
            self._conn.commit()
            log.info("untracked_positions_dropped", n=len(dropped))
        return dropped

    def force_set_position(self, token_id: str, size: float, avg_price: float, source: str) -> None:
        """Overwrite a position unconditionally (used when on-chain is truth)."""
        prev = self.positions.get(token_id)
        self.set_position(token_id, size, avg_price)
        log.warning("position_forced", token=token_id[:12], source=source,
                    prev=round(prev.size, 2) if prev else 0.0, now=round(size, 2))

    # ── order context snapshots ───────────────────────────────────────
    def record_order_context(self, oid: str, *, cid: str, token_id: str, side: str,
                             price: float, size: float, regime: str, fv: float,
                             vol_short: float | None, vol_ratio: float | None,
                             toxicity: float | None, flow_z: float | None,
                             inventory_util: float | None, risk_size_scale: float | None,
                             yes_best_bid: float | None, yes_best_ask: float | None,
                             our_offset_ticks: float | None,
                             pos_yes_size: float, pos_no_size: float,
                             strategy_type: str | None, base_size_usdc: float | None,
                             q_max_usdc: float | None,
                             vol_factor: float | None, vol_regime_factor: float | None,
                             placed_ts: float) -> None:
        """记录一笔下单时的完整决策环境快照。幂等：同一 order_id 不重复插。"""
        self._conn.execute(
            """INSERT OR IGNORE INTO order_context(
                order_id, condition_id, token_id, side, price, size,
                regime, fv, vol_short, vol_ratio, toxicity, flow_z,
                inventory_util, risk_size_scale,
                yes_best_bid, yes_best_ask, our_offset_ticks,
                pos_yes_size, pos_no_size,
                strategy_type, base_size_usdc, q_max_usdc,
                vol_factor, vol_regime_factor, placed_ts
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, cid, token_id, side, price, size,
             regime, fv, vol_short, vol_ratio, toxicity, flow_z,
             inventory_util, risk_size_scale,
             yes_best_bid, yes_best_ask, our_offset_ticks,
             pos_yes_size, pos_no_size,
             strategy_type, base_size_usdc, q_max_usdc,
             vol_factor, vol_regime_factor, placed_ts),
        )
        self._conn.commit()

    def mark_order_filled(self, order_id: str, *, price: float, size: float, ts: float) -> None:
        """回填某笔订单的成交结果。只更新还没记成交的行。"""
        self._conn.execute(
            "UPDATE order_context SET fill_price=?, fill_size=?, fill_ts=? "
            "WHERE order_id=? AND fill_ts IS NULL",
            (price, size, ts, order_id),
        )
        self._conn.commit()

    def mark_order_canceled(self, order_id: str, *, ts: float) -> None:
        """回填某笔订单被撤单的时间。"""
        self._conn.execute(
            "UPDATE order_context SET canceled_ts=? WHERE order_id=? AND canceled_ts IS NULL",
            (ts, order_id),
        )
        self._conn.commit()

    def mark_all_unfilled_as_canceled(self, *, now: float) -> None:
        """引擎重启时批量标记：所有还没成交也没标记撤单的旧单，统一算"重启时结束"。

        理由：重启后 cancel_all 清了场，本地不再追踪这些单；从分析角度看
        它们的最终状态就是"引擎重启时不再挂着了"，不该永远显示"挂着"。
        """
        cur = self._conn.execute(
            "UPDATE order_context SET canceled_ts=? "
            "WHERE fill_ts IS NULL AND canceled_ts IS NULL",
            (now,),
        )
        self._conn.commit()
        if cur.rowcount > 0:
            log.info("bulk_canceled_on_restart", n=cur.rowcount)

    # ── maintenance / reporting ─────────────────────────────────────────
    def checkpoint_wal(self) -> None:
        """Truncate the WAL so it can't grow without bound under high volume."""
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def record_pnl(self, equity: float, net_cash: float, inv_value: float, daily_pnl: float) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.execute(
                "INSERT OR REPLACE INTO pnl_snapshots(ts,equity,net_cash,inventory_value,daily_pnl)"
                " VALUES(?,?,?,?,?)",
                (time.time(), equity, net_cash, inv_value, daily_pnl),
            )
            self._conn.commit()

    def snapshot(self) -> dict[str, object]:
        return {
            "positions": {k: json.loads(_pos_json(v)) for k, v in self.positions.items() if v.size > 0},
            "open_orders": len(self.orders),
        }


def _pos_json(p: Position) -> str:
    return json.dumps({"size": round(p.size, 4), "avg_price": round(p.avg_price, 4)})
