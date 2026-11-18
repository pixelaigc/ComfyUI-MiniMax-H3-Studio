#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""区分「镜头切换」与「色档跳变」。

【为什么必须区分】
最大 L1 大**不等于**缺陷：
  * **镜头切换**（cut）：画面内容突变，但全局亮度/色调连续 —— 这是
    正常的镜头语言，属素材内容特性。
  * **色档跳变**：全局亮度/色调阶跃 —— 这是**缺陷**（画面整体闪一下）。

上一轮 flf 的 +0.717 亮度降之所以要复核，就是因为「L1 大」和
「亮度突变」是两件事。本脚本用**全局亮度跳变幅度**做判据。

【指标】
  d_lum  = |mean(f[i+1]) - mean(f[i])|          跳变处的全局亮度阶跃
  d_lum_med = 该段相邻帧亮度差的**中位绝对值**      该段的正常亮度波动
  ratio  = d_lum / max(d_lum_med, 1e-6)         亮度阶跃相对正常波动的倍数

判读：
  * ratio 高 + L1 大  => 亮度阶跃（缺陷）
  * ratio 低 + L1 大  => 镜头切换（内容特性）

用法：
  python3 tools/seam_kind.py --seg a.mp4 --seg b.mp4
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def frames(path: str, w: int = 1280):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        capture_output=True, text=True)
    st = json.loads(out.stdout)["streams"][0]
    W, H = int(st["width"]), int(st["height"])
    if W != w:
        H = round(H * w / W)
        W = w
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={W}:{H}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    fb = W * H * 3
    n = len(raw) // fb
    import numpy as np
    return np.frombuffer(raw[:n * fb], dtype=np.uint8).reshape(n, H, W, 3)


def analyse(path: str) -> dict:
    import numpy as np
    arr = frames(path)
    n = len(arr)
    means = arr.reshape(n, -1).mean(axis=1)
    l1 = np.abs(arr[1:].astype(np.int16) - arr[:-1].astype(np.int16)).reshape(n - 1, -1).mean(axis=1)
    d_lum = np.abs(means[1:] - means[:-1])
    d_lum_med = float(np.median(d_lum)) if len(d_lum) else 0.0
    top = sorted(range(len(l1)), key=lambda i: -float(l1[i]))[:5]
    out = {
        "path": path, "frames": n,
        "median_l1": round(float(np.median(l1)), 4),
        "median_abs_d_lum": round(d_lum_med, 4),
        "top": [],
    }
    for i in top:
        dl = float(d_lum[i])
        ratio = dl / d_lum_med if d_lum_med > 1e-9 else float("inf")
        out["top"].append({
            "at_frame": i + 1,
            "l1": round(float(l1[i]), 4),
            "d_lum": round(dl, 4),
            "d_lum_ratio": round(ratio, 2) if d_lum_med > 1e-9 else None,
            "lum_from": round(float(means[i]), 2),
            "lum_to": round(float(means[i + 1]), 2),
        })
    out["max_l1"] = out["top"][0]["l1"] if out["top"] else 0.0
    out["max_l1_at"] = out["top"][0]["at_frame"] if out["top"] else 0
    out["max_l1_d_lum_ratio"] = out["top"][0]["d_lum_ratio"] if out["top"] else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", action="append", default=[])
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()
    rows = []
    for p in args.seg:
        r = analyse(p)
        rows.append(r)
        print(f"\n=== {p}")
        print(f"  帧数={r['frames']} 段内中位L1={r['median_l1']} "
              f"相邻帧亮度差中位={r['median_abs_d_lum']}")
        for t in r["top"]:
            ratio = t["d_lum_ratio"]
            rs = f"{ratio}x" if ratio is not None else "inf"
            print(f"    @{t['at_frame']:3d}  L1={t['l1']:8.3f}  "
                  f"Δ亮度={t['d_lum']:7.3f} ({rs} 中位)  "
                  f"{t['lum_from']:7.2f} -> {t['lum_to']:7.2f}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
