#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上 E2E：真跑接续链，验证成片帧数**精确**命中用户口径。

【用户口径（最终标准）】
    成片总帧数 = Σ(各段请求秒数 × 24) - (段数-1) × 24
  场景A  5s → 5s       -> 216 帧 = 9.000 秒
  场景B  5s → 5s → 8s  -> 384 帧 = 16.000 秒

【接续上下文怎么来】走**前端同款**路径：
  GET /api/jobs/{上一段id}/continuation  ->  context["continuation"]
  -> 原样作为下一段的 params["continuation"] 提交。

用法：python3 tests/live_cont_exact_frames.py --scenario A|B
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
DATA = Path("/root/projects/comfyui-minimax-h3-studio/data")
OUTDIR = Path("/root/projects/h3studio-live/docs/evidence-exact-frames-20261117")
ASSET_DB = "/mnt/usb2t/asset_library/asset_library.db"
FPS = 24

PROMPT = ("一位少女从低头缓缓抬头看向镜头，镜头缓推至半身，"
          "薄荷绿短发，粉色背心，柔和自然光")


def http_json(path, method="GET", body=None):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode() if body else None,
        method=method, headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def pick_image_asset():
    conn = sqlite3.connect(ASSET_DB)
    conn.row_factory = sqlite3.Row
    try:
        img = conn.execute(
            "SELECT id,name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return dict(img) if img else None


def job_row(job_id):
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def wait_done(job_id, timeout=3600):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        row = job_row(job_id)
        if not row:
            time.sleep(2)
            continue
        if row["status"] in ("done", "failed", "canceled"):
            return row["status"], row["error"], json.loads(row["outputs"] or "[]")
        last = f"{row['status']} {row.get('progress_text') or ''}"
        print(f"      ... {last}", flush=True)
        time.sleep(5)
    return "timeout", last, []


def submit(mode, params, asset_refs):
    boundary = "----h3Exact" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []
    for name, value in (("mode", mode),
                        ("params", json.dumps(params, ensure_ascii=False)),
                        ("asset_refs", json.dumps(asset_refs, ensure_ascii=False))):
        parts.append(f"--{boundary}\r\n".encode()
                     + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                     + value.encode() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return {"_http_error": exc.code, "_body": exc.read().decode()[:600]}


def probe(path):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        encoding="utf-8", timeout=120)
    d = json.loads(raw)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    f = d.get("format") or {}
    return {
        "width": (v or {}).get("width"), "height": (v or {}).get("height"),
        "frames": int((v or {}).get("nb_frames") or 0),
        "fps": (v or {}).get("r_frame_rate"),
        "video_duration": float((v or {}).get("duration") or 0.0),
        "format_duration": float(f.get("duration") or 0.0),
        "has_audio": a is not None,
        "audio_codec": (a or {}).get("codec_name"),
        "size_bytes": int(f.get("size") or 0),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="A", choices=["A", "B"])
    args = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)

    durations = [5.0, 5.0] if args.scenario == "A" else [5.0, 5.0, 8.0]
    target = sum(int(round(d * FPS)) for d in durations) - (len(durations) - 1) * FPS

    img = pick_image_asset()
    if not img:
        print("!! 素材库里没有图片素材")
        return 2

    print(f"===== 场景{args.scenario}: 时长链 {durations} =====")
    print(f"  目标成片 = {target} 帧 = {target / FPS:.4f}s", flush=True)

    jobs = []
    prev_job = ""
    for index, dur in enumerate(durations):
        params = {
            "prompt": PROMPT,
            "width": 1280, "height": 736,
            "duration": dur,
            "seed": 20261117,
            "aspect": "16:9",
            "model_key": "fl2va_pruned_int8",
        }
        asset_refs = [{"asset_id": img["id"], "slot": "first_frame", "ordinal": 0},
                      {"asset_id": img["id"], "slot": "last_frame", "ordinal": 1}]
        if index > 0:
            # 【前端同款】取上一段的接续上下文
            ctx = http_json(f"/api/jobs/{prev_job}/continuation?duration={dur:g}")
            cont_info = ctx.get("continuation") or {}
            if not cont_info:
                print(f"  !! 拿不到接续上下文: {json.dumps(ctx, ensure_ascii=False)[:400]}")
                return 1
            params["continuation"] = cont_info
            plan = ctx.get("duration_plan") or {}
            print(f"  接续上下文: chain={cont_info.get('chain_id')} "
                  f"stage={cont_info.get('stage_index')} "
                  f"服务端预期成片={plan.get('total_frames')} 帧 "
                  f"({plan.get('total_seconds')}s)", flush=True)

        print(f"\n  --- 第{index+1}段（请求 {dur}s）---", flush=True)
        resp = submit("flf", params, asset_refs)
        if "_http_error" in resp:
            print(f"  !! 提交失败 HTTP {resp['_http_error']}: {resp['_body']}")
            return 1
        job_id = resp.get("id") or resp.get("job_id")
        print(f"  job_id = {job_id}", flush=True)
        if not job_id:
            print(f"  !! 提交异常: {resp}")
            return 1
        status, error, outputs = wait_done(job_id)
        print(f"  状态 = {status}", flush=True)
        if status != "done":
            print(f"  !! 失败: {error}")
            return 1
        jobs.append({"job_id": job_id, "duration": dur, "outputs": outputs})
        prev_job = job_id

    last_outputs = jobs[-1]["outputs"]
    full = next((o for o in last_outputs if o.get("is_full")), None)
    if full is None:
        full = next((o for o in last_outputs if o.get("kind") == "video"), None)
    if full is None:
        print("!! 没找到成片产物")
        return 1
    full_path = DATA / "outputs" / full["file"]
    info = probe(full_path)
    print(f"\n  成片 = {full_path}")
    print(f"  ffprobe = {json.dumps(info, ensure_ascii=False)}")

    seg_frames = []
    for entry in jobs:
        seg = next((o for o in entry["outputs"]
                    if o.get("kind") == "video" and not o.get("is_full")), None)
        if seg:
            p = DATA / "outputs" / seg["file"]
            seg_frames.append({"job_id": entry["job_id"],
                               "duration": entry["duration"],
                               "frames": probe(p)["frames"],
                               "path": str(p)})
    for s in seg_frames:
        print(f"  段 {s['job_id']} 请求{s['duration']}s -> {s['frames']} 帧")

    ok = info["frames"] == target
    print(f"\n  >>> {'PASS' if ok else 'FAIL'}: 成片 {info['frames']} 帧 "
          f"= {info['frames']/FPS:.4f}s   目标 {target} 帧 = {target/FPS:.4f}s")

    summary = {"scenario": args.scenario, "durations": durations,
               "target_frames": target, "result": info,
               "segments": seg_frames, "pass": ok,
               "jobs": [j["job_id"] for j in jobs]}
    (OUTDIR / f"scenario{args.scenario}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.copy(full_path, OUTDIR / f"scenario{args.scenario}-full.mp4")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
