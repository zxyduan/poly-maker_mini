"""
调试：用 startTs + endTs 拉一周历史（不传 interval）。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import httpx

YES_TOKEN_ID = "35198549486569600595408965368290795524428922595732253580266052028158040373233"


async def main() -> None:
    now = int(datetime.now(timezone.utc).timestamp())

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 测试 1：只传 startTs（7 天前），不传 interval
        start_7d = now - 7 * 86400
        print("=== 只传 startTs=7天前，不传 interval ===")
        r = await client.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": YES_TOKEN_ID, "startTs": start_7d},
        )
        print(f"状态码: {r.status_code}")
        if r.status_code == 200:
            history = r.json().get("history", [])
            print(f"返回 {len(history)} 个点")
            if history:
                first_dt = datetime.fromtimestamp(history[0]["t"], tz=timezone.utc)
                last_dt = datetime.fromtimestamp(history[-1]["t"], tz=timezone.utc)
                days = (history[-1]["t"] - history[0]["t"]) / 86400
                print(f"最早: {first_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"最新: {last_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"跨度: {days:.1f} 天")
        else:
            print(r.text[:500])
        print()

        # 测试 2：startTs + endTs，14 天前到现在
        start_14d = now - 14 * 86400
        print("=== startTs=14天前 + endTs=现在 ===")
        r2 = await client.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": YES_TOKEN_ID, "startTs": start_14d, "endTs": now},
        )
        print(f"状态码: {r2.status_code}")
        if r2.status_code == 200:
            history = r2.json().get("history", [])
            print(f"返回 {len(history)} 个点")
            if history:
                first_dt = datetime.fromtimestamp(history[0]["t"], tz=timezone.utc)
                last_dt = datetime.fromtimestamp(history[-1]["t"], tz=timezone.utc)
                days = (history[-1]["t"] - history[0]["t"]) / 86400
                print(f"最早: {first_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"最新: {last_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"跨度: {days:.1f} 天")
        else:
            print(r2.text[:500])
        print()

        # 测试 3：startTs + endTs + fidelity（控制点数）
        print("=== startTs=14天前 + fidelity=168（每小时一个点）===")
        r3 = await client.get(
            "https://clob.polymarket.com/prices-history",
            params={"market": YES_TOKEN_ID, "startTs": start_14d, "fidelity": 168},
        )
        print(f"状态码: {r3.status_code}")
        if r3.status_code == 200:
            history = r3.json().get("history", [])
            print(f"返回 {len(history)} 个点")
            if history:
                first_dt = datetime.fromtimestamp(history[0]["t"], tz=timezone.utc)
                last_dt = datetime.fromtimestamp(history[-1]["t"], tz=timezone.utc)
                days = (history[-1]["t"] - history[0]["t"]) / 86400
                print(f"最早: {first_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"最新: {last_dt.strftime('%Y-%m-%d %H:%M UTC')}")
                print(f"跨度: {days:.1f} 天")
                # 打印前 5 个点，看看间隔
                print("前5个点时间戳:")
                for p in history[:5]:
                    dt = datetime.fromtimestamp(p["t"], tz=timezone.utc)
                    print(f"  {dt.strftime('%Y-%m-%d %H:%M')}  price={p['p']}")
        else:
            print(r3.text[:500])


if __name__ == "__main__":
    asyncio.run(main())
#（注：内容由AI生成）
