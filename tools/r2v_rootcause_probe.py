#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 项根因的决定性对照实验。

【要区分的两种成因】
  H1「参考视频条件 × 钉住区冲突」
     段2 的 T8 条件节点带着参考视频（其画面应接段1 的时间位置），
     而拷贝桥又把前 22 帧硬钉成段1 的尾部 ⇒ 两股力量在前若干帧冲突
     ⇒ 爆发式跳变。
     **判据**：段2 **去掉参考视频**（只留参考图）后跳变消失。

  H2「拷贝桥本身」
     跳变跟参考视频无关，是拷贝桥钉住区与新生成区的固有效应。
     **判据**：去掉参考视频后跳变**依然存在**。

【实验设计】
  A. 段1：r2v + 参考图 + 参考视频            （与扩样本同）
  B. 段2：接续段1，带参考视频                 （复现跳变）
  C. 段2'：接续段1，**不带**参考视频（只留参考图）  <- 关键对照
  D. 段2''：接续段1，**完全不带任何参考媒体**（纯 flf 式的接续）

【为什么 C/D 要用同一段1】
钉住区内容来自段1 的潜空间 —— 只要段1 相同，C/D 的钉住区就与 B 相同，
差异只在「条件节点带不带参考媒体」⇒ 单变量。

用法：
  python3 tools/r2v_rootcause_probe.py --seed 20261301 --json-out /tmp/rc.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.r2v_chain_runner import (  # noqa: E402
    submit, wait_job, db_params, outputs_of, locate_segment_file, seam,
)

BASE = "http://127.0.0.1:8190"
REF_IMAGE = "asset_afb7438fdadf"
REF_VIDEO = "asset_486ce2e0f061"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261301)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    base = {
        "prompt": "教室里的日常，少女走上讲台",
        "original_prompt": "教室里的日常，少女走上讲台",
        "model_key": "hybrid_fl2va_ref2va",
        "width": 1280, "height": 736,
        "resolution": "736p", "aspect": "16:9",
        "duration": 5, "steps": 4,
        "sampler": "euler", "scheduler": "beta",
        "shift_video": 12.0, "shift_audio": 12.0,
        "seed": args.seed,
        "audio_track_source": "generated",
        "twopass": {"enabled": False},
        "accel_lora": "", "accel_lora_strength": 1.0,
        "accel_loras": [], "effect_loras": [],
    }
    refs_with_video = [
        {"asset_id": REF_IMAGE, "slot": "ref_image", "ordinal": 0},
        {"asset_id": REF_VIDEO, "slot": "ref_video", "ordinal": 0},
    ]
    refs_image_only = [
        {"asset_id": REF_IMAGE, "slot": "ref_image", "ordinal": 0},
    ]

    import urllib.request
    ctx = None

    def fetch_ctx(jid):
        url = f"{BASE}/api/jobs/{jid}/continuation?duration=5"
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode())

    results = {}

    print("=== A. 段1（r2v + 参考图 + 参考视频）===")
    j1 = submit("r2v", dict(base), refs_with_video)
    print("  job:", j1)
    r = wait_job(j1)
    print("  status:", r.get("status"), "err:", (r.get("error") or "")[:200])
    if r.get("status") != "done":
        return 1
    ctx = fetch_ctx(j1).get("continuation") or {}
    print("  接续上下文 ok, stage:", ctx.get("stage_index"))

    def run_seg(label, seed_off, refs, extra=None):
        p = dict(base)
        p["prompt"] = base["prompt"] + "（接续段）"
        p["original_prompt"] = p["prompt"]
        p["seed"] = args.seed + seed_off
        p["continuation"] = dict(ctx)
        if extra:
            p.update(extra)
        print(f"=== {label} ===")
        jid = submit("r2v", p, refs)
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
        for t in s["top3"]:
            print(f"     @{t['at_frame']:3d} L1={t['l1']:8.3f} "
                  f"亮度 {t['lum_from']:7.2f} -> {t['lum_to']:7.2f} (Δ={t['d_lum']:+.2f})")
        return jid, s

    jB, sB = run_seg("B. 段2 接续（带参考视频）= 复现跳变", 1, refs_with_video)
    jC, sC = run_seg("C. 段2 接续（**只有参考图**，无参考视频）", 1, refs_image_only)
    # D：完全不带参考媒体 —— r2v 至少要 1 个参考素材，所以用参考图 + 空视频槽
    jD, sD = run_seg("D. 段2 接续（参考图 + 参考视频，但**音轨来源=reference**）",
                     1, refs_with_video,
                     {"audio_track_source": "reference"})

    rec = {"seg1_job": j1,
           "B": {"job": jB, "seam": sB},
           "C": {"job": jC, "seam": sC},
           "D": {"job": jD, "seam": sD}}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
