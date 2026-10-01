"""
每日市场统计采集器的单元测试。

跑法：uv run pytest tests/test_daily_stats.py -v

测试覆盖：
  - _bucket_by_day 纯函数：分组、跨天、空输入、坏数据
  - DailyStatsStore 读写：upsert、backfill、freeze、recent_avg
  - DailyStatsCollector 集成：mock HTTP 后跑完整采集流程
"""

from __future__ import annotations

import sqlite3 as sql
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from polymaker.catalog.daily_stats import (
    DailyStatsCollector,
    DailyStatsStore,
    _bucket_by_day,
    _utc_today,
)


# ── fixture：每个测试用一个临时数据库，互不影响 ───────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> DailyStatsStore:
    """创建一个临时数据库的 Store，测试结束自动清理。"""
    db = tmp_path / "test_state.db"
    s = DailyStatsStore(db)
    yield s
    s.close()


# ── _bucket_by_day：纯函数测试 ──────────────────────────────────────────


class TestBucketByDay:
    """测试小时价格点按天分组的逻辑。"""

    def test_basic_single_day(self) -> None:
        """同一天的 4 个小时点，应该算出正确的 open/high/low/close。"""
        # 2026-10-01 10:00 到 13:00 UTC，每小时一个价格
        base = int(datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp())
        points = [
            {"t": base, "p": 0.10},
            {"t": base + 3600, "p": 0.12},   # 最高
            {"t": base + 7200, "p": 0.09},   # 最低
            {"t": base + 10800, "p": 0.11},
        ]
        result = _bucket_by_day(points)
        assert "2026-10-01" in result
        o, h, lo, c = result["2026-10-01"]
        assert o == 0.10   # 第一个价格 = 开盘
        assert h == 0.12   # 最大值 = 最高
        assert lo == 0.09  # 最小值 = 最低
        assert c == 0.11   # 最后一个价格 = 收盘

    def test_multi_day_split(self) -> None:
        """跨天的点应该分到不同的日期组里。"""
        # 9月30日 22:00 和 23:00，10月1日 02:00 和 03:00
        day1 = int(datetime(2026, 9, 30, 22, tzinfo=timezone.utc).timestamp())
        day2 = int(datetime(2026, 10, 1, 2, tzinfo=timezone.utc).timestamp())
        points = [
            {"t": day1, "p": 0.20},
            {"t": day1 + 3600, "p": 0.22},   # 还是 9/30
            {"t": day2, "p": 0.18},          # 进入 10/1
            {"t": day2 + 3600, "p": 0.19},
        ]
        result = _bucket_by_day(points)
        assert set(result.keys()) == {"2026-09-30", "2026-10-01"}
        assert result["2026-09-30"] == (0.20, 0.22, 0.20, 0.22)
        assert result["2026-10-01"] == (0.18, 0.19, 0.18, 0.19)

    def test_empty_input(self) -> None:
        """空输入返回空字典。"""
        assert _bucket_by_day([]) == {}

    def test_malformed_points_skipped(self) -> None:
        """坏数据（t 是 None、p 是 None、t 不是数字）应该被跳过，不崩。"""
        base = int(datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp())
        points = [
            {"t": base, "p": 0.10},          # 正常
            {"t": None, "p": 0.20},          # t 坏
            {"t": base + 3600, "p": None},   # p 坏
            {"t": "not-a-number", "p": 0.3}, # t 类型坏
        ]
        result = _bucket_by_day(points)
        assert len(result) == 1  # 只有一条有效
        assert result["2026-10-01"] == (0.10, 0.10, 0.10, 0.10)


# ── DailyStatsStore：写操作测试 ─────────────────────────────────────────


class TestStoreUpsert:
    """测试 upsert_today：第一次写、累加写、last_volume_num。"""

    def test_first_upsert_creates_row(self, store: DailyStatsStore) -> None:
        """第一次写入，应该正确创建一条今天的记录。"""
        store.upsert_today(
            "0xabc",
            volume_delta=100.0,
            open=0.10, high=0.12, low=0.09, close=0.11,
            last_volume_num=5000.0,
        )
        rows = store.history("0xabc")
        assert len(rows) == 1
        r = rows[0]
        assert r.date == _utc_today()
        assert r.volume_usdc == 100.0
        assert r.price_open == 0.10
        assert r.price_high == 0.12
        # range_pct = (0.12 - 0.09) / 0.10 = 0.3
        assert r.range_pct == pytest.approx((0.12 - 0.09) / 0.10)
        assert r.frozen is False  # 今天不冻结

    def test_second_upsert_accumulates_volume(self, store: DailyStatsStore) -> None:
        """第二次写入，成交量应该累加，OHLC 应该覆盖为最新值。"""
        # 第 1 小时：累计成交量 5000，增量 100
        store.upsert_today("0xabc", volume_delta=100.0,
                           open=0.10, high=0.12, low=0.09, close=0.11,
                           last_volume_num=5000.0)
        # 第 2 小时：累计成交量 5400，增量 400
        store.upsert_today("0xabc", volume_delta=400.0,
                           open=0.10, high=0.13, low=0.09, close=0.12,
                           last_volume_num=5400.0)
        rows = store.history("0xabc")
        assert len(rows) == 1  # 还是一条（同一天）
        assert rows[0].volume_usdc == 500.0   # 100 + 400 = 500
        assert rows[0].price_high == 0.13      # OHLC 被最新快照覆盖

    def test_last_volume_num(self, store: DailyStatsStore) -> None:
        """last_volume_num 应该正确返回最近一次的累计值。"""
        assert store.last_volume_num("0xabc") is None  # 空市场
        store.upsert_today("0xabc", volume_delta=0.0,
                           open=0.1, high=0.1, low=0.1, close=0.1,
                           last_volume_num=9999.0)
        assert store.last_volume_num("0xabc") == 9999.0


class TestStoreBackfill:
    """测试 backfill_day：历史回填、不覆盖已有数据。"""

    def test_backfill_historical_day(self, store: DailyStatsStore) -> None:
        """回填昨天的记录，应该自动标记为 frozen。"""
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        store.backfill_day("0xabc", yesterday,
                           open=0.10, high=0.15, low=0.08, close=0.12)
        rows = store.history("0xabc")
        assert len(rows) == 1
        assert rows[0].date == yesterday
        assert rows[0].frozen is True          # 历史天自动冻结
        assert rows[0].volume_usdc == 0.0      # 历史天没有成交量 baseline

    def test_backfill_does_not_overwrite(self, store: DailyStatsStore) -> None:
        """重复回填同一天，不应该覆盖已有数据（INSERT OR IGNORE）。"""
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        # 第一次回填
        store.backfill_day("0xabc", yesterday,
                           open=0.10, high=0.15, low=0.08, close=0.12)
        # 第二次回填不同的值
        store.backfill_day("0xabc", yesterday,
                           open=0.20, high=0.30, low=0.15, close=0.25)
        r = store.history("0xabc")[0]
        assert r.price_open == 0.10   # 还是第一次的值，没被覆盖


class TestStoreFreeze:
    """测试 freeze_yesterday：把昨天标记为冻结。"""

    def test_freeze_yesterday(self, store: DailyStatsStore) -> None:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        # 手动插一条未冻结的昨天记录
        conn = sql.connect(store.path)
        conn.execute(
            "INSERT INTO market_daily_stats (condition_id, date, volume_usdc, collected_ts, frozen) "
            "VALUES (?, ?, 100.0, 0.0, 0)",
            ("0xabc", yesterday),
        )
        conn.commit()
        conn.close()

        n = store.freeze_yesterday()
        assert n == 1  # 冻结了 1 条
        rows = store.history("0xabc")
        assert rows[0].frozen is True


# ── DailyStatsStore：读操作测试 ─────────────────────────────────────────


class TestRecentAvg:
    """测试 recent_avg：只算 frozen 的记录，排除今天。"""

    def test_avg_only_includes_frozen(self, store: DailyStatsStore) -> None:
        """3 条 frozen 记录，应该算平均成交量和平均振幅。"""
        d1 = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        d2 = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        d3 = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        # range_pct = (high - low) / open
        store.backfill_day("0xabc", d1, open=0.10, high=0.12, low=0.09, close=0.11)  # 0.3
        store.backfill_day("0xabc", d2, open=0.20, high=0.22, low=0.18, close=0.20)  # 0.2
        store.backfill_day("0xabc", d3, open=0.30, high=0.36, low=0.27, close=0.33)  # 0.3

        avg_vol, avg_range = store.recent_avg("0xabc", days=3)
        # 回填的记录 volume_usdc 都是 0
        assert avg_vol == 0.0
        # 平均振幅 = (0.3 + 0.2 + 0.3) / 3
        assert avg_range == pytest.approx((0.3 + 0.2 + 0.3) / 3)

    def test_avg_excludes_today(self, store: DailyStatsStore) -> None:
        """今天的记录不应该被算进 recent_avg（因为没 frozen）。"""
        store.upsert_today("0xabc", volume_delta=1000.0,
                           open=0.10, high=0.20, low=0.05, close=0.15,
                           last_volume_num=10000.0)
        avg_vol, avg_range = store.recent_avg("0xabc", days=3)
        assert avg_vol == 0.0     # 没有 frozen 的记录
        assert avg_range == 0.0

    def test_avg_empty_market(self, store: DailyStatsStore) -> None:
        """空市场应该返回 (0.0, 0.0)，不报错。"""
        avg_vol, avg_range = store.recent_avg("0xnonexistent")
        assert avg_vol == 0.0
        assert avg_range == 0.0


# ── DailyStatsCollector：集成测试（mock HTTP）─────────────────────────────


class TestCollector:
    """测试采集器完整流程。HTTP 请求全部 mock，不打真实 API。"""

    @pytest.fixture
    def collector(self, store: DailyStatsStore) -> DailyStatsCollector:
        c = DailyStatsCollector(store)
        # 把 httpx client 换成 mock
        c._gamma = AsyncMock()
        c._clob = AsyncMock()
        return c

    @pytest.mark.asyncio
    async def test_first_collect_no_volume_baseline(self, collector: DailyStatsCollector) -> None:
        """第一次采集：没有 baseline，volume_delta 应该是 0。"""
        # mock Gamma 返回累计成交量 5000
        gamma_resp = MagicMock()
        gamma_resp.json.return_value = [{"volumeNum": 5000.0}]
        gamma_resp.raise_for_status = MagicMock()
        collector._gamma.get = AsyncMock(return_value=gamma_resp)

        # mock CLOB 返回今天的小时价格
        now = datetime.now(timezone.utc)
        today_ts = int(now.replace(hour=10, minute=0, second=0).timestamp())
        clob_resp = MagicMock()
        clob_resp.json.return_value = {
            "history": [
                {"t": today_ts, "p": 0.10},
                {"t": today_ts + 3600, "p": 0.11},
                {"t": today_ts + 7200, "p": 0.12},
            ]
        }
        clob_resp.raise_for_status = MagicMock()
        collector._clob.get = AsyncMock(return_value=clob_resp)

        await collector.collect("0xabc", "yes-token-123")

        rows = collector._store.history("0xabc")
        assert len(rows) == 1
        assert rows[0].volume_usdc == 0.0   # 第一次跑，没有 baseline
        assert rows[0].price_open == 0.10
        assert rows[0].price_close == 0.12

    @pytest.mark.asyncio
    async def test_second_collect_computes_delta(self, collector: DailyStatsCollector) -> None:
        """第二次采集：有 baseline，应该算出正确的成交量增量。"""
        # 先模拟第一次跑完，DB 里已经有 last_volume_num = 5000
        collector._store.upsert_today(
            "0xabc", volume_delta=0.0,
            open=0.10, high=0.12, low=0.10, close=0.11,
            last_volume_num=5000.0,
        )

        # 现在累计成交量涨到 5400
        gamma_resp = MagicMock()
        gamma_resp.json.return_value = [{"volumeNum": 5400.0}]
        gamma_resp.raise_for_status = MagicMock()
        collector._gamma.get = AsyncMock(return_value=gamma_resp)

        now = datetime.now(timezone.utc)
        today_ts = int(now.replace(hour=10, minute=0, second=0).timestamp())
        clob_resp = MagicMock()
        clob_resp.json.return_value = {
            "history": [
                {"t": today_ts, "p": 0.10},
                {"t": today_ts + 3600, "p": 0.13},
            ]
        }
        clob_resp.raise_for_status = MagicMock()
        collector._clob.get = AsyncMock(return_value=clob_resp)

        await collector.collect("0xabc", "yes-token-123")

        rows = collector._store.history("0xabc")
        # 5400 - 5000 = 400，累加到原来的 0 上
        assert rows[0].volume_usdc == 400.0

    @pytest.mark.asyncio
    async def test_backfills_historical_days(self, collector: DailyStatsCollector) -> None:
        """价格历史里有过去的天数，应该自动回填（冷启动）。"""
        gamma_resp = MagicMock()
        gamma_resp.json.return_value = [{"volumeNum": 1000.0}]
        gamma_resp.raise_for_status = MagicMock()
        collector._gamma.get = AsyncMock(return_value=gamma_resp)

        # 价格点：2 天前有 2 个点，今天有 2 个点
        d2 = int((datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=12).timestamp())
        today_ts = int(datetime.now(timezone.utc).replace(hour=10).timestamp())
        clob_resp = MagicMock()
        clob_resp.json.return_value = {
            "history": [
                {"t": d2, "p": 0.08},
                {"t": d2 + 3600, "p": 0.09},
                {"t": today_ts, "p": 0.10},
                {"t": today_ts + 3600, "p": 0.11},
            ]
        }
        clob_resp.raise_for_status = MagicMock()
        collector._clob.get = AsyncMock(return_value=clob_resp)

        await collector.collect("0xabc", "yes-token-123")

        rows = collector._store.history("0xabc")
        assert len(rows) == 2  # 今天 + 2 天前回填的
        by_date = {r.date: r for r in rows}
        d2_str = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        assert d2_str in by_date
        assert by_date[d2_str].frozen is True    # 历史天自动冻结
        assert by_date[d2_str].price_open == 0.08

    @pytest.mark.asyncio
    async def test_gamma_failure_no_crash(self, collector: DailyStatsCollector) -> None:
        """Gamma 拉取失败，不应该崩，OHLC 照常写，volume=0。"""
        collector._gamma.get = AsyncMock(side_effect=Exception("network down"))

        now = datetime.now(timezone.utc)
        today_ts = int(now.replace(hour=10).timestamp())
        clob_resp = MagicMock()
        clob_resp.json.return_value = {"history": [{"t": today_ts, "p": 0.10}]}
        clob_resp.raise_for_status = MagicMock()
        collector._clob.get = AsyncMock(return_value=clob_resp)

        # 不应该抛异常
        await collector.collect("0xabc", "yes-token-123")

        rows = collector._store.history("0xabc")
        assert len(rows) == 1
        assert rows[0].price_open == 0.10
        assert rows[0].volume_usdc == 0.0  # Gamma 挂了，没有成交量数据

    @pytest.mark.asyncio
    async def test_price_history_failure_writes_nothing(self, collector: DailyStatsCollector) -> None:
        """CLOB 价格历史拉取失败，应该什么都不写。"""
        collector._gamma.get = AsyncMock(return_value=MagicMock(json=lambda: [{"volumeNum": 1000}]))
        collector._clob.get = AsyncMock(side_effect=Exception("clob down"))

        await collector.collect("0xabc", "yes-token-123")

        rows = collector._store.history("0xabc")
        assert len(rows) == 0  # 没有价格数据，直接返回，什么都不写
#（注：内容由AI生成）
