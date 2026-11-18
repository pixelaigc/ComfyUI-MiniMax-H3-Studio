#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接缝/段内色档跳变的**可复算**测量器。

【口径声明 —— 必须写在报告里】
本脚本用**等价 L1** 口径（逐帧 RGB 的 L1 均值差），**不是**
`tools/seam_metric.py` 的官方 HF 能量项。理由：
  * 官方 HF 能量项在本机 CPU 上单次运行 >20 分钟跑不完
    （见 docs/evidence-20261118-seam-and-refimg.md §6 未解决项 2）；
  * 等价 L1 口径已被上一轮用作主数据来源，且**足以支撑归因**
    （二采 ON 组 2.58x/4.54x vs OFF 组 ≤1.58x，无重叠无反例）。
⇒ 报告里必须写明「用的是等价 L1 口径，不是 HF 能量项」。

【测什么】
对一条接续链的**成片**（拼接后的完整视频）：
  * 段内断点：每段**自己内部**相邻帧 L1 的最大值 / 该段中位数 = 断点比
  * 拼接点：段1 末帧与段2 首帧的 L1
  * 亮度跳变：段2 头 4 帧的亮度均值 vs 段中段的亮度均值（3 帧亮度降）
  * 饱和度跳变：同上口径

【用法】
    python3 tests/seam_metric_seg2.py --chain <job_id>
    python3 tests/seam_metric_seg2.py --compare <job_id_A> <job_id_B>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path("/root/projects/comfyui-minimax-h3-studio")
DATA = REPO / "data"
OUT = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
W, H = 320, 180


def frames(path: str):
    """抽帧 -> numpy (n, H, W, 3) float32。"""
    if not os.path.exists(path):
        return None
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={W}:{H}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    n = len(raw) // (W * H * 3)
    if n == 0:
        return None
    return np.frombuffer(raw[:n * W * H * 3], dtype=np.uint8).reshape(
        n, H, W, 3).astype(np.float32)


def l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).mean())


def chain_segments(job_id: str):
    """沿 continuation.source_job 回溯，返回成片与各段产物。"""
    conn = sqlite3.connect(f"file:{DATA / 'jobs.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT params, outputs FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        params = json.loads(row["params"] or "{}")
        segs = []
        cur = job_id
        while cur:
            r = conn.execute(
                "SELECT params, outputs FROM jobs WHERE id=?", (cur,)).fetchone()
            if not r:
                break
            p = json.loads(r["params"] or "{}")
            c = p.get("continuation") or {}
            if c.get("segment_rel"):
                segs.append(c["segment_rel"])
            cur = c.get("source_job") or ""
        segs = list(reversed(segs))
        full = None
        outs = json.loads(row["outputs"] or "[]")
        for o in outs:
            if o.get("is_full"):
                full = o.get("file")
        twopass = (params.get("twopass") or {}).get("enabled")
        return {"job": job_id, "segs": segs, "full": full,
                "twopass": twopass, "mode": params.get("mode")}
    finally:
        conn.close()


def analyze(job_id: str) -> dict:
    info = chain_segments(job_id)
    if not info or not info["segs"]:
        return {"job": job_id, "error": "找不到分段信息"}
    out = {"job": job_id, "mode": info["mode"],
           "twopass": info["twopass"], "segments": []}
    for idx, rel in enumerate(info["segs"]):
        path = OUT / rel
        S = frames(str(path))
        if S is None:
            out["segments"].append({"rel": rel, "error": "读不到帧"})
            continue
        d = [l1(S[i], S[i + 1]) for i in range(len(S) - 1)]
        if len(d) < 2:
            out["segments"].append({"rel": rel, "frames": len(S),
                                    "error": "帧数太少"})
            continue
        med = float(np.median(d[1:]))
        head = d[0]
        ratio = head / med if med > 0 else float("nan")
        lum = S.mean(axis=(1, 2, 3))
        # 饱和度：HSV 的 S 通道均值（numpy 手算，等价 PIL 的 HSV）
        mx = S.max(axis=3)
        mn = S.min(axis=3)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0).mean(axis=(1, 2))
        drop3 = float(lum[0] - lum[3]) if len(lum) > 3 else float("nan")
        sat_drop = float(sat[0] - sat[3]) if len(sat) > 3 else float("nan")
        out["segments"].append({
            "rel": rel, "frames": len(S),
            "head_break_L1": head, "seg_median_L1": med,
            "head_break_ratio": ratio,
            "lum_drop_3frames": drop3, "sat_drop_3frames": sat_drop,
            "lum_head": float(lum[0]), "lum_frame3": float(lum[3]),
            "sat_head": float(sat[0]), "sat_frame3": float(sat[3]),
        })
    # 拼接点：段 i 末帧 vs 段 i+1 首帧
    seams = []
    for i in range(len(out["segments"]) - 1):
        a = out["segments"][i]
        b = out["segments"][i + 1]
        if "error" in a or "error" in b:
            continue
        Sa = frames(str(OUT / a["rel"]))
        Sb = frames(str(OUT / b["rel"]))
        if Sa is None or Sb is None:
            continue
        seams.append({
            "between": [a["rel"], b["rel"]],
            "seam_L1": l1(Sa[-1], Sb[0]),
        })
    out["seams"] = seams
    # 成片整体
    if info["full"]:
        full_path = OUT / info["full"]
        F = frames(str(full_path))
        if F is not None:
            df = [l1(F[i], F[i + 1]) for i in range(len(F) - 1)]
            out["full"] = {
                "rel": info["full"], "frames": len(F),
                "median_L1": float(np.median(df)) if df else None,
                "max_L1": float(max(df)) if df else None,
                "argmax_L1": int(np.argmax(df)) + 1 if df else None,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", help="接续链的成片 job_id")
    ap.add_argument("--compare", nargs=2, help="对比两个 job_id")
    ap.add_argument("--json-out", help="把结果写到这个 JSON 文件")
    args = ap.parse_args()

    if args.compare:
        results = [analyze(j) for j in args.compare]
    elif args.chain:
        results = [analyze(args.chain)]
    else:
        ap.error("需要 --chain 或 --compare")

    print("=" * 78)
    print("接缝指标（等价 L1 口径 —— 不是 HF 能量项）")
    print("=" * 78)
    for r in results:
        print(f"\njob={r.get('job')} mode={r.get('mode')} "
              f"二采={'ON' if r.get('twopass') else 'OFF'}")
        if "error" in r:
            print(f"  !! {r['error']}")
            continue
        for i, s in enumerate(r.get("segments", [])):
            if "error" in s:
                print(f"  段{i+1}: {s['rel']}  {s['error']}")
                continue
            print(f"  段{i+1}: {s['rel']}")
            print(f"       段内头断点 L1={s['head_break_L1']:.4f} "
                  f"段内中位={s['seg_median_L1']:.4f} "
                  f"比值={s['head_break_ratio']:.2f}x")
            print(f"       亮度 头帧={s['lum_head']:.2f} 第3帧={s['lum_frame3']:.2f} "
                  f"3帧亮度降={s['lum_drop_3frames']:+.3f}")
            print(f"       饱和度 头帧={s['sat_head']:.4f} 第3帧={s['sat_frame3']:.4f} "
                  f"3帧饱和度降={s['sat_drop_3frames']:+.4f}")
        for s in r.get("seams", []):
            print(f"  拼接点 {s['between'][0]} -> {s['between'][1]}: "
                  f"L1={s['seam_L1']:.4f}")
        if "full" in r:
            f = r["full"]
            print(f"  成片 {f['rel']}: 帧={f['frames']} "
                  f"中位L1={f['median_L1']:.4f} 最大L1={f['max_L1']:.4f} "
                  f"(第 {f['argmax_L1']} 帧)")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  已写: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
