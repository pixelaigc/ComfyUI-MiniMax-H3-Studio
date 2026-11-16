# -*- coding: utf-8 -*-
"""线上 8190 真实跑通：flf 接续（段1→段2）+ r2v 接续。

【为什么用真实素材】
用**素材库里真实存在的**素材（一张 PNG + 一段 149.88s 的 flac），
不造假数据 —— 假素材跑出来的时长对齐没有意义。

【跑什么】
  1. r2v：段1（参考图 + 参考音乐）-> 点「接续」-> 段2（接上一段的潜空间）
  2. flf：段1（首帧）-> 点「接续」-> 段2
  3. 用 ffprobe 核对：段视频时长、完整视频时长、音轨来源与长度

【产出】
  evidence/cont-seg-live-<时间戳>/ 下的 JSON + ffprobe 输出

运行：python3 tests/live_cont_seg_e2e.py
"""

from __future__ import annotations

import json
import os
import hashlib
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_BASE", "http://127.0.0.1:8190")
REPO = Path("/root/projects/comfyui-minimax-h3-studio")
DATA = REPO / "data"
OUT_ROOT = Path("/root/projects/h3studio-live/evidence")


def api(path, method="GET", payload=None, timeout=60):
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def ffprobe(path):
    """ffprobe -> {视频时长, 音轨时长, 帧数, 宽高}。"""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,duration,nb_frames,width,height",
           "-of", "json", str(path)]
    raw = subprocess.check_output(cmd, encoding="utf-8", timeout=60)
    data = json.loads(raw)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    frames = video.get("nb_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    return {
        "path": str(path),
        "format_duration": float(fmt.get("duration") or 0.0),
        "video_duration": float(video.get("duration") or 0.0),
        "width": video.get("width"),
        "height": video.get("height"),
        "frames": frames,
        "has_audio": audio is not None,
        "audio_duration": (float(audio.get("duration") or 0.0)
                           if audio else None),
        "audio_codec": (audio or {}).get("codec_name"),
    }


def job_row(job_id):
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        conn.close()


def wait_done(job_id, timeout=1800):
    """等任务到终态。返回 (status, error, outputs)。"""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        row = job_row(job_id)
        if not row:
            time.sleep(2)
            continue
        status = row["status"]
        if status in ("done", "failed", "canceled"):
            outputs = json.loads(row["outputs"] or "[]")
            return status, row["error"], outputs
        last = f"{status} {row.get('progress_text') or ''}"
        time.sleep(4)
    return "timeout", last, []


def pick_assets():
    """从素材库里挑一张图片 + 一段音频（真实素材）。"""
    import sqlite3 as s3
    conn = s3.connect("/mnt/usb2t/asset_library/asset_library.db")
    conn.row_factory = s3.Row
    try:
        image = conn.execute(
            "SELECT id, name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        audio = conn.execute(
            "SELECT id, name FROM assets WHERE category='audio' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return (dict(image) if image else None), (dict(audio) if audio else None)


def submit(mode, params, asset_refs):
    """提交一个生成任务（multipart —— 与前端同格式）。返回 job_id。"""
    boundary = "----h3studioE2E" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"提交失败 HTTP {exc.code}: {detail}") from exc
    if not data.get("id"):
        raise RuntimeError(f"提交失败: {data}")
    return data["id"]


def run_chain(tag, mode, params1, refs1, params2, refs2):
    """跑一条接续链：段1 -> 点接续 -> 段2 -> 核对产物。"""
    result = {"tag": tag, "mode": mode}
    print(f"\n=== [{tag}] 段1 提交 ===", flush=True)
    job1 = submit(mode, params1, refs1)
    result["job1"] = job1
    print(f"[{tag}] 段1 job={job1}", flush=True)
    status, error, outputs = wait_done(job1)
    result["job1_status"] = status
    result["job1_error"] = error
    result["job1_outputs"] = outputs
    print(f"[{tag}] 段1 -> {status} {error or ''}", flush=True)
    if status != "done":
        return result

    # 潜空间落盘（接续链的起点）
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT params FROM jobs WHERE id=?", (job1,)).fetchone()
    conn.close()
    p1 = json.loads(row["params"] or "{}")
    cont1 = p1.get("continuation") or {}
    result["job1_continuation"] = cont1
    latent_dir = cont1.get("latent_run_id") or ""
    stage_path = (Path("/home/ubuntu/comfyui/output/relay_kit")
                  / latent_dir / "stage_00000.safetensors")
    result["job1_latent_path"] = str(stage_path)
    result["job1_latent_exists"] = stage_path.is_file()
    result["job1_latent_bytes"] = (stage_path.stat().st_size
                                   if stage_path.is_file() else 0)
    print(f"[{tag}] 段1 潜空间: {stage_path} "
          f"存在={result['job1_latent_exists']} "
          f"大小={result['job1_latent_bytes']}", flush=True)

    # 点「接续」（= 前端调 GET /api/jobs/{id}/continuation）
    ctx = api(f"/api/jobs/{job1}/continuation")
    result["continuation_ctx_ok"] = ctx.get("ok")
    result["continuation_ctx_error"] = ctx.get("error")
    result["continuation_ctx_audio"] = ctx.get("audio")
    result["continuation_ctx_asset_refs"] = ctx.get("asset_refs")
    print(f"[{tag}] 接续上下文 ok={ctx.get('ok')} "
          f"error={ctx.get('error') or ''}", flush=True)
    if not ctx.get("ok"):
        return result

    # 段2：带上接续上下文
    params2 = dict(params2)
    params2["continuation"] = ctx["continuation"]
    print(f"[{tag}] 段2 提交 ===", flush=True)
    job2 = submit(mode, params2, refs2)
    result["job2"] = job2
    print(f"[{tag}] 段2 job={job2}", flush=True)
    status2, error2, outputs2 = wait_done(job2)
    result["job2_status"] = status2
    result["job2_error"] = error2
    result["job2_outputs"] = outputs2
    print(f"[{tag}] 段2 -> {status2} {error2 or ''}", flush=True)

    # 段2 的接续上下文（音频时序）
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT params FROM jobs WHERE id=?", (job2,)).fetchone()
    conn.close()
    p2 = json.loads(row["params"] or "{}")
    result["job2_continuation"] = p2.get("continuation") or {}

    # 段2 潜空间落盘
    cont2 = p2.get("continuation") or {}
    stage2 = (Path("/home/ubuntu/comfyui/output/relay_kit")
              / (cont2.get("latent_run_id") or "") / "stage_00000.safetensors")
    result["job2_latent_path"] = str(stage2)
    result["job2_latent_exists"] = stage2.is_file()
    print(f"[{tag}] 段2 潜空间: {stage2} "
          f"存在={result['job2_latent_exists']}", flush=True)

    # ffprobe 核对
    probes = {}
    for name, outs in (("job1", outputs), ("job2", outputs2)):
        for item in outs:
            if item.get("kind") != "video":
                continue
            path = Path("/home/ubuntu/comfyui/output") / item["file"]
            if not path.is_file():
                path = Path(item.get("file") or "")
            key = f"{name}_{item.get('file')}"
            try:
                probes[key] = ffprobe(path)
                probes[key]["is_full"] = bool(item.get("is_full"))
            except Exception as exc:
                probes[key] = {"path": str(path), "error": str(exc)}
    result["probes"] = probes
    for key, info in probes.items():
        print(f"[{tag}] {key}: 时长={info.get('video_duration')}s "
              f"帧={info.get('frames')} 音轨={info.get('has_audio')} "
              f"音频长={info.get('audio_duration')} full={info.get('is_full')}",
              flush=True)
    return result


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"cont-seg-live-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"started_at": stamp, "base": BASE, "runs": []}

    image, audio = pick_assets()
    print(f"素材: image={image} audio={audio}", flush=True)
    if not image or not audio:
        print("素材库缺素材，无法跑真实测试", flush=True)
        return 1

    # ---- r2v：参考图 + 参考音乐 ----
    r2v_params1 = {
        "prompt": "一只猫在窗边打盹，暖光，镜头缓慢推进",
        "model_key": "ref2va_pruned_int8",
        "accel_lora": "ckpt850",
        "accel_lora_strength": 1.0,
        "steps": 4, "sampler": "euler", "scheduler": "beta",
        "width": 512, "height": 288, "duration": 2.0,
        "resolution": "288p", "aspect": "16:9",
        "shift_video": 12.0, "shift_audio": 3.0,
        "attention": "default", "twopass": {"enabled": False},
        "audio_track_source": "reference",
        "seed": 20261116,
    }
    r2v_refs = [
        {"asset_id": image["id"], "slot": "ref_image", "ordinal": 0},
        {"asset_id": audio["id"], "slot": "ref_audio", "ordinal": 0},
    ]
    r2v_params2 = dict(r2v_params1)
    r2v_params2["prompt"] = "猫睁开眼抬头看向窗外，镜头继续推进"
    r2v_params2["seed"] = 20261117

    report["runs"].append(run_chain(
        "r2v", "r2v", r2v_params1, r2v_refs, r2v_params2, r2v_refs))

    # ---- flf：首帧 ----
    flf_params1 = {
        "prompt": "人物缓缓转头，露出微笑",
        "model_key": "fl2va_pruned_int8",
        "accel_lora": "ckpt850",
        "accel_lora_strength": 1.0,
        "steps": 4, "sampler": "euler", "scheduler": "beta",
        "width": 512, "height": 288, "duration": 2.0,
        "resolution": "288p", "aspect": "16:9",
        "shift_video": 12.0, "shift_audio": 3.0,
        "attention": "default", "twopass": {"enabled": False},
        "audio_track_source": "generated",
        "seed": 20261118,
    }
    flf_refs = [{"asset_id": image["id"], "slot": "first_frame", "ordinal": 0}]
    flf_params2 = dict(flf_params1)
    flf_params2["prompt"] = "人物转向镜头，眼神坚定"
    flf_params2["seed"] = 20261119

    report["runs"].append(run_chain(
        "flf", "flf", flf_params1, flf_refs, flf_params2, flf_refs))

    report["finished_at"] = time.strftime("%Y%m%d-%H%M%S")
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out_dir / 'report.json'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
