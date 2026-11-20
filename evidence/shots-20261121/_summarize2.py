import json, collections
P = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/measurements.json"
d = json.load(open(P, encoding="utf-8"))
keys = [k for k in d if not k.startswith("_")]

# ---- 1. 真实（非嵌套）重叠：重新过滤 ancestor 关系 ----
print("########## 真实重叠（排除祖先/后代）##########")
real_overlaps = collections.defaultdict(list)
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    for o in m.get("cardOverlaps", []):
        real_overlaps[k].append(o)
    # page-level: 过滤掉 jobsArea(父) vs jobsHead/jobsTabs/jobsGrid(子)
    for o in m.get("overlaps", []):
        if o["a"] == "jobsArea" and o["b"] in ("jobsHead","jobsTabs","jobsGrid"): continue
        if o["b"] == "jobsArea" and o["a"] in ("jobsHead","jobsTabs","jobsGrid"): continue
        if o["a"] == "jobsHead" and o["b"] == "jobsTabs": continue
        real_overlaps[k].append({"PAGE": o})
for k, v in real_overlaps.items():
    if v:
        print(f"--- {k}")
        for o in v[:8]: print("   ", o)

# ---- 2. 对比度去重（按 selector + ratio）----
print("\n########## 对比度不足（跨页去重，按 selector）##########")
agg = {}
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    for c in m.get("lowContrast", []):
        sig = c["sel"]
        rec = agg.setdefault(sig, {"minRatio": 99, "ratio": c["ratio"], "need": c["need"],
                                   "fontSize": c["fontSize"], "weight": c["weight"],
                                   "color": c["color"], "bg": c["bg"], "txt": c["txt"], "pages": []})
        if c["ratio"] < rec["minRatio"]:
            rec["minRatio"] = c["ratio"]; rec["ratio"] = c["ratio"]; rec["need"] = c["need"]
            rec["fontSize"] = c["fontSize"]; rec["weight"] = c["weight"]
            rec["color"] = c["color"]; rec["bg"] = c["bg"]; rec["txt"] = c["txt"]
        if k not in rec["pages"]: rec["pages"].append(k)
rows = sorted(agg.items(), key=lambda kv: kv[1]["minRatio"])
print(f"unique selectors with low contrast: {len(rows)}")
for sig, r in rows[:45]:
    print(f"  {r['minRatio']:>5} (need {r['need']}) fs={r['fontSize']} w={r['weight']} {sig}  color={r['color']} bg={r['bg']}  :: {r['txt'][:34]}  pages={len(r['pages'])} eg={r['pages'][0]}")

# ---- 3. 文本截断：跨页去重 ----
print("\n########## 文本溢出容器（scrollWidth>clientWidth，跨页去重）##########")
agg2 = {}
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    for c in m.get("clipped", []):
        sig = c["sel"]
        rec = agg2.setdefault(sig, {"maxDx": 0, "dx": 0, "clientW": 0, "scrollW": 0,
                                    "overflowX": c["overflowX"], "whiteSpace": c["whiteSpace"],
                                    "textOverflow": c["textOverflow"], "txt": c["txt"], "pages": []})
        if c["dx"] > rec["maxDx"]:
            rec["maxDx"] = c["dx"]; rec["dx"] = c["dx"]; rec["clientW"] = c["clientW"]
            rec["scrollW"] = c["scrollW"]; rec["overflowX"] = c["overflowX"]
            rec["whiteSpace"] = c["whiteSpace"]; rec["textOverflow"] = c["textOverflow"]; rec["txt"] = c["txt"]
        if k not in rec["pages"]: rec["pages"].append(k)
rows2 = sorted(agg2.items(), key=lambda kv: -kv[1]["maxDx"])
print(f"unique clipped containers: {len(rows2)}")
for sig, r in rows2[:50]:
    print(f"  dx={r['maxDx']:>5} ({r['clientW']}->{r['scrollW']}) ovx={r['overflowX']} ws={r['whiteSpace']} te={r['textOverflow']}  {sig} :: {r['txt'][:36]}  pages={len(r['pages'])} eg={r['pages'][0]}")

# ---- 4. 小触控目标去重 ----
print("\n########## 触控目标 <44px（跨页去重）##########")
agg3 = {}
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    for s in m.get("smallTargets", []):
        sig = s["sel"]
        rec = agg3.setdefault(sig, {"min": None, "w": s["w"], "h": s["h"], "txt": s["txt"], "pages": []})
        if rec["min"] is None or s["w"]*s["h"] < rec["min"]:
            rec["min"] = s["w"]*s["h"]; rec["w"] = s["w"]; rec["h"] = s["h"]; rec["txt"] = s["txt"]
        if k not in rec["pages"]: rec["pages"].append(k)
rows3 = sorted(agg3.items(), key=lambda kv: kv[1]["min"])
print(f"unique small targets: {len(rows3)}")
for sig, r in rows3[:55]:
    print(f"  {r['w']}x{r['h']}  {sig} :: {r['txt'][:28]}  pages={len(r['pages'])} eg={r['pages'][0]}")

# ---- 5. nav scroll overflow ----
print("\n########## nav-scroll 溢出 ##########")
seen=set()
for k in keys:
    m = d[k].get("metrics")
    if not m: continue
    no = m.get("navOverflow")
    if no:
        t = (no["scrollW"], no["clientW"], no["scrollH"], no["clientH"])
        if t not in seen:
            seen.add(t); print("  ", k, no)
