#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立复算：r2v 接续链段内跳变（第 2 项取证）。

口径与上轮一致（等价 L1）：
  - 解码成 scaled RGB（1280 宽）
  - 逐帧 mean（numpy）
  - 帧间 L1 = mean(|f[i+1] - f[i]|)

用法：
  python3 seam_probe.py --seg <段2分段mp4> [--seg <段1分段mp4>] [--full <成片mp4>]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys


def probe(path: str, scale_width: int = 1280) -> dict:
    """用 ffmpeg 解码成 rawvideo rgb24，逐帧算 mean 与相邻 L1。"""
    # 先取分辨率
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_frames,r_frame_rate,duration",
         "-of", "json", path],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"ffprobe 失败: {path}\n{out.stderr}")
    info = json.loads(out.stdout)["streams"][0]
    w, h = int(info["width"]), int(info["height"])
    if w != scale_width:
        h = round(h * scale_width / w)
        w = scale_width
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    frame_bytes = w * h * 3
    n = len(raw) // frame_bytes
    try:
        import numpy as np
    except ImportError:
        print("需要 numpy", file=sys.stderr)
        raise
    arr = np.frombuffer(raw[:n * frame_bytes], dtype=np.uint8).reshape(n, h, w, 3)
    means = arr.reshape(n, -1).mean(axis=1)
    l1 = np.abs(arr[1:].astype(np.int16) - arr[:-1].astype(np.int16)).reshape(n - 1, -1).mean(axis=1)
    return {
        "path": path, "frames": n, "width": w, "height": h,
        "duration": float(info.get("duration") or 0.0),
        "nb_frames": info.get("nb_frames"),
        "means": [round(float(x), 4) for x in means],
        "l1": [round(float(x), 4) for x in l1],
    }


def audio_probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,duration,sample_rate,channels",
         "-of", "json", path],
        capture_output=True, text=True)
    try:
        streams = json.loads(out.stdout).get("streams") or []
    except Exception:
        streams = []
    if not streams:
        return {"present": False}
    s = streams[0]
    return {"present": True, "codec": s.get("codec_name"),
            "duration": float(s.get("duration") or 0.0),
            "sample_rate": s.get("sample_rate"), "channels": s.get("channels")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", action="append", default=[], help="分段视频")
    ap.add_argument("--full", action="append", default=[], help="成片视频")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    result = {"segments": [], "fulls": []}
    for p in args.seg:
        d = probe(p)
        d["audio"] = audio_probe(p)
        result["segments"].append(d)
        l1 = d["l1"]
        if l1:
            med = sorted(l1)[len(l1) // 2]
            top = sorted(range(len(l1)), key=lambda i: -l1[i])[:5]
            print(f"\n=== {p}")
            print(f"  frames={d['frames']} size={d['width']}x{d['height']} dur={d['duration']} audio={d['audio'].get('present')}")
            print(f"  段内中位 L1 = {med:.4f}")
            print("  段内 L1 最大的 5 个帧位（1-based 表示「第 i 帧 -> 第 i+1 帧」）:")
            for i in top:
                print(f"    第{i+1}->{i+2}帧  L1={l1[i]:.4f}  "
                      f"亮度 {d['means'][i]:.2f} -> {d['means'][i+1]:.2f} "
                      f"(Δ={d['means'][i+1]-d['means'][i]:+.2f})")
    for p in args.full:
        d = probe(p)
        d["audio"] = audio_probe(p)
        result["fulls"].append(d)
        print(f"\n=== FULL {p}")
        print(f"  frames={d['frames']} size={d['width']}x{d['height']} dur={d['duration']}")
        print(f"  audio={d['audio']}")
        l1 = d["l1"]
        if l1:
            med = sorted(l1)[len(l1) // 2]
            top = sorted(range(len(l1)), key=lambda i: -l1[i])[:6]
            print(f"  成片中位 L1 = {med:.4f}")
            for i in top:
                print(f"    第{i+1}->{i+2}帧  L1={l1[i]:.4f}  "
                      f"亮度 {d['means'][i]:.2f} -> {d['means'][i+1]:.2f}")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
