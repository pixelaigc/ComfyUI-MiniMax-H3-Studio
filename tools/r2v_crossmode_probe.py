#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 项根因第三轮：**跨模式接续对照**（分离 task_type 与条件类型）。

【要分离的两个变量】
2026-11-19 的实测对照：

  | 变体         | task_type | 条件内容                          | 段内中位 L1 | 最大 L1 |
  |--------------|-----------|-----------------------------------|-------------|---------|
  | flf 段2      | FL2VA     | first_frame + last_frame（keyframe）| 0.654       | 2.06    |
  | r2v 段2      | Ref2VA    | ref_image + ref_video（语义参考）  | 5.132       | 74.69   |

  两个变量**同时**变了（任务类型 + 条件类型），单靠这张表无法归因。
  本脚本用**跨模式接续**把它们分开：

  S. 段1 = flf（首帧=图A）→ 段2 = flf（首帧=图A）   FL2VA + keyframe
  T. 段1 = r2v（参考图A）  → 段2 = flf（首帧=图A）   FL2VA + keyframe
  U. 段1 = r2v（参考图A）  → 段2 = r2v（参考图A）    Ref2VA + 语义参考

  T 与 S 的差别 = **上游是谁**（段1 是 flf 还是 r2v）
  T 与 U 的差别 = **条件类型**（keyframe vs 语义参考）

  若 T 无跳变而 U 有 ⇒ 条件类型（语义参考）是主因。
  若 T 与 U 都有跳变 ⇒ 主因在「上游是 r2v」这条链上（参考图本身）。

【跨模式接续允许吗】
代码层允许：`continuation_allowed` 只检查**上游**模式 ∈ CONTINUABLE_MODES，
不要求目标模式与上游相同（server._prepare_continuation_params 同）。

用法：
  python3 tools/r2v_crossmode_probe.py --seed 20261601 --json-out /tmp/cross.json
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
    submit, wait_job, outputs_of, locate_segment_file, seam,
)

BASE = "http://127.0.0.1:8190"
REF_IMAGE_A = "asset_afb7438fdadf"
REF_VIDEO = "asset_486ce2e0f061"

BASE_PARAMS = {
    "prompt": "教室里的日常，少女走上讲台",
    "original_prompt": "教室里的日常，少女走上讲台",
    "model_key": "hybrid_fl2va_ref2va",
    "width": 1280, "height": 736,
    "resolution": "736p", "aspect": "16:9",
    "duration": 5, "steps": 4,
    "sampler": "euler", "scheduler": "beta",
    "shift_video": 12.0, "shift_audio": 12.0,
    "audio_track_source": "generated",
    "twopass": {"enabled": False},
    "accel_lora": "", "accel_lora_strength": 1.0,
    "accel_loras": [], "effect_loras": [],
}


def fetch_ctx(jid):
    url = f"{BASE}/api/jobs/{jid}/continuation?duration=5"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261601)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    refs_img = [{"asset_id": REF_IMAGE_A, "slot": "ref_image", "ordinal": 0}]
    refs_img_vid = refs_img + [
        {"asset_id": REF_VIDEO, "slot": "ref_video", "ordinal": 0}]
    refs_first = [{"asset_id": REF_IMAGE_A, "slot": "first_frame", "ordinal": 0}]

    def run_seg(mode, label, seed_off, refs, cont=None, prompt_suffix="（接续段）"):
        p = dict(BASE_PARAMS)
        p["prompt"] = BASE_PARAMS["prompt"] + prompt_suffix
        p["original_prompt"] = p["prompt"]
        p["seed"] = args.seed + seed_off
        if cont:
            p["continuation"] = dict(cont)
        print(f"=== {label} ===")
        jid = submit(mode, p, refs)
        print("  job:", jid)
        rr = wait_job(jid)
        print("  status:", rr.get("status"), "err:", (rr.get("error") or "")[:300])
        if rr.get("status") != "done":
            return None, None
        f = locate_segment_file(jid, outputs_of(jid))
        if not f:
            print("  ⚠ 没找到分段文件")
            return None, None
        s = seam(f)
        print(f"  帧数={s['frames']} 中位L1={s['median_l1']} 最大L1={s['max_l1']} "
              f"@{s['max_l1_at_frame']} 比值={s['max_l1_ratio']}x")
        for t in s["top3"][:2]:
            print(f"     @{t['at_frame']:3d} L1={t['l1']:8.3f} "
                  f"亮度 {t['lum_from']:7.2f} -> {t['lum_to']:7.2f} (Δ={t['d_lum']:+.2f})")
        return jid, s

    rec = {}

    # ---- 段1：r2v（参考图A + 参考视频）----
    print("=== 段1（r2v + 参考图A + 参考视频）===")
    p1 = dict(BASE_PARAMS)
    p1["seed"] = args.seed
    j1 = submit("r2v", p1, refs_img_vid)
    print("  job:", j1)
    r = wait_job(j1)
    print("  status:", r.get("status"), "err:", (r.get("error") or "")[:200])
    if r.get("status") != "done":
        return 1
    ctx_r2v = fetch_ctx(j1).get("continuation") or {}
    f1 = locate_segment_file(j1, outputs_of(j1))
    s1 = seam(f1) if f1 else None
    if s1:
        print(f"  段1: 帧数={s1['frames']} 中位L1={s1['median_l1']} "
              f"最大L1={s1['max_l1']} @{s1['max_l1_at_frame']} 比值={s1['max_l1_ratio']}x")

    # ---- 段1': flf（首帧=图A）----
    print("=== 段1'（flf + 首帧=图A）===")
    p1b = dict(BASE_PARAMS)
    p1b["seed"] = args.seed + 10
    j1b = submit("flf", p1b, refs_first)
    print("  job:", j1b)
    rb = wait_job(j1b)
    print("  status:", rb.get("status"), "err:", (rb.get("error") or "")[:200])
    ctx_flf = {}
    s1b = None
    if rb.get("status") == "done":
        ctx_flf = fetch_ctx(j1b).get("continuation") or {}
        f1b = locate_segment_file(j1b, outputs_of(j1b))
        s1b = seam(f1b) if f1b else None
        if s1b:
            print(f"  段1': 帧数={s1b['frames']} 中位L1={s1b['median_l1']} "
                  f"最大L1={s1b['max_l1']} @{s1b['max_l1_at_frame']} 比值={s1b['max_l1_ratio']}x")

    # ---- U. r2v 段1 -> r2v 段2（参考图A）----
    jU, sU = run_seg("r2v", "U. 段2 r2v（参考图A）= Ref2VA + 语义参考",
                     1, refs_img_vid, ctx_r2v)
    # ---- T. r2v 段1 -> flf 段2（首帧=图A）----
    jT, sT = run_seg("flf", "T. 段2 flf（首帧=图A）= FL2VA + keyframe",
                     1, refs_first, ctx_r2v)
    # ---- S. flf 段1' -> flf 段2（首帧=图A）----
    jS, sS = run_seg("flf", "S. 段2 flf（首帧=图A，上游是 flf）",
                     11, refs_first, ctx_flf)

    rec = {"seg1_r2v": {"job": j1, "seam": s1},
           "seg1_flf": {"job": j1b, "seam": s1b},
           "U_r2v_seg2": {"job": jU, "seam": sU},
           "T_r2vseg1_flfseg2": {"job": jT, "seam": sT},
           "S_flfseg1_flfseg2": {"job": jS, "seam": sS}}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
