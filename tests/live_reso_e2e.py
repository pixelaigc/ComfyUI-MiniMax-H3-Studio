#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上 E2E：在 8190 上真跑一次**大分辨率**的首尾帧(flf)与参考生(r2v)。

【为什么有这个脚本】
2026-11-17 用户报「首尾帧/参考生面板的最大分辨率被限制」。
根因是我们自己的 VIDEO_MODE_SIZE_POLICY 把 flf/r2v 钉在 0.8MP，
而 nonebot seg1/seg2 用的是 ResolutionSelector(megapixels=2) -> 1920x1088。
本脚本是**实测证据**：真的在 8190 上提交 1920x1088 的 flf 与 r2v，
然后 ffprobe 产物，确认像素真的是 1920x1088（而不是被服务端改小）。

【可重复】每次跑都新建任务，产物路径唯一。
用法：python3 tests/live_reso_e2e.py [--mode flf|r2v|both]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
DATA = Path("/root/projects/comfyui-minimax-h3-studio/data")
OUTDIR = Path("/root/projects/h3studio-live/docs/evidence-reso-20261117")
ASSET_DB = "/mnt/usb2t/asset_library/asset_library.db"
ASSET_ROOT = Path("/mnt/usb2t/asset_library")
# 上游 T8 的参考视频最小帧数（core.py:31）。素材库里大量 1 秒短片不满足。
MIN_REF_VIDEO_FRAMES = 48

# nonebot seg1/seg2 的 ResolutionSelector{megapixels: 2, multiple: 32}
# -> 16:9 下就是 1920x1088
TARGET_W, TARGET_H = 1920, 1088


def http_json(path: str, method: str = "GET", body=None):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def pick_assets():
    """素材库里各挑一个真实素材（图片/视频）。

    【为什么视频要按帧数筛，不能只取最新】
    上游 MiniMaxH3AudioConditioningT8 的参考视频策略要求
    **ref_video 至少 48 帧**（core.py:31
    "ref_video_{index} must contain at least 48 frames under the
    official reference policy"）。素材库里有大量 1 秒的 e2e-sample
    短视频（24 帧），若只取「最新一个」，r2v 必被上游拒收
    （ValueError: ref_video_1 must contain at least 48 frames）。
    所以这里按 ffprobe 实测帧数筛选，取第一个 >= 48 帧的素材。
    """
    conn = sqlite3.connect(ASSET_DB)
    conn.row_factory = sqlite3.Row
    try:
        img = conn.execute(
            "SELECT id,name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        vid_rows = conn.execute(
            "SELECT a.id, a.name, v.storage_path FROM assets a "
            "JOIN asset_versions v ON v.asset_id=a.id "
            "WHERE a.category='video' AND a.deleted_at IS NULL "
            "ORDER BY a.created_at DESC").fetchall()
    finally:
        conn.close()

    vid = None
    for r in vid_rows:
        rel = r["storage_path"] or ""
        full = rel if rel.startswith("/") else str(ASSET_ROOT / rel)
        if not Path(full).exists():
            continue
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-count_frames", "-show_entries", "stream=nb_read_frames",
                 "-of", "csv=p=0", full],
                capture_output=True, text=True, timeout=60)
            frames = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if frames.isdigit() and int(frames) >= MIN_REF_VIDEO_FRAMES:
            vid = {"id": r["id"], "name": r["name"],
                   "frames": int(frames), "path": full}
            break
    return (dict(img) if img else None), vid


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
        status = row["status"]
        if status in ("done", "failed", "canceled"):
            return status, row["error"], json.loads(row["outputs"] or "[]")
        last = f"{status} {row.get('progress_text') or ''}"
        print(f"      ... {last}", flush=True)
        time.sleep(5)
    return "timeout", last, []


def submit(mode, params, asset_refs):
    """提交生成任务。

    【契约】POST /api/jobs 是 **multipart**（server.py:api_create）：
      mode        -> 文本
      params      -> JSON 文本
      asset_refs  -> JSON 文本（[{"asset_id","slot","ordinal"}]）
    不是 application/json —— 传 JSON 会被当成 multipart 解析失败。
    """
    boundary = "----h3studioReso" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts: list[bytes] = []

    def add_field(name: str, value: str):
        parts.append(
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode() + b"\r\n")

    add_field("mode", mode)
    add_field("params", json.dumps(params, ensure_ascii=False))
    add_field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def ffprobe(path: str) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    frames = (video or {}).get("nb_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    return {
        "path": path,
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "frames": frames,
        "fps": (video or {}).get("r_frame_rate"),
        "video_duration": float((video or {}).get("duration") or 0.0),
        "format_duration": float(fmt.get("duration") or 0.0),
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
    }


def run_one(mode: str, img_asset: dict, vid_asset: dict) -> dict:
    print(f"\n===== {mode} @ {TARGET_W}x{TARGET_H} =====", flush=True)
    # 【格式说明】asset_refs 是 [{"asset_id","slot","ordinal"}] 结构
    # （server.py:_apply_asset_refs 的契约），不是 "asset:<id>" 字符串。
    if mode == "flf":
        asset_refs = [
            {"asset_id": img_asset["id"], "slot": "first_frame", "ordinal": 0},
            {"asset_id": img_asset["id"], "slot": "last_frame", "ordinal": 1},
        ]
    else:
        asset_refs = [
            {"asset_id": img_asset["id"], "slot": "ref_image", "ordinal": 0},
            {"asset_id": vid_asset["id"], "slot": "ref_video", "ordinal": 0},
        ]

    params = {
        "prompt": ("一位少女站在日式教室讲台前，镜头缓慢推近，薄荷绿短发，"
                   "粉色背心，细腻光影" if mode == "r2v" else
                   "一位少女从低头缓缓抬头看向镜头，镜头缓推至半身，"
                   "薄荷绿短发，粉色背心，柔和自然光"),
        "width": TARGET_W,
        "height": TARGET_H,
        "duration": 5,
        "seed": 20261117,
        "aspect": "16:9",
    }
    if mode == "r2v":
        # r2v 必须用 Ref2VA 通道的权重（服务端会拦 FL2VA-only）。
        # 选 hybrid_fl2va_ref2va —— 与 nonebot seg1/seg2 用的
        # minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors 同款。
        params["model_key"] = "hybrid_fl2va_ref2va"
    # 首尾帧/参考图的来源**只走 asset_refs**（素材库强制引用契约）——
    # 不再另外传 *_asset_id 参数，避免两套来源打架。

    print(f"  提交: mode={mode} {TARGET_W}x{TARGET_H} "
          f"assets={[r['slot'] for r in asset_refs]}", flush=True)
    resp = submit(mode, params, asset_refs)
    job_id = resp.get("id") or resp.get("job_id")
    print(f"  job_id = {job_id}", flush=True)
    if not job_id:
        print(f"  !! 提交失败: {json.dumps(resp, ensure_ascii=False)[:400]}")
        return {"mode": mode, "ok": False, "submit_error": resp}

    status, error, outputs = wait_done(job_id)
    row = job_row(job_id) or {}
    print(f"  状态 = {status}", flush=True)
    if status != "done":
        print(f"  !! 失败: {error}")
        return {"mode": mode, "ok": False, "job_id": job_id,
                "status": status, "error": error,
                "params": row.get("params")}

    # 产物
    # 【契约】jobs.outputs 里视频条目的字段是 {"kind":"video","file":...}，
    # **不是** {"type":"video","path":...}。历史上本脚本只认 path，
    # 导致 flf/r2v 明明跑通却记成 "没有产物路径"（假阴性）。
    # 这里同时接受 file / path 两种键，file 优先。
    video_out = None
    for o in outputs:
        if not isinstance(o, dict):
            continue
        rel = o.get("file") or o.get("path") or ""
        if o.get("kind") == "video" or o.get("type") == "video" \
                or str(rel).endswith(".mp4"):
            video_out = o
            break
    if video_out is None:
        for o in outputs:
            if isinstance(o, dict) and (o.get("file") or o.get("path")):
                video_out = o
                break

    rel = (video_out or {}).get("file") or (video_out or {}).get("path")
    path = str(rel) if rel else ""
    if path and not path.startswith("/"):
        # 【契约】jobs.outputs[].file 是相对 **data_dir/outputs** 的路径
        # （server.py: OUT_DIR = DATA / "outputs"；full_rel 形如
        #  "videos/v-h3-studio-000535.mp4"）。
        # 历史脚本误拼成 DATA/rel -> "data/videos/xxx.mp4" -> ffprobe 报
        # "没有产物路径"（假阴性）。这里按 OUT_DIR 拼。
        path = str(DATA / "outputs" / path)
    probe = ffprobe(path) if path else {"error": "没有产物路径"}
    print(f"  产物 = {path}")
    print(f"  ffprobe = {json.dumps(probe, ensure_ascii=False)}")

    # 落库参数（证明服务端没有把尺寸改小）
    stored = row.get("params")
    try:
        stored = json.loads(stored) if stored else {}
    except (TypeError, ValueError):
        stored = {}
    print(f"  落库 width/height = {stored.get('width')}x{stored.get('height')}")

    ok = (probe.get("width") == TARGET_W
          and probe.get("height") == TARGET_H)
    verdict = "PASS" if ok else "FAIL"
    print(f"  >>> {verdict}: 产物 {probe.get('width')}x{probe.get('height')} "
          f"(目标 {TARGET_W}x{TARGET_H})", flush=True)

    return {"mode": mode, "ok": ok, "job_id": job_id, "status": status,
            "params": stored, "probe": probe, "path": path}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="both",
                    choices=["flf", "r2v", "both"])
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    img, vid = pick_assets()
    print(f"素材: img={img} vid={vid}")
    if not img:
        print("!! 素材库里没有图片素材，无法实测")
        return 2

    modes = ["flf", "r2v"] if args.mode == "both" else [args.mode]
    results = []
    for mode in modes:
        if mode == "r2v" and not vid:
            print("跳过 r2v（素材库里没有视频素材）")
            continue
        results.append(run_one(mode, img, vid))

    summary = {
        "target": f"{TARGET_W}x{TARGET_H}",
        "nonebot_seg_reference": "ResolutionSelector{megapixels:2, multiple:32}",
        "results": results,
    }
    (OUTDIR / "live_reso_e2e.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总 =====")
    for r in results:
        p = r.get("probe", {})
        print(f"  {r['mode']}: {'PASS' if r.get('ok') else 'FAIL'} "
              f"{p.get('width')}x{p.get('height')} "
              f"{p.get('frames')}帧 {p.get('format_duration')}s "
              f"-> {r.get('path')}")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
