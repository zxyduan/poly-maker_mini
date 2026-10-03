from polymaker.config import Config
cfg = Config.load("config")

print(f"one_way_adaptive.enabled: {cfg.one_way_adaptive.enabled}")
print(f"recalc_interval_s: {cfg.one_way_adaptive.recalc_interval_s}")
print()
print("所有 profiles:")
for name, p in cfg.profiles.items():
    print(f"  {name}: type={p.type}")
