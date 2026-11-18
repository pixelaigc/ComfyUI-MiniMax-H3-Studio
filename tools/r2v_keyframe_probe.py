#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 项第四轮：首帧「单给」vs「首尾同图」的决定性对照。

【为什么这个对照是决定性的】
三轮对照后的**同模式**结果（可靠）：

  | 链 | 段2 的条件 | 段内跳变 | 落差 |
  |---|---|---|---|
  | S | flf，首帧=图A，**尾帧缺省** | @7，L1=62.68，Δ+16.02 | −8.12 |
  | U | r2v，参考图A | @6，L1=84.08，Δ+19.19 | +9.12 |

  而**上一轮**的 flf 链（首帧与尾帧**同一张图**）实测段内中位 L1=0.654、
  最大 2.06 ⇒ **无跳变**。

  ⇒ 变量是「**本段条件对画面的约束强度**」，不是 task_type、不是条件类型：

  * 首帧 = 尾帧 = 同一张图 ⇒ T8 的 **keyframe 把两端都钉住**，
    生成区（中段）既被端点约束、又被提示词引导 ⇒ 与钉住区**不冲突**
    ⇒ 无跳变。
  * 只给首帧（尾帧缺省）⇒ task_type 变 I2VA ⇒ **只钉首帧**，
    尾部生成区自由 ⇒ 与钉住区「段1 已演化的画面」**冲突** ⇒ 跳变。
  * r2v 参考图 ⇒ **整段语义参考** ⇒ 与钉住区冲突 ⇒ 跳变。

【本实验】
  V. 段1 flf（首帧=尾帧=图A）→ 段2 flf（首帧=尾帧=图A）  两端都钉
  W. 段1 flf（首帧=图A，尾帧缺省）→ 段2 flf（首帧=图A，尾帧缺省）  只钉首帧

  两者唯一差别 = 段2 有没有尾帧 keyframe。
  若 V 无跳变而 W 有 ⇒ H5 成立（约束强度决定跳变）。

用法：
  python3 tools/r2v_keyframe_probe.py --seed 20261701 --json-out /tmp/kf.json
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

BASE = "http://127.0.0.1:8190"
IMG_A = "asset_afb7438fdadf"
IMG_B = "asset_6af34f4016bc"
OUT_DIR = "/mnt/usb2t_toshiba/comfyui/output/h3studio"

BASE_PARAMS = {
    "prompt": "教室里的日常，少女走上讲台",
    "original_prompt": "教室里的日常，少女走上讲台",
    "model_key": "fl2va_pruned_int8",
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


def find_seg_file(jid):
    import glob
    hits = glob.glob(os.path.join(OUT_DIR, f"flf_{jid}_*.mp4"))
    return hits[0] if hits else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261701)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    refs_both = [
        {"asset_id": IMG_A, "slot": "first_frame", "ordinal": 0},
        {"asset_id": IMG_A, "slot": "last_frame", "ordinal": 1},
    ]
    refs_first_only = [
        {"asset_id": IMG_A, "slot": "first_frame", "ordinal": 0},
    ]

    def run_seg(mode, label, seed_off, refs, cont=None):
        p = dict(BASE_PARAMS)
        p["prompt"] = BASE_PARAMS["prompt"] + "（接续段）"
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
        f = find_seg_file(jid)
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

    # ---- V：首帧=尾帧=图A（两端都钉）----
    print("=== V 段1（flf，首帧=尾帧=图A）===")
    p1 = dict(BASE_PARAMS)
    p1["seed"] = args.seed
    jv1 = submit("flf", p1, refs_both)
    print("  job:", jv1)
    rv = wait_job(jv1)
    print("  status:", rv.get("status"), "err:", (rv.get("error") or "")[:200])
    ctx_v = {}
    sv1 = None
    if rv.get("status") == "done":
        ctx_v = fetch_ctx(jv1).get("continuation") or {}
        f = find_seg_file(jv1)
        sv1 = seam(f) if f else None
        if sv1:
            print(f"  段1: 帧数={sv1['frames']} 中位L1={sv1['median_l1']} "
                  f"最大L1={sv1['max_l1']} @{sv1['max_l1_at_frame']} 比值={sv1['max_l1_ratio']}x")

    # ---- W：只给首帧（只钉首帧）----
    print("=== W 段1（flf，只给首帧=图A）===")
    p1w = dict(BASE_PARAMS)
    p1w["seed"] = args.seed + 10
    jw1 = submit("flf", p1w, refs_first_only)
    print("  job:", jw1)
    rw = wait_job(jw1)
    print("  status:", rw.get("status"), "err:", (rw.get("error") or "")[:200])
    ctx_w = {}
    sw1 = None
    if rw.get("status") == "done":
        ctx_w = fetch_ctx(jw1).get("continuation") or {}
        f = find_seg_file(jw1)
        sw1 = seam(f) if f else None
        if sw1:
            print(f"  段1: 帧数={sw1['frames']} 中位L1={sw1['median_l1']} "
                  f"最大L1={sw1['max_l1']} @{sw1['max_l1_at_frame']} 比值={sw1['max_l1_ratio']}x")

    jV, sV = run_seg("flf", "V 段2 接续（首帧=尾帧=图A，两端都钉）",
                     1, refs_both, ctx_v)
    jW, sW = run_seg("flf", "W 段2 接续（只给首帧，只钉首帧）",
                     11, refs_first_only, ctx_w)

    rec = {"V_both_frames": {"seg1_job": jv1, "seg1": sv1,
                             "seg2_job": jV, "seg2": sV},
           "W_first_only": {"seg1_job": jw1, "seg1": sw1,
                            "seg2_job": jW, "seg2": sW}}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
