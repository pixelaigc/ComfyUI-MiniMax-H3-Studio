#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keyframe 对照的**补测**（只跑段2；段1 已有数据）。

【为什么只跑段2】
2026-11-19 07:00 前的那次 `r2v_keyframe_probe` 在跑段2 时被
comfyui-watchdog 重启 ComfyUI 打断（06:23:47），两个 flf 接续段 failed，
且 ComfyUI history 被清空 ⇒ 失败原因无法取回。
段1 的结果已经落库（V 无跳变 / W 有跳变），本脚本只补段2。

【用法】
  python3 tools/r2v_keyframe_seg2.py --seed 20261701 --json-out /tmp/kf2.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.r2v_chain_runner import (  # noqa: E402
    submit, wait_job, outputs_of, seam,
)
from tools.r2v_keyframe_probe import (  # noqa: E402
    BASE_PARAMS, IMG_A, fetch_ctx, find_seg_file,
)

BASE = "http://127.0.0.1:8190"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261701)
    ap.add_argument("--seg1-v", default="25140ba36491", help="V 的段1 job id")
    ap.add_argument("--seg1-w", default="b3d7e06cc7be", help="W 的段1 job id")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    refs_both = [
        {"asset_id": IMG_A, "slot": "first_frame", "ordinal": 0},
        {"asset_id": IMG_A, "slot": "last_frame", "ordinal": 1},
    ]
    refs_first_only = [
        {"asset_id": IMG_A, "slot": "first_frame", "ordinal": 0},
    ]

    def run_seg(label, seed_off, refs, seg1_job):
        ctx = fetch_ctx(seg1_job).get("continuation") or {}
        if not ctx:
            print(f"  ⚠ 取不到 {seg1_job} 的接续上下文")
            return None, None
        p = dict(BASE_PARAMS)
        p["prompt"] = BASE_PARAMS["prompt"] + "（接续段）"
        p["original_prompt"] = p["prompt"]
        p["seed"] = args.seed + seed_off
        p["continuation"] = dict(ctx)
        print(f"=== {label} ===")
        jid = submit("flf", p, refs)
        print("  job:", jid)
        rr = wait_job(jid)
        print("  status:", rr.get("status"), "err:", (rr.get("error") or "")[:300])
        if rr.get("status") != "done":
            return jid, None
        f = find_seg_file(jid)
        if not f:
            print("  ⚠ 没找到分段文件")
            return jid, None
        s = seam(f)
        print(f"  帧数={s['frames']} 中位L1={s['median_l1']} 最大L1={s['max_l1']} "
              f"@{s['max_l1_at_frame']} 比值={s['max_l1_ratio']}x")
        for t in s["top3"][:2]:
            print(f"     @{t['at_frame']:3d} L1={t['l1']:8.3f} "
                  f"亮度 {t['lum_from']:7.2f} -> {t['lum_to']:7.2f} (Δ={t['d_lum']:+.2f})")
        return jid, s

    jV, sV = run_seg("V 段2 接续（首帧=尾帧=图A，两端都钉）", 1, refs_both, args.seg1_v)
    jW, sW = run_seg("W 段2 接续（只给首帧，只钉首帧）", 11, refs_first_only, args.seg1_w)

    rec = {"V_both_frames": {"seg1_job": args.seg1_v, "seg2_job": jV, "seg2": sV},
           "W_first_only": {"seg1_job": args.seg1_w, "seg2_job": jW, "seg2": sW},
           "note": "段2 补测（首跑被 comfyui-watchdog 重启 ComfyUI 打断）"}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
