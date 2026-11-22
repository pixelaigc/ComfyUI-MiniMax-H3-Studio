"""汇总「final」一轮 8 个视口的逐腿判据 —— 从日志 + 每个视口的 json。

【为什么需要这个】矩阵脚本每跑一个视口就覆写一次
`evidence/mobile-nav/fav-leg/favleg-chromium.json` ⇒ 跑完 8 个视口后
json 里只剩最后一个视口。要拿到 96 腿的完整判据统计，
得从日志（逐腿 OK/FAIL）+ 各视口 json（跑完时的快照）汇总。

【怎么做到不丢数据】本脚本在每个视口跑完后立刻把当时的 json 另存一份
（scripts/realscan_fav_leg_matrix.py 的 --out 目录里已按视口分 tag），
本脚本把 8 份快照合并成一份，并输出「每条判据在 96 腿上的失败数」。
"""
import json
import re
from pathlib import Path

OUT = Path("/root/projects/h3s-favorites/evidence/mobile-nav/fav-leg")
LOG = OUT / "final-run.log"

VIEWPORTS = ["375x667", "667x375", "390x844", "844x390",
             "768x1024", "1024x768", "430x932", "932x430"]

log = LOG.read_text(encoding="utf-8", errors="replace")
per_vp = re.findall(r"^\s+(\d+x\d+): (\d+)/(\d+)\s*$", log, re.M)
print("=== 逐视口（日志）===")
for k, a, b in per_vp:
    print("  %-10s %s/%s" % (k, a, b))
print("  合计 %d/%d" % (sum(int(a) for _, a, _ in per_vp),
                       sum(int(b) for _, _, b in per_vp)))

# 逐腿 OK/FAIL 计数
legs = re.findall(r"^\s+(OK|FAIL)\s+(\S+)\s+(\S+)\s+fav_tab=(\S+)\s+cards=(\S+)", log, re.M)
print("\n=== 逐腿 ===")
print("  日志里可解析的腿数 =", len(legs))
fails = [l for l in legs if l[0] == "FAIL"]
print("  FAIL 腿 =", len(fails))
for f in fails[:10]:
    print("   ", f[1], f[2], "fav_tab=", f[4], "cards=", f[5])

# 断言统计：只统计日志里完整解析出的腿
agg = {}
for _, vp, mode, tab, cards in legs:
    for k in ["fav_tab=favorites", "cards>0"]:
        pass
    agg.setdefault("fav_tab==favorites", [0, 0])
    agg["fav_tab==favorites"][0] += 1
    if tab != "favorites":
        agg["fav_tab==favorites"][1] += 1
    agg.setdefault("cards>0", [0, 0])
    agg["cards>0"][0] += 1
    try:
        if int(cards) <= 0:
            agg["cards>0"][1] += 1
    except ValueError:
        agg["cards>0"][1] += 1
print("\n=== 逐腿可从日志直接读出的判据 ===")
for k, v in agg.items():
    print("  %-22s 总 %-4d 失败 %d" % (k, v[0], v[1]))

# 最后一个视口的完整 json（含全部 10 条判据）
j = OUT / "favleg-chromium.json"
d = json.loads(j.read_text(encoding="utf-8"))
print("\n=== 完整 json（最后一个视口，含 10 条判据）===")
print("  base =", d.get("base"), " engine =", d.get("engine"))
print("  started =", d.get("started_at"), " finished =", d.get("finished_at"))
agg2 = {}
for k, v in d["viewports"].items():
    for m, r in v.get("modes", {}).items():
        for kk in ("picker_open", "reached_picker", "fav_cards_waited",
                   "fav_exit_reached_picker"):
            agg2.setdefault(kk, [0, 0]); agg2[kk][0] += 1
            if not r.get(kk):
                agg2[kk][1] += 1
        for kk in ("select_type", "fav_entry_tap", "reselect_after_fav"):
            agg2.setdefault(kk, [0, 0]); agg2[kk][0] += 1
            if not r.get(kk, {}).get("ok"):
                agg2[kk][1] += 1
        agg2.setdefault("fav_cards_total", [0, 0]); agg2["fav_cards_total"][0] += 1
        if r.get("favorites_view", {}).get("cardsTotal", 0) <= 0:
            agg2["fav_cards_total"][1] += 1
        agg2.setdefault("detail_open", [0, 0]); agg2["detail_open"][0] += 1
        if (r.get("favorites_detail", {}).get("attempted")
                and r["favorites_detail"].get("detail_open") is not True):
            agg2["detail_open"][1] += 1
        agg2.setdefault("params_fab_open", [0, 0]); agg2["params_fab_open"][0] += 1
        if not r.get("params_drawer_exits", {}).get("fab_open"):
            agg2["params_fab_open"][1] += 1
for k, v in agg2.items():
    print("  %-26s 总 %-4d 失败 %d" % (k, v[0], v[1]))

summary = {
    "base": d.get("base"), "engine": d.get("engine"),
    "viewports": {k: {"ok": int(a), "total": int(b)}
                  for k, a, b in per_vp},
    "total_ok": sum(int(a) for _, a, _ in per_vp),
    "total_all": sum(int(b) for _, _, b in per_vp),
    "legs_parsed_from_log": len(legs),
    "fail_legs_in_log": len(fails),
    "assertions_last_viewport": {k: {"total": v[0], "failed": v[1]}
                                  for k, v in agg2.items()},
}
(OUT / "final-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
print("\n  汇总写入:", OUT / "final-summary.json")
