#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 2 项**最终验证**：给接续段补上「端点 keyframe」后，跳变是否消失。

【已闭环的根因】
| 变体 | 本段条件 | 段1（非接续） | 段2（接续） |
|---|---|---|---|
| V | 首帧=尾帧（FL2VA，**两端都钉**） | 1.94（无跳变） | **1.68（无跳变）** |
| W | 只给首帧（I2VA，**只钉首帧**） | **81.08（跳变）** | **77.73（跳变）** |
| r2v | 参考图（Ref2VA，**不钉端点**） | 7.13（无跳变） | **84.08（跳变）** |

⇒ **跳变由「本段条件是否钉住画面端点」决定，与接续无关。**
接续机制（拷贝桥钉住区）本身**干净**（V 的接续段无跳变）。

【本实验：验证「补端点 keyframe ⇒ 跳变消失」】
  X. r2v 段1（参考图A）→ r2v 段2（参考图A **+ 首帧=图A**）
     —— 与 U（r2v 段1 → r2v 段2，只有参考图）唯一差别 = 段2 加了首帧。
     若 X 无跳变而 U 有 ⇒ 修复方案成立。

【为什么这个修复方向可行且最小】
`build_r2v` 现在只接 `ref_images`（语义参考）。让它**额外**把「段1 的
末帧」或「首帧素材」接到 T8 的 `first_frame` 槽位（keyframe，钉第 0 帧），
就能把本段的端点钉住 ⇒ 生成区不再自由漂移。
**注意**：这是**生产语义变更**（改了 r2v 的画面约束），本实验先验证效果，
是否采纳由 willis 拍板。

用法：
  python3 tools/r2v_endpoint_keyframe_probe.py --seed 20261801 --json-out /tmp/ekf.json
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
REF_IMAGE = "asset_afb7438fdadf"
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

REFS_R2V = [
    {"asset_id": REF_IMAGE, "slot": "ref_image", "ordinal": 0},
    {"asset_id": REF_VIDEO, "slot": "ref_video", "ordinal": 0},
]
# 段2 加首帧（= 同一张参考图）⇒ T8 会把它当 keyframe 钉在第 0 帧
REFS_R2V_PLUS_FIRST = REFS_R2V + [
    {"asset_id": REF_IMAGE, "slot": "first_frame", "ordinal": 0},
]


def fetch_ctx(jid):
    url = f"{BASE}/api/jobs/{jid}/continuation?duration=5"
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261801)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    print("=== 段1（r2v + 参考图A + 参考视频）===")
    p1 = dict(BASE_PARAMS)
    p1["seed"] = args.seed
    j1 = submit("r2v", p1, REFS_R2V)
    print("  job:", j1)
    r = wait_job(j1)
    print("  status:", r.get("status"), "err:", (r.get("error") or "")[:200])
    if r.get("status") != "done":
        return 1
    ctx = fetch_ctx(j1).get("continuation") or {}
    print("  接续上下文 stage:", ctx.get("stage_index"))

    def run(label, refs, seed_off):
        p = dict(BASE_PARAMS)
        p["prompt"] = BASE_PARAMS["prompt"] + "（接续段）"
        p["original_prompt"] = p["prompt"]
        p["seed"] = args.seed + seed_off
        p["continuation"] = dict(ctx)
        print(f"=== {label} ===")
        jid = submit("r2v", p, refs)
        print("  job:", jid)
        rr = wait_job(jid)
        print("  status:", rr.get("status"), "err:", (rr.get("error") or "")[:300])
        if rr.get("status") != "done":
            return jid, None
        f = locate_segment_file(jid, outputs_of(jid))
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

    jU, sU = run("U 段2 接续（只有参考图，无端点 keyframe）= 现状", REFS_R2V, 1)
    jX, sX = run("X 段2 接续（参考图 **+ 首帧 keyframe**）= 候选修复",
                 REFS_R2V_PLUS_FIRST, 1)

    rec = {"seg1_job": j1,
           "U_no_endpoint": {"job": jU, "seam": sU},
           "X_with_endpoint": {"job": jX, "seam": sX}}
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
