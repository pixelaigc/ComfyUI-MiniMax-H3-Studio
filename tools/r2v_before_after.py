#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1：修复前 vs 修复后 的 r2v 接续链逐条对照表 + 配对统计。

【输入】
  BEFORE  修复前的 5 条链（上轮跑的，证据已入库）
  AFTER   修复后的 5 条链（本轮在已上线版本上用**同 seed 同素材同脚本**重跑）

【口径（与上轮完全一致，不改）】
  等价 L1：scaled RGB(1280) -> numpy mean -> 帧间 L1 均值差
  比值    = 段内最大 L1 / 段内中位 L1
  Δ亮度   = 跳变处前后两帧的亮度差

【要输出的三组对照】
  1) 段1（链首，非接续）
  2) 段2（接续）
  3) 对照段（同 seed、同素材、**非接续**）
  每组都给：中位 L1 / 最大 L1 / @帧号 / Δ亮度 / 比值

【统计】
  * 接续段 − 对照段 的逐条差值（配对）
  * 配对 t 量级（n 小，只报量级不报 p 值结论）
  * 方向一致性（几/几条同方向）

用法：
  python3 tools/r2v_before_after.py \
      --before evidence/20261119-r2v-cont-continuation/r2v_expand_5chains.json \
      --after  /tmp/r2v_after.json \
      --json-out evidence/20261119-r2v-cont-continuation/r2v_before_after.json
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys


def _dl(s: dict, key: str):
    """取 top3 里 Δ亮度绝对值最大的那一条的 Δ亮度。"""
    top3 = s.get("top3") or []
    if not top3:
        return None
    best = max(top3, key=lambda x: abs(x.get("d_lum", 0.0)))
    return best.get("d_lum")


def _row(r: dict) -> dict:
    """把一条链的记录压成一行。"""
    seg1, seg2 = r["seg1"], r["seg2"]
    ctrl = r.get("ctrl")
    out = {
        "seed": r["seed"],
        "seg1_job": r.get("seg1_job"),
        "seg2_job": r.get("seg2_job"),
        "ctrl_job": r.get("ctrl_job"),
        "seg1": {
            "median_l1": seg1["median_l1"], "max_l1": seg1["max_l1"],
            "at_frame": seg1["max_l1_at_frame"], "ratio": seg1["max_l1_ratio"],
            "d_lum": _dl(seg1, "d_lum"),
        },
        "seg2": {
            "median_l1": seg2["median_l1"], "max_l1": seg2["max_l1"],
            "at_frame": seg2["max_l1_at_frame"], "ratio": seg2["max_l1_ratio"],
            "d_lum": _dl(seg2, "d_lum"),
        },
    }
    if ctrl:
        out["ctrl"] = {
            "median_l1": ctrl["median_l1"], "max_l1": ctrl["max_l1"],
            "at_frame": ctrl["max_l1_at_frame"], "ratio": ctrl["max_l1_ratio"],
            "d_lum": _dl(ctrl, "d_lum"),
        }
    # 证据字段（修复后才有）
    for k, key in (("seg1_fields", "seg1_fields"), ("seg2_fields", "seg2_fields"),
                   ("ctrl_fields", "ctrl_fields")):
        if r.get(k):
            f = r[k]
            out[key] = {
                "ref_video_frame_count": f.get("ref_video_frame_count"),
                "chain_total_frames": f.get("chain_total_frames"),
                "video_drop": f.get("video_drop"),
                "stage_index": f.get("stage_index"),
            }
    for k, key in (("seg2_comfy", "seg2_comfy"), ("seg1_comfy", "seg1_comfy")):
        if r.get(k):
            c = r[k]
            out[key] = {
                "found": c.get("found"),
                "sampler_nodes": [s.get("class_type") for s in (c.get("sampler_nodes") or [])],
                "has_selflift": c.get("has_selflift"),
            }
    return out


def _paired(a: list[float], b: list[float]) -> dict:
    diffs = [x - y for x, y in zip(a, b)]
    t = None
    if len(diffs) > 1:
        sd = st.stdev(diffs)
        t = (st.mean(diffs) / (sd / (len(diffs) ** 0.5))) if sd else 0.0
    return {
        "n": len(diffs),
        "diffs": [round(d, 4) for d in diffs],
        "mean_diff": round(st.mean(diffs), 4) if diffs else None,
        "median_diff": round(st.median(diffs), 4) if diffs else None,
        "sigma": round(st.pstdev(diffs), 4) if diffs else None,
        "same_direction_positive": sum(1 for d in diffs if d > 0),
        "paired_t": round(t, 3) if t is not None else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    before = [_row(r) for r in json.load(open(args.before))]
    after = [_row(r) for r in json.load(open(args.after))]
    bmap = {r["seed"]: r for r in before}
    amap = {r["seed"]: r for r in after}
    common = sorted(set(bmap) & set(amap))

    print(f"═══ R1 修复前 vs 修复后 逐条对照（共同 seed: {len(common)}）═══")
    print(f"修复前 seed: {sorted(bmap)}")
    print(f"修复后 seed: {sorted(amap)}")
    missing = sorted(set(bmap) ^ set(amap))
    if missing:
        print(f"⚠️ 不配对的 seed: {missing}")
    print()

    hdr = (f"{'seed':>10} | {'段2 前 maxL1':>12} {'比值':>7} {'@帧':>4} {'Δ亮度':>7} "
           f"| {'段2 后 maxL1':>12} {'比值':>7} {'@帧':>4} {'Δ亮度':>7} "
           f"| {'比值变化':>9}")
    print("── 接续段（段2）──")
    print(hdr)
    print("-" * len(hdr))
    b2r, a2r = [], []
    for seed in common:
        b, a = bmap[seed], amap[seed]
        b2r.append(b["seg2"]["ratio"])
        a2r.append(a["seg2"]["ratio"])
        print(f"{seed:>10} | {b['seg2']['max_l1']:>12.2f} {b['seg2']['ratio']:>6.2f}x "
              f"{b['seg2']['at_frame']:>4} {str(b['seg2']['d_lum']):>7} "
              f"| {a['seg2']['max_l1']:>12.2f} {a['seg2']['ratio']:>6.2f}x "
              f"{a['seg2']['at_frame']:>4} {str(a['seg2']['d_lum']):>7} "
              f"| {a['seg2']['ratio'] - b['seg2']['ratio']:>+8.2f}x")
    print()
    print("── 段1（链首，非接续）──")
    print(f"{'seed':>10} | {'前 maxL1':>10} {'比值':>7} | {'后 maxL1':>10} {'比值':>7}")
    for seed in common:
        b, a = bmap[seed], amap[seed]
        print(f"{seed:>10} | {b['seg1']['max_l1']:>10.2f} {b['seg1']['ratio']:>6.2f}x "
              f"| {a['seg1']['max_l1']:>10.2f} {a['seg1']['ratio']:>6.2f}x")
    print()
    print("── 对照段（同 seed 非接续）──")
    print(f"{'seed':>10} | {'前 maxL1':>10} {'比值':>7} | {'后 maxL1':>10} {'比值':>7}")
    for seed in common:
        b, a = bmap[seed], amap[seed]
        if not b.get("ctrl") or not a.get("ctrl"):
            continue
        print(f"{seed:>10} | {b['ctrl']['max_l1']:>10.2f} {b['ctrl']['ratio']:>6.2f}x "
              f"| {a['ctrl']['max_l1']:>10.2f} {a['ctrl']['ratio']:>6.2f}x")
    print()

    stats = {}
    print("═══ 配对统计 ═══")
    if b2r and a2r:
        s = _paired(a2r, b2r)
        stats["seg2_ratio_after_minus_before"] = s
        print(f"① 接续段比值（修复后 − 修复前）: n={s['n']} 均值={s['mean_diff']:+.2f}x "
              f"中位={s['median_diff']:+.2f}x σ={s['sigma']:.2f} "
              f"变差条数={s['same_direction_positive']}/{s['n']} 配对 t={s['paired_t']}")
    # 接续段 vs 对照段（修复后）—— 隔离「接续」这个变量
    a_ctrl_r = [amap[s]["ctrl"]["ratio"] for s in common
                if amap[s].get("ctrl") and bmap[s].get("ctrl")]
    a_cont_r = [amap[s]["seg2"]["ratio"] for s in common
                if amap[s].get("ctrl") and bmap[s].get("ctrl")]
    if a_ctrl_r:
        s = _paired(a_cont_r, a_ctrl_r)
        stats["after_seg2_minus_ctrl"] = s
        print(f"② 修复后：接续段 − 对照段        : n={s['n']} 均值={s['mean_diff']:+.2f}x "
              f"中位={s['median_diff']:+.2f}x σ={s['sigma']:.2f} "
              f"接续更差={s['same_direction_positive']}/{s['n']} 配对 t={s['paired_t']}")
    b_ctrl_r = [bmap[s]["ctrl"]["ratio"] for s in common
                if amap[s].get("ctrl") and bmap[s].get("ctrl")]
    b_cont_r = [bmap[s]["seg2"]["ratio"] for s in common
                if amap[s].get("ctrl") and bmap[s].get("ctrl")]
    if b_ctrl_r:
        s = _paired(b_cont_r, b_ctrl_r)
        stats["before_seg2_minus_ctrl"] = s
        print(f"③ 修复前：接续段 − 对照段        : n={s['n']} 均值={s['mean_diff']:+.2f}x "
              f"中位={s['median_diff']:+.2f}x σ={s['sigma']:.2f} "
              f"接续更差={s['same_direction_positive']}/{s['n']} 配对 t={s['paired_t']}")
    print()

    print("═══ 修复后落库证据 ═══")
    for seed in common:
        a = amap[seed]
        f2 = a.get("seg2_fields") or {}
        f1 = a.get("seg1_fields") or {}
        c2 = a.get("seg2_comfy") or {}
        print(f"  seed {seed}: 段1 ref_video_frame_count={f1.get('ref_video_frame_count')} "
              f"chain_total_frames={f1.get('chain_total_frames')} "
              f"video_drop={f1.get('video_drop')}")
        print(f"           段2 ref_video_frame_count={f2.get('ref_video_frame_count')} "
              f"chain_total_frames={f2.get('chain_total_frames')} "
              f"video_drop={f2.get('video_drop')} stage={f2.get('stage_index')}")
        print(f"           段2 采样节点={c2.get('sampler_nodes')} SelfLift={c2.get('has_selflift')}")

    out = {
        "before_file": args.before, "after_file": args.after,
        "common_seeds": common,
        "before": {str(s): bmap[s] for s in common},
        "after": {str(s): amap[s] for s in common},
        "stats": stats,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
