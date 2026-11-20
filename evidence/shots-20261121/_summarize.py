import json, sys
P = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/measurements.json"
d = json.load(open(P, encoding="utf-8"))
keys = [k for k in d if not k.startswith("_")]
print("PAGES:", len(keys))
print("\n########## 横向溢出汇总 ##########")
for k in keys:
    m = d[k].get("metrics")
    if not m: 
        print(f"{k}: METRICS-ERR {d[k].get('metrics_error')}"); continue
    h = m["hOverflow"]
    flag = "OVERFLOW" if h["hasHScroll"] else "ok"
    print(f"\n--- {k} [{flag}] docSW={h['docScrollW']} innerW={h['innerW']} bodySW={h['bodyScrollW']}")
    for o in h["offenders"][:10]:
        print(f"    {o['sel']}  right={o['right']} w={o['w']} overR={o['overRight']} overL={o['overLeft']} :: {o['txt'][:32]}")
print("\n\n########## 重叠汇总 ##########")
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    if m["overlaps"] or m["cardOverlaps"]:
        print(f"\n--- {k}")
        for o in m["overlaps"][:8]: print("   PAGE-OVERLAP", o)
        for o in m["cardOverlaps"][:8]: print("   CARD-OVERLAP", o)
print("\n\n########## 对比度不足汇总 ##########")
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    if m["lowContrastCount"]:
        print(f"\n--- {k}  count={m['lowContrastCount']}")
        seen=set()
        for c in m["lowContrast"][:14]:
            key=(c["sel"],c["ratio"])
            if key in seen: continue
            seen.add(key)
            print(f"    {c['sel']} ratio={c['ratio']} (need {c['need']}) fs={c['fontSize']} w={c['weight']} color={c['color']} bg={c['bg']} :: {c['txt'][:32]}")
print("\n\n########## 小触控目标汇总 ##########")
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    if m["smallTargetCount"]:
        print(f"\n--- {k}  count={m['smallTargetCount']}")
        for s in m["smallTargets"][:18]:
            print(f"    {s['sel']} {s['w']}x{s['h']} :: {s['txt'][:26]}")
print("\n\n########## console errors ##########")
for e in d.get("_consoleErrors", [])[:20]: print("  ", e)
print("\n\n########## perPageOptions ##########")
print(d.get("_perPageOptions"))
