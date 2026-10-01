"""
每日市场统计采集器：每个市场、每个交易日一条 OHLC + 成交量。

设计目标：
  - 长尾盘单边策略需要两个核心信号：日成交量 + 日振幅
  - 一个市场一年才 365 条数据，SQLite 完全无压力
  - 独立进程运行（不依赖主引擎），每小时跑一次，一直挂着攒数据
  - 业务进程（路由模块、CLI）只负责读，读写通过 WAL 模式并发安全

数据来源：
  - Gamma /markets         → 返回 volumeNum（累计历史总成交量），两次采集相减 = 当日增量
  - CLOB  /prices-history  → 返回小时级价格序列，按 UTC 日期分组聚合出 OHLC
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from polymaker.logging import get_logger

log = get_logger("catalog.daily_stats")


# ── 数据库表结构 ──────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_daily_stats (
    condition_id   TEXT NOT NULL,
    date           TEXT NOT NULL,              -- UTC 日期，格式 'YYYY-MM-DD'
    volume_usdc    REAL NOT NULL DEFAULT 0.0,  -- 当日总成交量（USDC）
    price_open     REAL,                        -- 当日开盘价
    price_high     REAL,                        -- 当日最高价
    price_low      REAL,                        -- 当日最低价
    price_close    REAL,                        -- 当日收盘价
    range_pct      REAL,                        -- 日振幅 = (high - low) / open
    last_volume_num REAL,                       -- 上次采集时的累计成交量（算增量用）
    frozen         INTEGER NOT NULL DEFAULT 0,  -- 0=今天还在更新，1=昨天已冻结
    collected_ts   REAL NOT NULL,               -- 最后采集时间戳
    PRIMARY KEY (condition_id, date)            -- 联合主键：一个市场一天只有一条
);
-- 查询最近 N 天是高频操作，按日期倒序建索引
CREATE INDEX IF NOT EXISTS idx_daily_stats_cid_date
    ON market_daily_stats(condition_id, date DESC);
"""


# ── 数据类：一条日记录 ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DayStat:
    """一个市场某一天的统计快照（只读，不可变）。"""

    condition_id: str
    date: str
    volume_usdc: float
    price_open: float | None
    price_high: float | None
    price_low: float | None
    price_close: float | None
    range_pct: float | None
    frozen: bool


# ── Store 层：SQLite 读写 ────────────────────────────────────────────────


class DailyStatsStore:
    """市场日级统计的 SQLite 持久化层。

    职责：只管读写，不碰网络、不碰业务逻辑。
    读写隔离：WAL 模式下，业务进程读的时候采集器可以同时写，
    不会出现"读一半数据"的脏读问题——SQLite 事务原子性保证了这一点。
    """

    def __init__(self, db_path: str | Path = "state.db") -> None:
        self.path = str(db_path)
        # check_same_thread=False：连接在异步循环里可能跨线程使用，关掉检查
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row  # 查询结果可以按列名访问（row["volume_usdc"]）

        # WAL 模式：写操作先写日志文件，不锁整个数据库；读操作从快照读，不阻塞
        # 这就是为什么业务进程读的时候采集器能同时写、不会脏读
        self._conn.execute("PRAGMA journal_mode=WAL")

        # busy_timeout：万一真的写冲突了，等 5 秒而不是立刻抛 database is locked
        self._conn.execute("PRAGMA busy_timeout=5000")

        # 建表（如果不存在）
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接，程序退出时调用。"""
        self._conn.close()

    # ── 写操作 ────────────────────────────────────────────────────────

    def upsert_today(
        self,
        condition_id: str,
        *,
        volume_delta: float,
        open: float | None,
        high: float | None,
        low: float | None,
        close: float | None,
        last_volume_num: float | None,
    ) -> None:
        """插入或更新"今天"这条记录。成交量是累加的：每小时跑一次，增量累加。

        逻辑：
          - 如果今天还没有记录 → volume_usdc = 本次增量
          - 如果今天已有记录（第二、三...次采集）→ volume_usdc = 已有值 + 本次增量
          - OHLC 字段直接覆盖为最新快照（到当天结束时自然收敛到真实 OHLC）
          - 如果今天的记录已经被冻结（异常情况）→ 打 warning 跳过
        """
        today = _utc_today()
        now = time.time()

        # 先查今天有没有记录
        existing = self._conn.execute(
            "SELECT volume_usdc, frozen FROM market_daily_stats "
            "WHERE condition_id=? AND date=?",
            (condition_id, today),
        ).fetchone()

        if existing is None:
            # 今天第一次写，volume 就是本次增量
            new_vol = volume_delta
        else:
            if existing["frozen"]:
                # 正常情况下今天不应该是 frozen 状态，这是时钟偏移的安全网
                log.warning("stats_today_frozen_unexpected", cid=condition_id[:8], date=today)
                return
            # 累加：已有成交量 + 本次增量
            new_vol = (existing["volume_usdc"] or 0.0) + volume_delta

        # 提前算好日振幅存进去，查询时不用重算
        range_pct = None
        if open and high is not None and low is not None and open > 0:
            range_pct = (high - low) / open

        # UPSERT：主键冲突就更新，不冲突就插入
        # ON CONFLICT(condition_id, date) DO UPDATE SET ... excluded.xxx
        # excluded 是 SQLite 关键字，指"本来要插入的那行数据"
        self._conn.execute(
            """INSERT INTO market_daily_stats
               (condition_id, date, volume_usdc, price_open, price_high, price_low,
                price_close, range_pct, last_volume_num, frozen, collected_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(condition_id, date) DO UPDATE SET
                 volume_usdc=excluded.volume_usdc,
                 price_open=excluded.price_open,
                 price_high=excluded.price_high,
                 price_low=excluded.price_low,
                 price_close=excluded.price_close,
                 range_pct=excluded.range_pct,
                 last_volume_num=excluded.last_volume_num,
                 collected_ts=excluded.collected_ts""",
            (condition_id, today, new_vol, open, high, low, close,
             range_pct, last_volume_num, 0, now),
        )
        self._conn.commit()

    def backfill_day(
        self,
        condition_id: str,
        date: str,
        *,
        open: float | None,
        high: float | None,
        low: float | None,
        close: float | None,
    ) -> None:
        """回填历史某一天的 OHLC（冷启动用）。

        注意：历史日期没有累计成交量的 baseline，所以 volume_usdc = 0。
        直接标记 frozen=1，后续不会再更新这条记录。
        用 INSERT OR IGNORE：已经有这条记录就跳过，不覆盖已有数据。
        """
        range_pct = None
        if open and high is not None and low is not None and open > 0:
            range_pct = (high - low) / open

        self._conn.execute(
            """INSERT OR IGNORE INTO market_daily_stats
               (condition_id, date, volume_usdc, price_open, price_high, price_low,
                price_close, range_pct, last_volume_num, frozen, collected_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (condition_id, date, 0.0, open, high, low, close,
             range_pct, None, 1, time.time()),  # frozen=1：历史天直接冻结
        )
        self._conn.commit()

    def freeze_yesterday(self) -> int:
        """把昨天的记录标记为冻结（不再更新）。返回冻结了几条。

        每次采集开头先调这个：UTC 零点之后第一次跑，昨天就被锁住了。
        路由查 recent_avg 只查 frozen=1 的，今天不完整的数据不会污染均值。
        """
        yesterday = _utc_offset_date(-1)
        cur = self._conn.execute(
            "UPDATE market_daily_stats SET frozen=1 WHERE date=? AND frozen=0",
            (yesterday,),
        )
        self._conn.commit()
        return cur.rowcount

    # ── 读操作 ────────────────────────────────────────────────────────

    def recent_avg(self, condition_id: str, days: int = 3) -> tuple[float, float]:
        """返回最近 N 个完整交易日的 (平均成交量, 平均振幅)。

        关键：只统计 frozen=1 的记录（昨天及之前），不包含今天——
        今天的数据还在累积，不完整，会污染均值。
        数据不够 N 天就用现有的；一条都没有返回 (0.0, 0.0)。
        """
        rows = self._conn.execute(
            """SELECT volume_usdc, range_pct FROM market_daily_stats
               WHERE condition_id=? AND frozen=1
               ORDER BY date DESC LIMIT ?""",
            (condition_id, days),
        ).fetchall()
        if not rows:
            return 0.0, 0.0

        vols = [r["volume_usdc"] or 0.0 for r in rows]
        # range_pct 可能是 NULL（当天只有一个价格点，high=low），过滤掉再算
        ranges = [r["range_pct"] for r in rows if r["range_pct"] is not None]

        avg_vol = sum(vols) / len(vols)
        avg_range = sum(ranges) / len(ranges) if ranges else 0.0
        return avg_vol, avg_range

    def history(self, condition_id: str, limit: int = 30) -> list[DayStat]:
        """查最近 N 天的记录，新的在前。CLI 调试用。"""
        rows = self._conn.execute(
            """SELECT * FROM market_daily_stats
               WHERE condition_id=? ORDER BY date DESC LIMIT ?""",
            (condition_id, limit),
        ).fetchall()
        return [
            DayStat(
                condition_id=r["condition_id"],
                date=r["date"],
                volume_usdc=r["volume_usdc"],
                price_open=r["price_open"],
                price_high=r["price_high"],
                price_low=r["price_low"],
                price_close=r["price_close"],
                range_pct=r["range_pct"],
                frozen=bool(r["frozen"]),
            )
            for r in rows
        ]

    def last_volume_num(self, condition_id: str) -> float | None:
        """取最近一条记录里的 last_volume_num（上次采集时的累计成交量）。

        用来算本次采集的增量：delta = 当前累计 - 上次累计。
        如果返回 None 说明这个市场是第一次跑，没有 baseline。
        """
        row = self._conn.execute(
            "SELECT last_volume_num FROM market_daily_stats "
            "WHERE condition_id=? ORDER BY date DESC LIMIT 1",
            (condition_id,),
        ).fetchone()
        return row["last_volume_num"] if row else None


# ── Collector 层：从 API 拉数据，算完写入 Store ─────────────────────────


class DailyStatsCollector:
    """采集器：从 Gamma + CLOB 拉数据，算 OHLC 和日成交量，写入 Store。

    使用方式：
        store = DailyStatsStore("state.db")
        collector = DailyStatsCollector(store)
        await collector.collect(condition_id="0xabc...", yes_token_id="12345...")
    """

    def __init__(
        self,
        store: DailyStatsStore,
        *,
        gamma_host: str = "https://gamma-api.polymarket.com",
        clob_host: str = "https://clob.polymarket.com",
        timeout: float = 15.0,
        lookback_days: int = 30,
    ) -> None:
        self._store = store
        # Gamma 和 CLOB 是两个不同的 host，各建一个 AsyncClient
        self._gamma = httpx.AsyncClient(base_url=gamma_host, timeout=timeout)
        self._clob = httpx.AsyncClient(base_url=clob_host, timeout=timeout)
        self._lookback_days = lookback_days  # 预留：以后要拉更多历史可以用

    async def aclose(self) -> None:
        """关闭 HTTP 连接，程序退出时调用。"""
        await self._gamma.aclose()
        await self._clob.aclose()

    # ── 主流程：一次采集循环 ──────────────────────────────────────────

    async def collect(self, condition_id: str, yes_token_id: str) -> None:
        """跑一次完整的采集：冻结昨天 → 拉数据 → 回填历史 → 更新今天。"""

        # 第 0 步：先把昨天冻结，防止继续更新
        self._store.freeze_yesterday()

        # 第 1 步：拉两个数据源
        cum_volume = await self._fetch_cumulative_volume(condition_id)
        price_series = await self._fetch_price_history(yes_token_id)

        # 价格数据拉不到就别算了，连 OHLC 都没有
        if not price_series:
            log.warning("stats_no_price_series", cid=condition_id[:8])
            return

        # 第 2 步：把小时价格点按 UTC 天分组，算出每天的 OHLC
        by_day = _bucket_by_day(price_series)

        # 第 3 步：历史天数回填（冷启动时自动补最近 7 天的 OHLC）
        today = _utc_today()
        for date_str, (o, h, lo, c) in by_day.items():
            if date_str == today:
                continue  # 今天单独处理
            # INSERT OR IGNORE：已经有这条记录就跳过，不覆盖
            self._store.backfill_day(condition_id, date_str,
                                     open=o, high=h, low=lo, close=c)

        # 第 4 步：处理今天这条记录
        today_ohlc = by_day.get(today)
        if today_ohlc is None:
            return  # 今天还没有价格点，等下次再来
        o, h, lo, c = today_ohlc

        # 算成交量增量 = 当前累计 - 上次累计
        prev_vol = self._store.last_volume_num(condition_id)
        if cum_volume is not None and prev_vol is not None:
            # 正常情况：有 baseline，算增量
            # max(0, ...) 防止数据回退导致负成交量
            delta = max(0.0, cum_volume - prev_vol)
        else:
            # 第一次跑，没有 baseline，volume 记 0
            # 下一次跑的时候才能算出真实增量（这次的 cum_volume 存进去当 baseline）
            delta = 0.0

        # 第 5 步：写入今天的记录
        self._store.upsert_today(
            condition_id,
            volume_delta=delta,
            open=o, high=h, low=lo, close=c,
            last_volume_num=cum_volume,
        )

    # ── 两个 fetcher：拉远程数据 ──────────────────────────────────────

    async def _fetch_cumulative_volume(self, condition_id: str) -> float | None:
        """从 Gamma 拉累计成交量 volumeNum（从市场创建到现在的总成交量，USDC）。

        注意：这是累计值，不是当日值。两次采集相减 = 这段时间的增量。
        失败返回 None，不抛异常——后台采集器不能因为一次网络抖动就崩。
        """
        try:
            r = await self._gamma.get(
                "/markets",
                params={"condition_ids": condition_id, "limit": 1},
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            return float(data[0].get("volumeNum") or 0.0)
        except Exception as exc:  # 后台采集器，任何异常都不能让进程崩
            log.warning("stats_volume_fetch_failed", cid=condition_id[:8], err=str(exc))
            return None

    async def _fetch_price_history(self, token_id: str) -> list[dict[str, Any]]:
        """从 CLOB 拉历史价格序列，用于按天聚合 OHLC。

        用 startTs 指定往前拉多少天，不传 interval（两者互斥）。
        fidelity 控制数据点密度：每 2 小时一个点，一天 12 个点，足够算 OHLC。
        失败返回空列表，不抛异常。
        """
        import time as _time
        now = int(_time.time())
        start_ts = now - self._lookback_days * 86400
        # fidelity = 天数 * 12（每 2 小时一个点）
        fidelity = self._lookback_days * 12

        try:
            r = await self._clob.get(
                "/prices-history",
                params={
                    "market": token_id,
                    "startTs": start_ts,
                    "fidelity": fidelity,
                },
            )
            r.raise_for_status()
            return r.json().get("history", [])
        except Exception as exc:  # 后台采集器，任何异常都不能让进程崩
            log.warning("stats_price_fetch_failed", token=token_id[:12], err=str(exc))
            return []


# ── 工具函数 ────────────────────────────────────────────────────────────


def _utc_today() -> str:
    """返回 UTC 今天的日期字符串 'YYYY-MM-DD'。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_offset_date(offset_days: int) -> str:
    """返回 UTC 偏移 N 天的日期字符串。offset_days=-1 就是昨天。"""
    dt = datetime.now(timezone.utc) + timedelta(days=offset_days)
    return dt.strftime("%Y-%m-%d")


def _bucket_by_day(points: list[dict[str, Any]]) -> dict[str, tuple[float, float, float, float]]:
    """把小时价格点按 UTC 日期分组，算出每天的 OHLC。

    输入：[{"t": 时间戳, "p": 价格}, ...]
    输出：{"YYYY-MM-DD": (open, high, low, close), ...}
      - open  = 当天第一个价格
      - high  = 当天最高价格
      - low   = 当天最低价格
      - close = 当天最后一个价格
    坏数据（时间戳不是数字、价格不是数字）直接跳过，不崩。
    """
    buckets: dict[str, list[float]] = {}

    for pt in points:
        ts = pt.get("t")
        price = pt.get("p")
        if ts is None or price is None:
            continue
        try:
            ts_int = int(ts)
            price_f = float(price)
        except (TypeError, ValueError):
            continue  # 坏数据跳过

        # 时间戳转 UTC 日期字符串
        date_str = datetime.fromtimestamp(ts_int, tz=timezone.utc).strftime("%Y-%m-%d")
        buckets.setdefault(date_str, []).append(price_f)

    # 把每天的价格列表转成 (open, high, low, close)
    out: dict[str, tuple[float, float, float, float]] = {}
    for date_str, prices in buckets.items():
        if not prices:
            continue
        out[date_str] = (prices[0], max(prices), min(prices), prices[-1])
    return out
#（注：内容由AI生成）
