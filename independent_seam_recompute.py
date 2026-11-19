#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立复算本轮 3 条 flf 接续链的接缝指标（不复用归档 summary，直接从 mp4 重算）。

【为什么要复算】
归档的 seam_repeat.json 里 `lum_drop_3frames` 数值异常大（+69 / +9.4 / +16.9），
那是「段2 内部前三帧的亮度演化」，会被段内光照事件污染 —— 不是接缝台阶。
真正测接缝亮度台阶的是：
    A = lum[段2 首帧] - lum[段1 末帧]
本脚本直接从**原始 mp4** 重算 A / 头断点比 / 拼接点 L1 / 饱和度跳变，
给出均值、标准差、极差、符号分布，用于判定「系统性偏暗」还是「单样本波动」。

【口径】等价 L1 口径（逐帧 RGB 的 L1 均值差，320x180），不是官方 HF 能量项。
【实现独立】不 import live_flf_seam_repeat.py 的统计函数，只用同样的 ffmpeg 取帧方式
（320x180 rgb24 rawvideo），保证复算路径与原测量器一致但代码路径独立。
"""
from __future__ import annotations

import json
import os
import statistics as st
import subprocess
import sys
from pathlib import Path

import numpy as np

DATA_OUT = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
EVID = Path(sys.argv[1] if len(sys.argv) > 1 else
            "/root/projects/h3s-resume-cand/evidence/evidence-20261120-resume-flf")
SEAM_JSON = sorted(EVID.glob("*/seam_repeat.json"))[-1]


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
    import numpy as np
    return np.frombuffer(raw[:n * 320 * 180 * 3], dtype=np.uint8).reshape(
        n, 180, 320, 3).astype(np.float32)


def l1(a, b):
    import numpy as np
    return float(np.abs(a - b).mean())


def luma(S):
    import numpy as np
    # Rec.601 亮度（与原测量器同口径）
    return (0.299 * S[..., 0] + 0.587 * S[..., 1] + 0.114 * S[..., 2]).mean(axis=(1, 2))


def sat(S):
    import numpy as np
    mx = S.max(axis=-1)
    mn = S.min(axis=-1)
    return ((mx - mn) / np.maximum(mx, 1e-6)).mean(axis=(1, 2))


def main():
    d = json.load(open(SEAM_JSON))
    rows = []
    for c in d["chains"]:
        segs = c["seam"]["segments"]
        if len(segs) < 2:
            continue
        S1 = frames(DATA_OUT / segs[0]["rel"])
        S2 = frames(DATA_OUT / segs[1]["rel"])
        if S1 is None or S2 is None:
            print(f"!! {c['tag']} mp4 缺失: {segs[0]['rel']} / {segs[1]['rel']}")
            continue
        l1f, l2f = luma(S1), luma(S2)
        s1f, s2f = sat(S1), sat(S2)
        # 段内相邻帧 L1
        d1 = np.abs(np.diff(S1, axis=0)).mean(axis=(1, 2, 3))
        d2 = np.abs(np.diff(S2, axis=0)).mean(axis=(1, 2, 3))
        rows.append({
            "tag": c["tag"], "seed": c["seed"],
            "job1": c.get("job1"), "job2": c.get("job2"),
            "seg1_job2_std_sampler":
                (c.get("job2_workflow_analysis") or {}).get("has_standard_sampler"),
            "seg2_job2_std_sampler":
                (c.get("job2_workflow_analysis") or {}).get("has_standard_sampler"),
            "seg1_selflift": (c.get("job1_workflow_analysis") or {}).get("has_selflift"),
            "seg2_selflift": (c.get("job2_workflow_analysis") or {}).get("has_selflift"),
            # 指标 A：接缝亮度台阶（真正的接缝跳变）
            "A_seam_lum_delta": float(l2f[0] - l1f[-1]),
            # 指标 B：段2 内部前三帧亮度演化（任务原口径，会被段内光照污染）
            "B_seg2_lum_drop_3f": float(l2f[0] - l2f[3]) if len(l2f) > 3 else None,
            # 指标 C：段2 头断点比
            "C_seg2_head_break_ratio": float(d2[0] / np.median(d2)) if np.median(d2) > 0 else None,
            # 指标 D：拼接点 L1
            "D_seam_L1": float(l1(S1[-1], S2[0])),
            # 附加：接缝饱和度台阶
            "A_seam_sat_delta": float(s2f[0] - s1f[-1]),
            "seg1_lum_last": float(l1f[-1]), "seg2_lum_first": float(l2f[0]),
        })

    def stat(vals, label, unit=""):
        vals = [v for v in vals if v is not None]
        if not vals:
            print(f"  {label}: (空)")
            return
        m = st.mean(vals)
        sd = st.stdev(vals) if len(vals) > 1 else 0.0
        pos = sum(1 for v in vals if v > 0)
        neg = sum(1 for v in vals if v < 0)
        print(f"  {label}: n={len(vals)} mean={m:+.4f}{unit} sd={sd:.4f} "
              f"range={max(vals)-min(vals):.4f} sign +{pos}/-{neg}")
        print(f"      vals={[round(v,4) for v in vals]}")

    print("=" * 78)
    print(f"本轮 3 条 flf 接续链 —— 接缝指标独立复算（源：{SEAM_JSON}）")
    print("=" * 78)
    print(f"\n{'链':<8}{'seed':<10}{'A接缝亮度差':>13}{'B段2三帧降':>13}"
          f"{'C头断点比':>11}{'D拼接L1':>11}{'段2标准采样':>12}")
    for r in rows:
        print(f"{r['tag']:<8}{r['seed']:<10}{r['A_seam_lum_delta']:>13.4f}"
              f"{r['B_seg2_lum_drop_3f']:>13.4f}{r['C_seg2_head_break_ratio']:>11.4f}"
              f"{r['D_seam_L1']:>11.4f}{str(r['seg2_job2_std_sampler']):>12}")

    print("\n【统计量】")
    stat([r["A_seam_lum_delta"] for r in rows], "指标A 接缝亮度台阶 (灰阶)")
    stat([r["B_seg2_lum_drop_3f"] for r in rows], "指标B 段2三帧亮度降 (灰阶)")
    stat([r["C_seg2_head_break_ratio"] for r in rows], "指标C 段2头断点比 (倍)")
    stat([r["D_seam_L1"] for r in rows], "指标D 拼接点L1")
    stat([r["A_seam_sat_delta"] for r in rows], "接缝饱和度台阶")

    A = [r["A_seam_lum_delta"] for r in rows]
    print("\n【判定】系统性偏暗的必要条件 = |均值| >= 0.3 且 符号方向一致")
    m = st.mean(A) if A else 0.0
    pos = sum(1 for v in A if v > 0)
    neg = sum(1 for v in A if v < 0)
    print(f"  |mean(A)| = {abs(m):.4f}  -> {'满足' if abs(m) >= 0.3 else '不满足'} 门槛 0.3")
    print(f"  符号方向: 正{pos}/负{neg} -> {'一致' if pos == 0 or neg == 0 else '不一致'}")
    systematic = abs(m) >= 0.3 and (pos == 0 or neg == 0)
    print(f"  ⇒ 判定：{'系统性偏暗' if systematic else '单样本波动（不构成系统性偏暗）'}")

    out = EVID / "independent_seam_recompute.json"
    json.dump({"source_seam_json": str(SEAM_JSON), "rows": rows,
               "verdict_systematic_dark_bias": systematic,
               "metric_A_mean": m, "metric_A_sign_pos": pos, "metric_A_sign_neg": neg},
              open(out, "w"), ensure_ascii=False, indent=1)
    print(f"\n已写出: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
