#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 项根因的**第二轮**定位：参考图语义 × 钉住区。

【为什么需要这一轮】
2026-11-19 第一轮定位到「参考视频画面切片被绑在音轨来源上」，据此做了修复
（画面切片与音轨解耦 + 参考视频不够长时丢弃）。**部署后在线复验显示：
修复确实生效**（段2 落库 `ref_video_frame_count=50`、`chain_total_frames=124`
⇒ 参考视频被丢弃，B/C/D 三段产出字节级一致），**但跳变仍在**
（@14，L1=49.51，Δ亮度 +11.90 = 27.59× 该段中位）。

⇒ 参考视频只是**部分**成因，主因还在。

【这一轮要区分的两种可能】
  H3「参考图语义 × 钉住区」
     r2v 的参考图是 **CLIP 语义参考**（`minimax_ref_items`），
     要求整段画面都「像那张图」；而拷贝桥把段1 的尾部**硬钉**在前 22 帧
     （钉住区是「段1 已演化的画面」）。
     ⇒ 两者在钉住区边界正面冲突 ⇒ 爆发式跳变。
     **判据**：段2 换一张**不同的**参考图 ⇒ 跳变的**位置/幅度**随之改变。

  H4「r2v 条件节点整体（与参考图无关）」
     r2v 的 T8 走 Ref2VA 任务类型，flf 走 FL2VA —— 若差异来自
     任务类型/条件节点本身，则换参考图**不影响**跳变。

【对照设计】
  P. 段1（r2v + 参考图A）→ 段2（接续，参考图A）    现状
  Q. 段1（r2v + 参考图A）→ 段2（接续，参考图B）    只换参考图
  R. 段1（r2v + 参考图A）→ 对照段（非接续，参考图A） 同 seed 非接续

【为什么 Q 是决定性的】
P 与 Q 的唯一差别 = 段2 的参考图。P/Q 的跳变差异直接归因于参考图语义。
若 P ≈ Q ⇒ H3 不成立（跳变与参考图无关）⇒ 指向 H4。

用法：
  python3 tools/r2v_refimage_probe.py --seed 20261501 --json-out /tmp/refimg.json
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
REF_IMAGE_A = "asset_afb7438fdadf"     # i-zimage-000301.png（上一轮用的）
REF_IMAGE_B = "asset_6af34f4016bc"     # i-zimage-000351.png（另一张）
REF_VIDEO = "asset_486ce2e0f061"


def fetch_ctx(jid):
    url = f"{BASE}/api/jobs/{jid}/continuation?duration=5"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261501)
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
    refs_a = [
        {"asset_id": REF_IMAGE_A, "slot": "ref_image", "ordinal": 0},
        {"asset_id": REF_VIDEO, "slot": "ref_video", "ordinal": 0},
    ]
    refs_b = [
        {"asset_id": REF_IMAGE_B, "slot": "ref_image", "ordinal": 0},
        {"asset_id": REF_VIDEO, "slot": "ref_video", "ordinal": 0},
    ]

    print("=== 段1（r2v + 参考图A + 参考视频）===")
    j1 = submit("r2v", dict(base), refs_a)
    print("  job:", j1)
    r = wait_job(j1)
    print("  status:", r.get("status"), "err:", (r.get("error") or "")[:200])
    if r.get("status") != "done":
        return 1
    ctx = fetch_ctx(j1).get("continuation") or {}
    print("  接续上下文 stage:", ctx.get("stage_index"))

    def run(label, seed_off, refs, is_ctrl=False):
        p = dict(base)
        p["prompt"] = base["prompt"] + ("（接续段）" if not is_ctrl else "")
        p["original_prompt"] = p["prompt"]
        p["seed"] = args.seed + seed_off
        if not is_ctrl:
            p["continuation"] = dict(ctx)
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

    jP, sP = run("P. 段2 接续（参考图A = 与段1 相同）", 1, refs_a)
    jQ, sQ = run("Q. 段2 接续（参考图B = 与段1 不同）", 1, refs_b)
    jR, sR = run("R. 对照段（非接续，参考图A，同 seed）", 1, refs_a, is_ctrl=True)

    rec = {"seg1_job": j1,
           "P_same_image": {"job": jP, "seam": sP},
           "Q_other_image": {"job": jQ, "seam": sQ},
           "R_control": {"job": jR, "seam": sR}}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
