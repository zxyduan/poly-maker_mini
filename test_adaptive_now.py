from polymaker.config import Config
from polymaker.catalog.snapshots import SnapshotStore
from polymaker.catalog.adaptive import AdaptiveEngine

cfg = Config.load("config")
store = SnapshotStore(cfg.paths.db)
engine = AdaptiveEngine(store)

# 拿第一个市场的 condition_id
conn = store._conn
row = conn.execute("SELECT DISTINCT condition_id FROM market_snapshots LIMIT 1").fetchone()
cid = row["condition_id"]

# 拿基础 profile
base = list(cfg.profiles.values())[0]
print(f"测试市场: {cid[:12]}...")
print(f"基础 profile: {base.type}")
print(f"基础 base_size_usdc: {base.base_size_usdc}")

result = engine.get_profile(cid, base)
p = result.profile
print(f"调整后 base_size_usdc: {p.base_size_usdc}")
print(f"调整后 ow_wall_min_gap_ticks: {p.ow_wall_min_gap_ticks}")
print(f"调整后 ow_panic_toxicity: {p.ow_panic_toxicity}")
print(f"just_switched: {result.just_switched}")
