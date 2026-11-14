#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H3-Studio 端到端成片收尾：等片段 -> 合成长片 -> 铺自产音轨 -> 落证据。

【为什么长片合成后还要铺音轨】Studio 的长片拼接用 ffmpeg concat ，
片段自带音轨会被 concat 直接串起来（音画不同步、且各片段音轨来源不同）。
成片需要一条**完整自产音轨**（本轮 YuE2 产物）铺在上面 —— 这一步
用 ffmpeg 完成，音轨源是素材库里的自产 FLAC。

【顺序】
  1. 等所有 r2v 片段 done（轮询 /api/jobs）
  2. POST /api/longfilms 合成（服务端 ffmpeg concat + 统一尺寸/帧率）
  3. 等长片任务 done
  4. ffmpeg 把自产音轨铺到成片（-shortest 取音频与画面较短者，
     末尾再补静音到满 120 秒，保证成片正好 2 分钟）
  5. 把成片 + 音轨 + 任务清单落到 evidence 目录
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
#: 证据目录（与 produce_h3_shortfilm.py 同一个约定）。
#:
#: 【2026-11-15 改 —— 可移植】原来写死成另一个 worktree 的路径；
#: 现在默认落在**本仓库自己的** evidence 目录，
#: 环境变量 H3_SHORTFILM_EV 可覆盖。
EV = Path(os.environ.get("H3_SHORTFILM_EV")
          or (Path(__file__).resolve().parent.parent
              / "evidence" / "h3-shortfilm"))
#: 自产 YuE2 音乐（149.88s）—— 铺到成片上的那条音轨。
#:
#: 【2026-11-15】原来写死成 EV 目录下的固定文件名；
#: 现在可以用 H3_SHORTFILM_MUSIC 指向任意本地音频文件，
#: 默认仍取证据目录里的那一份（不存在时脚本会明确报错说明）。
MUSIC_LOCAL = Path(os.environ.get("H3_SHORTFILM_MUSIC")
                    or (EV / "a-yue2-000050.flac"))
TARGET_SEC = 120.0                              # 成片目标 2 分钟


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _post_json(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def wait_clips(job_ids: list[str], timeout_sec: float) -> list[dict]:
    deadline = time.time() + timeout_sec
    ready: list[dict] = []
    while time.time() < deadline:
        done, pending, failed = [], [], []
        for jid in job_ids:
            try:
                job = _get(f"/api/jobs/{jid}")
            except Exception:
                pending.append(jid)
                continue
            status = job.get("status")
            if status == "done":
                done.append(job)
            elif status == "failed":
                failed.append({"job_id": jid, "error": (job.get("error") or "")[:300]})
            else:
                pending.append(jid)
        ready = done
        print(f"[wait] done={len(done)} pending={len(pending)} failed={len(failed)} "
              f"({time.strftime('%H:%M:%S')})", flush=True)
        if not pending:
            if failed:
                print("[failed jobs]", json.dumps(failed, ensure_ascii=False))
            break
        time.sleep(25)
    return ready


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True, help="逗号分隔的 r2v job id")
    ap.add_argument("--timeout", type=float, default=14400)
    args = ap.parse_args()
    job_ids = [x.strip() for x in args.jobs.split(",") if x.strip()]

    EV.mkdir(parents=True, exist_ok=True)
    clips = wait_clips(job_ids, args.timeout)
    if len(clips) < 2:
        print(f"[abort] 只有 {len(clips)} 个片段可用，不足 2 个，无法合成长片")
        return 1

    payload = {
        "title": "火星基地 · 两分钟",
        "intro": "H3-Studio 端到端实测：自产参考图（Z-Image 文生图）+ 自产音乐（YuE2）",
        "clips": [{"job_id": j["id"], "file": (j.get("outputs") or [{}])[0].get("file"),
                   "name": (j.get("outputs") or [{}])[0].get("name", "")}
                  for j in clips],
    }
    (EV / "longfilm_payload.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    resp = _post_json("/api/longfilms", payload)
    lf_id = resp["id"]
    print(f"[longfilm] submitted {lf_id}  clips={len(payload['clips'])}", flush=True)

    deadline = time.time() + 3600
    final = None
    while time.time() < deadline:
        job = _get(f"/api/jobs/{lf_id}")
        print(f"[longfilm] {job.get('status')} | {job.get('progress_text','')}", flush=True)
        if job.get("status") in ("done", "failed"):
            final = job
            break
        time.sleep(15)
    if not final or final.get("status") != "done":
        print("[abort] 长片合成失败:", json.dumps(final or {}, ensure_ascii=False)[:400])
        return 2

    out = (final.get("outputs") or [{}])[0]
    raw = EV / "longfilm_raw.mp4"
    with urllib.request.urlopen(f"{BASE}/files/{out['file']}", timeout=300) as resp, \
            open(raw, "wb") as fh:
        fh.write(resp.read())
    print(f"[longfilm] raw -> {raw} ({raw.stat().st_size} B)")

    # 铺自产音轨 + 补静音到 120 秒
    final_mp4 = EV / "H3Studio_火星基地_2分钟_480P.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-i", str(raw),
        "-i", str(MUSIC_LOCAL),
        "-filter_complex",
        f"[1:a]apad,atrim=0:{TARGET_SEC},asetpts=N/SR/TB[a];"
        f"[0:v]scale=864:480:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad=864:480:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p[v]",
        "-map", "[v]", "-map", "[a]",
        "-t", str(TARGET_SEC),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-movflags", "+faststart", str(final_mp4),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print("[ffmpeg] failed:", proc.stderr[-800:])
        return 3
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,channels",
         "-of", "default=noprint_wrappers=1", str(final_mp4)],
        capture_output=True, text=True)
    print("[ffmpeg] ok ->", final_mp4, final_mp4.stat().st_size, "B")
    print(probe.stdout)
    (EV / "final_film_probe.txt").write_text(probe.stdout, encoding="utf-8")
    (EV / "longfilm_job.json").write_text(
        json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[done] final film:", final_mp4)
    return 0


if __name__ == "__main__":
    sys.exit(main())
