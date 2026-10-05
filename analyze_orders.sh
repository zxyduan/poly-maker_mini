#!/bin/bash
# 订单+快照联合分析数据导出脚本
# 用法: bash analyze_orders.sh
# 输出: order_analysis_output.txt

OUTPUT="order_analysis_output.txt"
DB="state.db"

echo "========================================" > $OUTPUT
echo " 订单+快照联合分析导出 - $(date '+%Y-%m-%d %H:%M:%S')" >> $OUTPUT
echo "========================================" >> $OUTPUT
echo "" >> $OUTPUT

echo "--- 1. 订单总览 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  COUNT(*) AS 总挂单数,
  SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) AS 成交数,
  ROUND(100.0 * SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS 成交率pct,
  SUM(CASE WHEN canceled_ts IS NOT NULL AND fill_ts IS NULL THEN 1 ELSE 0 END) AS 撤单数,
  SUM(CASE WHEN fill_ts IS NULL AND canceled_ts IS NULL THEN 1 ELSE 0 END) AS 仍挂着
FROM order_context;" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 2. 按方向+市场状态分 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  side AS 方向,
  regime AS 市场状态,
  COUNT(*) AS 挂单数,
  SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) AS 成交数,
  ROUND(100.0 * SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS 成交率pct,
  ROUND(AVG(price), 4) AS 平均挂单价,
  ROUND(AVG(size), 1) AS 平均单量,
  ROUND(AVG(our_offset_ticks), 1) AS 平均offset跳
FROM order_context
GROUP BY side, regime
ORDER BY side, 挂单数 DESC;" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 3. 买单 offset 分桶 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  CASE
    WHEN our_offset_ticks IS NULL THEN 'unknown'
    WHEN our_offset_ticks >= -0.5 THEN '0_贴touch'
    WHEN our_offset_ticks >= -2.5 THEN '-1~-2压1-2跳'
    WHEN our_offset_ticks >= -5.5 THEN '-3~-5压3-5跳'
    ELSE '<=-6深压'
  END AS offset区间,
  COUNT(*) AS 挂单数,
  SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) AS 成交数,
  ROUND(100.0 * SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS 成交率pct,
  ROUND(AVG(size), 1) AS 平均单量
FROM order_context
WHERE side='BUY'
GROUP BY offset区间
ORDER BY MIN(our_offset_ticks);" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 4. 按成交量系数档分（看 vol_factor 和成交率关系）---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  CASE
    WHEN vol_factor IS NULL THEN 'unknown'
    WHEN vol_factor < 0.5 THEN '0.3_死盘档'
    WHEN vol_factor < 0.9 THEN '0.7_小盘档'
    WHEN vol_factor < 1.2 THEN '1.0_正常档'
    ELSE '1.5_活跃档'
  END AS 成交量系数档,
  COUNT(*) AS 挂单数,
  SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) AS 成交数,
  ROUND(100.0 * SUM(CASE WHEN fill_ts IS NOT NULL THEN 1 ELSE 0 END) / COUNT(*), 1) AS 成交率pct,
  ROUND(AVG(size), 1) AS 平均单量,
  ROUND(AVG(vol_factor), 2) AS 实际系数
FROM order_context
GROUP BY 成交量系数档
ORDER BY MIN(vol_factor);" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 5. 快照表概况：每个市场有多少桶 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  slug AS 市场,
  condition_id AS condition_id,
  COUNT(*) AS 桶数,
  datetime(MIN(bucket_ts), 'unixepoch', 'localtime') AS 最早桶,
  datetime(MAX(bucket_ts), 'unixepoch', 'localtime') AS 最新桶,
  ROUND(AVG(volume_delta), 1) AS 平均桶成交量
FROM market_snapshots
GROUP BY condition_id
ORDER BY 桶数 DESC;" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 6. 快照数据：全部桶（按时间倒序）---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  datetime(bucket_ts, 'unixepoch', 'localtime') AS 时间桶,
  slug AS 市场,
  ROUND(price_close, 4) AS 收盘价,
  ROUND(volume_delta, 1) AS 桶成交量,
  trend AS 趋势,
  trend_streak AS 连向,
  ROUND(drawdown_from_high*100, 1) AS 回撤pct,
  ROUND(price_percentile*100, 0) AS 价格分位pct,
  ROUND(bid_depth_top5, 0) AS 买盘深度,
  vol_regime AS 波动率档
FROM market_snapshots
ORDER BY bucket_ts DESC;" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 7. 快照统计：最近12桶（24小时）平均成交量 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  COUNT(*) AS 桶数,
  ROUND(AVG(volume_delta), 1) AS 平均每桶成交量,
  ROUND(MIN(volume_delta), 1) AS 最小桶成交量,
  ROUND(MAX(volume_delta), 1) AS 最大桶成交量,
  ROUND(SUM(volume_delta), 1) AS 总成交量,
  SUM(CASE WHEN volume_delta < 5 THEN 1 ELSE 0 END) AS 死盘桶数,
  SUM(CASE WHEN volume_delta >= 5 AND volume_delta < 20 THEN 1 ELSE 0 END) AS 小盘桶数,
  SUM(CASE WHEN volume_delta >= 20 AND volume_delta < 100 THEN 1 ELSE 0 END) AS 正常桶数,
  SUM(CASE WHEN volume_delta >= 100 THEN 1 ELSE 0 END) AS 活跃桶数
FROM (
  SELECT volume_delta FROM market_snapshots ORDER BY bucket_ts DESC LIMIT 12
);" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "--- 8. 最近 50 笔订单明细 ---" >> $OUTPUT
sqlite3 -header -column $DB "
SELECT 
  datetime(placed_ts, 'unixepoch', 'localtime') AS 时间,
  side AS 方向,
  ROUND(price, 4) AS 价格,
  ROUND(size, 1) AS 数量,
  regime AS 状态,
  ROUND(our_offset_ticks, 0) AS offset跳,
  ROUND(fv, 4) AS FV,
  ROUND(toxicity, 2) AS tox,
  ROUND(vol_factor, 2) AS vol系数,
  CASE 
    WHEN fill_ts IS NOT NULL THEN '成交'
    WHEN canceled_ts IS NOT NULL THEN '撤单'
    ELSE '挂着'
  END AS 结果,
  CASE WHEN fill_ts IS NOT NULL THEN ROUND(fill_ts - placed_ts, 0)
       WHEN canceled_ts IS NOT NULL THEN ROUND(canceled_ts - placed_ts, 0)
       ELSE ROUND(strftime('%s','now') - placed_ts, 0)
  END AS 存活秒
FROM order_context
ORDER BY placed_ts DESC
LIMIT 50;" >> $OUTPUT 2>&1
echo "" >> $OUTPUT

echo "========================================" >> $OUTPUT
echo " 导出完成: $OUTPUT" >> $OUTPUT
echo "========================================"

cat $OUTPUT
#（注：内容由AI生成）
