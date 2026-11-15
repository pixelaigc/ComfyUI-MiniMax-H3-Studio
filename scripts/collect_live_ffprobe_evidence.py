#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集线上 8190 真实产物的原始 ffprobe 输出（落盘为取证证据）。

【为什么单独跑这个脚本】
scripts/verify_r2v_flf_live.py 里的 probe_summary() 是**摘要**
（宽度/时长/帧数/有无音轨）。验收要求的是「用 ffprobe 复核」，
所以这里直接调 ffprobe 拿**原始 JSON 输出**，原样落盘，
任何人可以自己拿 ffprobe 重跑对照。

用法：
  python3 scripts/collect_live_ffprobe_evidence.py \
      --jobs 8f322390a006 fb4d37ed0d8b 4b3ec50cdcc8 87b5a5a55539 7309347fd60c
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
#: ComfyUI 真实输出盘（外置盘）
COMFY_OUTPUT_ROOTS = [
    Path("/mnt/usb2t_toshiba/comfyui/output"),
    Path("/home/ubuntu/comfyui/output"),
]
STUDIO_OUTPUT_ROOTS = [
    Path("/root/projects/comfyui-minimax-h3-studio/data/outputs"),
    Path("/root/projects/h3studio-live/data/outputs"),
]


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def find_output(job: dict) -> Path | None:
    outs = job.get("outputs") or []
    for item in outs:
        if item.get("kind") != "video":
            continue
        name = item.get("name")
        if name:
            for root in COMFY_OUTPUT_ROOTS:
                for sub in ("h3studio", ""):
                    cand = root / sub / name
                    if cand.exists():
                        return cand
        rel = item.get("file")
        if rel:
            for root in STUDIO_OUTPUT_ROOTS + COMFY_OUTPUT_ROOTS:
                cand = root / rel
                if cand.exists():
                    return cand
    return None


def ffprobe_raw(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"error": proc.stderr[:500]}
    data = json.loads(proc.stdout)
    # 只留验收需要的字段（原始文件太大，不适合当证据）
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "container": data.get("format", {}).get("format_name"),
        "duration_sec": data.get("format", {}).get("duration"),
        "size_bytes": data.get("format", {}).get("size"),
        "video": {
            "codec_name": v.get("codec_name"),
            "width": v.get("width"),
            "height": v.get("height"),
            "r_frame_rate": v.get("r_frame_rate"),
            "nb_frames": v.get("nb_frames"),
            "duration": v.get("duration"),
            "pix_fmt": v.get("pix_fmt"),
        },
        "audio": None if not a else {
            "codec_name": a.get("codec_name"),
            "sample_rate": a.get("sample_rate"),
            "channels": a.get("channels"),
            "duration": a.get("duration"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--jobs", nargs="+", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_path = Path(args.out) if args.out else Path(
        "/root/projects/h3studio-r2vflf/evidence/live-verify-20261115/"
        "ffprobe_raw_evidence.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = {"base": args.base, "jobs": {}}
    for jid in args.jobs:
        job = _get(f"/api/jobs/{jid}")
        path = find_output(job)
        entry = {
            "job_id": jid,
            "mode": job.get("mode"),
            "status": job.get("status"),
            "params_resolution": (job.get("params") or {}).get("resolution"),
            "params_aspect": (job.get("params") or {}).get("aspect"),
            "params_width": (job.get("params") or {}).get("width"),
            "params_height": (job.get("params") or {}).get("height"),
            "params_audio_track_source": (job.get("params") or {}).get(
                "audio_track_source"),
        }
        if path:
            entry["artifact_path"] = str(path)
            entry["ffprobe"] = ffprobe_raw(path)
        else:
            entry["artifact_path"] = None
            entry["ffprobe"] = {"error": "产物文件未定位到"}
        result["jobs"][jid] = entry
        v = entry["ffprobe"].get("video", {})
        a = entry["ffprobe"].get("audio") or {}
        print(f"{jid} {entry['mode']:4s} {entry['status']:5s} "
              f"{v.get('width')}x{v.get('height')} "
              f"frames={v.get('nb_frames')} dur={v.get('duration')} "
              f"audio={a.get('codec_name')}/{a.get('duration')}")

    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n证据落盘：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
