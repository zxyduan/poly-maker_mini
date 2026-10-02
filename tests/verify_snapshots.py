#!/usr/bin/env python3
"""快照数据全面验证脚本：对比数据库值和实时 API 拉的值。"""

import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

DB_PATH = Path("state.db")
CLOB_BOOK_URL = "https://clob.polymarket.com/book?token_id={token_id}"
CLOB_PRICE_URL = "https://clob.polymarket.com/prices-history?market={market}&startTs={start}&fidelity=60"


def get_market_info(conn: sqlite3.Connection, condition_id: str) -> Tuple[Optional[str], Optional[str]]:
    """从 markets 表拿 slug 和 yes_token_id（从 meta_json 里解析）。"""
    row = conn.execute(
        "SELECT slug, meta_json FROM markets WHERE condition_id=?",
        (condition_id,),
    ).fetchone()
    if not row:
        return None, None
    import json
    try:
        meta = json.loads(row[1]) if row[1] else {}
        tokens = meta.get("tokens", [])
        # 找 outcome == "Yes" 的 token
        yes_token = None
        for t in tokens:
            if t.get("outcome", "").lower() == "yes":
                yes_token = t.get("token_id")
                break
        # 找不到就取第一个
        if not yes_token and tokens:
            yes_token = tokens[0].get("token_id")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        yes_token = None
    return row[0], yes_token


def fetch_book_depth(token_id: str) -> tuple[float, float, int]:
    """实时拉订单簿，算前 5 档深度。"""
    r = requests.get(CLOB_BOOK_URL.format(token_id=token_id), timeout=10)
    data = r.json()

    bids = []
    for level in data.get("bids", []):
        try:
            price = float(level["price"])
            size = float(level["size"])
            bids.append((price, size))
        except (KeyError, ValueError):
            continue

    asks = []
    for level in data.get("asks", []):
        try:
            price = float(level["price"])
            size = float(level["size"])
            asks.append((price, size))
        except (KeyError, ValueError):
            continue

    bids.sort(key=lambda x: x[0], reverse=True)
    asks.sort(key=lambda x: x[0])

    bid_top5 = sum(p * s for p, s in bids[:5])
    ask_top5 = sum(p * s for p, s in asks[:5])
    bid_walls = sum(1 for p, s in bids[:5] if p * s >= 100)

    return bid_top5, ask_top5, bid_walls


def fetch_recent_price_bucket(token_id: str, bucket_ts: int) -> Optional[dict]:
    """拉最近 3 小时价格，提取指定桶的 OHLC。"""
    start = bucket_ts - 3600  # 往前多拉1小时
    r = requests.get(
        CLOB_PRICE_URL.format(market=token_id, start=start),
        timeout=10,
    )
    data = r.json().get("history", [])

    bucket_size = 7200  # 2小时
    prices_in_bucket = []
    for pt in data:
        ts = pt.get("t")
        price = pt.get("p")
        if ts is None or price is None:
            continue
        pt_bucket = (int(ts) // bucket_size) * bucket_size
        if pt_bucket == bucket_ts:
            prices_in_bucket.append(float(price))

    if not prices_in_bucket:
        return None

    return {
        "open": prices_in_bucket[0],
        "close": prices_in_bucket[-1],
        "high": max(prices_in_bucket),
        "low": min(prices_in_bucket),
    }


def main():
    if not DB_PATH.exists():
        print(f"数据库文件不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 拿每个市场最新一条快照
    rows = conn.execute(
        """SELECT s.* FROM market_snapshots s
           INNER JOIN (
               SELECT condition_id, MAX(bucket_ts) as max_ts
               FROM market_snapshots
               GROUP BY condition_id
           ) latest ON s.condition_id = latest.condition_id AND s.bucket_ts = latest.max_ts"""
    ).fetchall()

    if not rows:
        print("表里还没有数据")
        sys.exit(0)

    print(f"共 {len(rows)} 个市场，开始验证...\n")

    for row in rows:
        cid = row["condition_id"]
        slug = row["slug"]
        bucket_ts = row["bucket_ts"]

        market_slug, token_id = get_market_info(conn, cid)
        if not token_id:
            print(f"⚠️  {slug}: 找不到 token_id，跳过")
            continue

        print(f"=== {slug} ===")
        print(f"  桶时间: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(bucket_ts))}")
        print()

        # ── 1. 验证价格 OHLC ──
        real_ohlc = fetch_recent_price_bucket(token_id, bucket_ts)
        if real_ohlc:
            print("  📊 价格 OHLC 对比：")
            print(f"     open:  DB={row['price_open']:.4f}  实时={real_ohlc['open']:.4f}  差={abs(row['price_open']-real_ohlc['open']):.4f}")
            print(f"     close: DB={row['price_close']:.4f}  实时={real_ohlc['close']:.4f}  差={abs(row['price_close']-real_ohlc['close']):.4f}")
            print(f"     high:  DB={row['price_high']:.4f}  实时={real_ohlc['high']:.4f}  差={abs(row['price_high']-real_ohlc['high']):.4f}")
            print(f"     low:   DB={row['price_low']:.4f}  实时={real_ohlc['low']:.4f}  差={abs(row['price_low']-real_ohlc['low']):.4f}")
        else:
            print("  📊 价格: 该桶没有历史数据点")
        print()

        # ── 2. 验证盘口深度 ──
        real_bid, real_ask, real_walls = fetch_book_depth(token_id)

        bid_diff_pct = abs(row["bid_depth_top5"] - real_bid) / real_bid * 100 if real_bid > 0 else 0
        ask_diff_pct = abs(row["ask_depth_top5"] - real_ask) / real_ask * 100 if real_ask > 0 else 0

        print("  📚 盘口深度对比：")
        print(f"     买盘 top5: DB=${row['bid_depth_top5']:,.2f}  实时=${real_bid:,.2f}  差={bid_diff_pct:.1f}%")
        print(f"     卖盘 top5: DB=${row['ask_depth_top5']:,.2f}  实时=${real_ask:,.2f}  差={ask_diff_pct:.1f}%")
        print(f"     买盘墙数: DB={row['bid_wall_count']}  实时={real_walls}")
        print()

        # ── 3. 特征检查 ──
        print("  📈 特征值：")
        print(f"     trend={row['trend']}  trend_streak={row['trend_streak']}")
        print(f"     trend_direction={row['trend_direction']}")
        print(f"     drawdown_from_high={row['drawdown_from_high']:.4f}")
        print(f"     price_percentile={row['price_percentile']:.4f}")
        print(f"     vol_regime={row['vol_regime']}")
        print()
        print()

    # ── 数据量统计 ──
    print("=" * 60)
    print("📊 全表数据量统计：")
    total = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    print(f"  总记录数: {total}")

    market_count = conn.execute("SELECT COUNT(DISTINCT condition_id) FROM market_snapshots").fetchone()[0]
    print(f"  市场数量: {market_count}")

    oldest = conn.execute("SELECT MIN(datetime(bucket_ts, 'unixepoch')) FROM market_snapshots").fetchone()[0]
    newest = conn.execute("SELECT MAX(datetime(bucket_ts, 'unixepoch')) FROM market_snapshots").fetchone()[0]
    print(f"  最早数据: {oldest} UTC")
    print(f"  最新数据: {newest} UTC")

    conn.close()


if __name__ == "__main__":
    main()
#（注：内容由AI生成）
