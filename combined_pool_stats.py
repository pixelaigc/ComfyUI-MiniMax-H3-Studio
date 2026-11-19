#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并本轮两批 flf 接续链（batch a + batch b）做联合统计。

【为什么要合并】
第一批（seed 20261131-33，3 条）与第二批（seed 20261141-42，2 条）
用的是**同一素材**（pick_image 选中同一张）、不同 seed ⇒ 构成一个素材受控组。
单批 n=2 或 n=3 的符号方向都不可靠（方向一致 / 不一致会随批次翻转），
必须合并到 n=5 才有统计意义，再与历史池（dd7ab82 N=14）对照。
"""
from __future__ import annotations

import json
import statistics as st
import subprocess
import sys
from pathlib import Path

import numpy as np

DATA_OUT = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
BATCHES = [
    ("A(3条)", Path("/root/projects/h3s-resume-cand/evidence/evidence-20261120-resume-flf")),
    ("B(2条)", Path("/root/projects/h3s-resume-cand/evidence/evidence-20261120-resume-flf-b")),
]
HIST = Path("/root/projects/h3studio-live/evidence/evidence-20261118-flf-lum-closure/"
            "final_verdict_stats.json")


def frames(path: Path):
    if not path.exists():
        return None
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=320:180",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    n = len(raw) // (320 * 180 * 3)
    if n == 0:
        return None
    return np.frombuffer(raw[:n * 320 * 180 * 3], dtype=np.uint8).reshape(
        n, 180, 320, 3).astype(np.float32)


def luma(S):
    return (0.299 * S[..., 0] + 0.587 * S[..., 1] + 0.114 * S[..., 2]).mean(axis=(1, 2))


def sat(S):
    mx, mn = S.max(axis=-1), S.min(axis=-1)
    return ((mx - mn) / np.maximum(mx, 1e-6)).mean(axis=(1, 2))


def collect():
    rows = []
    for batch, root in BATCHES:
        sj = sorted(root.glob("*/seam_repeat.json"))[-1]
        d = json.load(open(sj))
        for c in d["chains"]:
            segs = c["seam"]["segments"]
            if len(segs) < 2:
                continue
            S1, S2 = frames(DATA_OUT / segs[0]["rel"]), frames(DATA_OUT / segs[1]["rel"])
            if S1 is None or S2 is None:
                print(f"!! mp4 缺失 {segs[0]['rel']} / {segs[1]['rel']}")
                continue
            l1f, l2f = luma(S1), luma(S2)
            s1f, s2f = sat(S1), sat(S2)
            d1 = np.abs(np.diff(S1, axis=0)).mean(axis=(1, 2, 3))
            d2 = np.abs(np.diff(S2, axis=0)).mean(axis=(1, 2, 3))
            rows.append({
                "batch": batch, "tag": c["tag"], "seed": c["seed"],
                "job1": c.get("job1"), "job2": c.get("job2"),
                "seg2_standard_sampler":
                    (c.get("job2_workflow_analysis") or {}).get("has_standard_sampler"),
                "seg2_selflift_count":
                    (c.get("job2_workflow_analysis") or {}).get("selflift_count"),
                "A_seam_lum_delta": float(l2f[0] - l1f[-1]),
                "B_seg2_lum_drop_3f": float(l2f[0] - l2f[3]) if len(l2f) > 3 else None,
                "C_seg2_head_break_ratio": float(d2[0] / np.median(d2)) if np.median(d2) > 0 else None,
                "D_seam_L1": float(np.abs(S1[-1] - S2[0]).mean()),
                "D_over_seg1_median": float(np.abs(S1[-1] - S2[0]).mean() / np.median(d1)),
                "A_seam_sat_delta": float(s2f[0] - s1f[-1]),
            })
    return rows


def stat(vals, label, unit=""):
    vals = [v for v in vals if v is not None]
    if not vals:
        print(f"  {label}: (空)")
        return None
    m, sd = st.mean(vals), (st.stdev(vals) if len(vals) > 1 else 0.0)
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    print(f"  {label}: n={len(vals)} mean={m:+.4f}{unit} sd={sd:.4f} "
          f"range={max(vals)-min(vals):.4f} sign +{pos}/-{neg}")
    print(f"      vals={[round(v, 4) for v in vals]}")
    return {"n": len(vals), "mean": m, "sd": sd, "range": max(vals) - min(vals),
            "sign_pos": pos, "sign_neg": neg, "values": vals}


def main():
    rows = collect()
    print("=" * 96)
    print(f"本轮 flf 接续链合并统计（批次 A + B，同素材、不同 seed，n={len(rows)}）")
    print("=" * 96)
    print(f"\n{'批次':<8}{'链':<7}{'seed':<10}{'A接缝亮度差':>13}{'B段2三帧降':>13}"
          f"{'C头断点比':>11}{'D拼接L1':>11}{'D/段1中位':>11}{'段2标准采样':>12}")
    for r in rows:
        print(f"{r['batch']:<8}{r['tag']:<7}{r['seed']:<10}{r['A_seam_lum_delta']:>13.4f}"
              f"{r['B_seg2_lum_drop_3f']:>13.4f}{r['C_seg2_head_break_ratio']:>11.4f}"
              f"{r['D_seam_L1']:>11.4f}{r['D_over_seg1_median']:>11.3f}"
              f"{str(r['seg2_standard_sampler']):>12}")

    print("\n【本轮 n=5 统计量】")
    stats = {
        "A_seam_lum_delta": stat([r["A_seam_lum_delta"] for r in rows], "指标A 接缝亮度台阶 (灰阶)"),
        "B_seg2_lum_drop_3f": stat([r["B_seg2_lum_drop_3f"] for r in rows], "指标B 段2三帧亮度降 (灰阶)"),
        "C_seg2_head_break_ratio": stat([r["C_seg2_head_break_ratio"] for r in rows], "指标C 段2头断点比 (倍)"),
        "D_seam_L1": stat([r["D_seam_L1"] for r in rows], "指标D 拼接点L1"),
        "D_over_seg1_median": stat([r["D_over_seg1_median"] for r in rows], "指标D/段1内部中位 (倍)"),
    }

    # 与历史池（dd7ab82 N=14）对照
    print("\n【与历史池对照（N=14，dd7ab82，归档证据）】")
    hist = json.load(open(HIST))
    hA = [x["seam_lum_delta"] for x in hist["sample_rows"]]
    hB = [x["seg2_lum_drop_3frames"] for x in hist["sample_rows"]]
    hD = [x["seam_L1"] for x in hist["sample_rows"]]
    hC = [x["seg2_head_break_ratio"] for x in hist["sample_rows"]]
    hist_stats = {
        "A_seam_lum_delta": stat(hA, "历史池 指标A 接缝亮度台阶 (灰阶)"),
        "B_seg2_lum_drop_3f": stat(hB, "历史池 指标B 段2三帧亮度降 (灰阶)"),
        "C_seg2_head_break_ratio": stat(hC, "历史池 指标C 段2头断点比 (倍)"),
        "D_seam_L1": stat(hD, "历史池 指标D 拼接点L1"),
    }

    # 合并池
    print("\n【本轮 n=5 ∪ 历史池 n=14 = 联合池 n=19】")
    allA = [r["A_seam_lum_delta"] for r in rows] + hA
    allB = [r["B_seg2_lum_drop_3f"] for r in rows] + hB
    allD = [r["D_seam_L1"] for r in rows] + hD
    combined = {
        "A_seam_lum_delta": stat(allA, "联合池 指标A 接缝亮度台阶 (灰阶)"),
        "B_seg2_lum_drop_3f": stat(allB, "联合池 指标B 段2三帧亮度降 (灰阶)"),
        "D_seam_L1": stat(allD, "联合池 指标D 拼接点L1"),
    }

    print("\n【判定：系统性偏暗的必要条件 = |mean(A)| >= 0.3 且 符号方向一致】")
    for name, s in (("本轮 n=5", stats["A_seam_lum_delta"]),
                    ("历史池 n=14", hist_stats["A_seam_lum_delta"]),
                    ("联合池 n=19", combined["A_seam_lum_delta"])):
        if not s:
            continue
        m, pos, neg = s["mean"], s["sign_pos"], s["sign_neg"]
        consistent = (pos == 0 or neg == 0)
        systematic = abs(m) >= 0.3 and consistent
        print(f"  {name}: |mean(A)|={abs(m):.4f} ({'≥' if abs(m)>=0.3 else '<'}0.3)  "
              f"符号 正{pos}/负{neg} ({'一致' if consistent else '不一致'})  "
              f"=> {'系统性偏暗' if systematic else '不构成系统性偏暗'}")

    out = Path("/root/projects/h3s-resume-cand/evidence/evidence-20261120-resume-flf/"
               "combined_pool_stats.json")
    json.dump({"rows": rows, "stats_this_round": stats, "stats_hist": hist_stats,
               "stats_combined": combined,
               "note": "指标A = lum[段2首帧]-lum[段1末帧]（真正的接缝亮度台阶）；"
                       "指标B = 段2内部前三帧亮度演化（任务原口径，受段内光照事件污染）"},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n已写出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
