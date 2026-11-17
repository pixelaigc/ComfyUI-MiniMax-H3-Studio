#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接缝的**逐帧证据**导出（2026-11-17 二采接缝色档残缺陷）。

产出：
  * 接续点前后各 N 帧的 PNG（可直接目检）
  * 接续点前后各 N 帧的**逐帧量化表**（CSV）
  * 段2 头 12 帧的**逐帧亮度 + 高频能量**剖面（这是断点的真凶）

用法：
  python3 tools/seam_frame_evidence.py <full.mp4> <seg1.mp4> <seg2.mp4> <out_dir>
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

FPS = 24


def probe_size(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    st = json.loads(out)["streams"][0]
    return int(st["width"]), int(st["height"])


def decode(path: str) -> np.ndarray:
    w, h = probe_size(path)
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
         "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)


def hf_energy(frames: np.ndarray) -> np.ndarray:
    g = frames.astype(np.float32).mean(axis=3)
    p = np.pad(g, ((0, 0), (1, 1), (1, 1)), mode="edge")
    blur = (p[:, :-2, :-2] + p[:, :-2, 1:-1] + p[:, :-2, 2:] +
            p[:, 1:-1, :-2] + p[:, 1:-1, 1:-1] + p[:, 1:-1, 2:] +
            p[:, 2:, :-2] + p[:, 2:, 1:-1] + p[:, 2:, 2:]) / 9.0
    return np.abs(g - blur).reshape(len(g), -1).mean(axis=1)


def export_png(video: Path, frame_idx: int, out: Path, label: str):
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video),
         "-vf", f"select=eq(n\\,{frame_idx})", "-vframes", "1",
         "-q:v", "2", str(out)], check=True, capture_output=True)
    return out


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__)
        return 1
    full_p, seg1_p, seg2_p, out_dir = sys.argv[1:5]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    full = decode(full_p)
    s1 = decode(seg1_p)
    s2 = decode(seg2_p)
    seam = len(s1)

    hf_full = hf_energy(full)
    hf_s2 = hf_energy(s2)
    m_full = full.astype(np.float32).reshape(len(full), -1, 3).mean(axis=1)
    m_s2 = s2.astype(np.float32).reshape(len(s2), -1, 3).mean(axis=1)
    lum_full = (full.astype(np.float32) @
                np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)).mean(axis=(1, 2))

    # ---- ① 接续点前后各 8 帧 PNG ----
    win = 8
    for i in range(max(0, seam - win), min(len(full), seam + win)):
        tag = f"seam_{i:04d}" + ("_SEAM" if i == seam else "")
        export_png(Path(full_p), i, out / f"full_{tag}.png", tag)
    for i in range(min(12, len(s2))):
        export_png(Path(seg2_p), i, out / f"seg2_head_{i:04d}.png", f"seg2_{i}")

    # ---- ② 成片逐帧量化表（缝前 12 + 缝后 12）----
    # 相邻帧差用 **逐通道绝对差之和（L1）** —— 与 seam_metric.py 口径一致。
    diffs_full = np.abs(np.diff(m_full, axis=0)).sum(axis=1)
    rows = []
    for i in range(max(0, seam - 12), min(len(full), seam + 12)):
        d = float(diffs_full[i - 1]) if i > 0 else 0.0
        rows.append({
            "frame": i, "role": "seam" if i == seam else ("before" if i < seam else "after"),
            "mean_r": round(float(m_full[i][0]), 4),
            "mean_g": round(float(m_full[i][1]), 4),
            "mean_b": round(float(m_full[i][2]), 4),
            "luma": round(float(lum_full[i]), 4),
            "hf_energy": round(float(hf_full[i]), 5),
            "diff_to_prev_l1": round(d, 5),
        })
    with (out / "full_frames_around_seam.csv").open("w", newline="", encoding="utf-8") as f:
        wcsv = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wcsv.writeheader()
        wcsv.writerows(rows)

    # ---- ③ 段2 头 12 帧剖面（断点的真凶在这里）----
    diffs_s2 = np.abs(np.diff(m_s2, axis=0)).sum(axis=1)
    rows2 = []
    for i in range(min(14, len(s2))):
        d = float(diffs_s2[i - 1]) if i > 0 else 0.0
        rows2.append({
            "seg2_frame": i,
            "mean_r": round(float(m_s2[i][0]), 4),
            "mean_g": round(float(m_s2[i][1]), 4),
            "mean_b": round(float(m_s2[i][2]), 4),
            "hf_energy": round(float(hf_s2[i]), 5),
            "diff_to_prev_l1": round(d, 5),
        })
    with (out / "seg2_head_profile.csv").open("w", newline="", encoding="utf-8") as f:
        wcsv = csv.DictWriter(f, fieldnames=list(rows2[0].keys()))
        wcsv.writeheader()
        wcsv.writerows(rows2)

    summary = {
        "full": str(full_p), "seg1": str(seg1_p), "seg2": str(seg2_p),
        "frames": {"full": len(full), "seg1": len(s1), "seg2": len(s2),
                   "conserved": len(full) == len(s1) + len(s2)},
        "seam_frame_index": seam,
        "seam_jump_l1": round(float(diffs_full[seam - 1]), 5),
        "seg2_head_break_l1": round(float(diffs_s2[0]), 5),
        "seg2_head_hf": [round(float(x), 5) for x in hf_s2[:4]],
        "seg2_mid_hf": round(float(hf_s2[len(s2) // 2]), 5),
        "seg2_head_luma": [round(float(lum_full[seam + i]), 4) for i in range(min(10, len(s2)))],
        "png_count": len(list(out.glob("*.png"))),
        "out_dir": str(out),
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
