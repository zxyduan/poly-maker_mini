#!/bin/bash
echo "=== 1. 进程状态 ==="
ps aux | grep snapshot-collector | grep -v grep || echo "❌ 采集器没在跑！"
echo ""

echo "=== 2. 最新数据时间 ==="
sqlite3 state.db "SELECT slug, datetime(MAX(bucket_ts), 'unixepoch') as latest_bucket FROM market_snapshots GROUP BY slug;" -column
echo ""

echo "=== 3. 最新采集时间 ==="
sqlite3 state.db "SELECT slug, datetime(MAX(collected_ts), 'unixepoch') as last_collect FROM market_snapshots GROUP BY slug;" -column
echo ""

echo "=== 4. 数据总量 ==="
sqlite3 state.db "SELECT COUNT(*) as total_rows, COUNT(DISTINCT condition_id) as markets FROM market_snapshots;" -column
