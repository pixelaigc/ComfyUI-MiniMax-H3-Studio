#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上 E2E：flf / r2v 接续链的**默认路径**必须是标准采样（等价 nonebot seg2）。

【本脚本要证明什么】
2026-11-18 的取证结论：接续链在**段 2 内部 frame0→1** 出现硬切式色调跳变，
根因是二采（SelfLift）在接续段的表现。本轮把接续段的默认采样路径改成
**标准 SamplerCustomAdvanced**（等价 nonebot minimax_h3_seg2）。

本脚本在**真实 8190 线上**跑 flf 与 r2v 各一条接续链，并从
**ComfyUI 自己的提交记录**（GET http://127.0.0.1:8188/history）导出
实际下发的工作流 JSON —— 证明采样节点是标准 SamplerCustomAdvanced、
**没有** SelfLift 节点。

【为什么必须从 ComfyUI history 取工作流】
服务端只落 params，不落最终工作流；ComfyUI history 才是权威。

用法：
    python3 tests/live_cont_seg2_std.py --mode both
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_BASE", "http://127.0.0.1:8190")
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
REPO = Path("/root/projects/comfyui-minimax-h3-studio")
DATA = REPO / "data"
OUT_ROOT = Path("/root/projects/h3studio-cont-seg2/evidence/evidence-20261118-seg2-std")
ASSET_DB = "/mnt/usb2t/asset_library/asset_library.db"
ASSET_ROOT = Path("/mnt/usb2t/asset_library")
#: 上游 T8 的参考视频最小帧数（h3_t8/core.py:31）
MIN_REF_VIDEO_FRAMES = 48


def api(path: str, method: str = "GET", payload=None, timeout: int = 120):
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def ffprobe(path: str) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()[:300]}
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", [])
                  if s.get("codec_type") == "audio"), None)
    frames = (video or {}).get("nb_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    return {
        "path": str(path),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "frames": frames,
        "fps": (video or {}).get("r_frame_rate"),
        "video_duration": float((video or {}).get("duration") or 0.0),
        "format_duration": float((data.get("format") or {}).get("duration") or 0.0),
        "has_audio": audio is not None,
    }


def job_row(job_id: str):
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def wait_done(job_id: str, timeout: int = 3600):
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


def pick_assets():
    """挑素材：图片任取，视频必须 >= 48 帧（上游 T8 的硬要求）。"""
    conn = sqlite3.connect(ASSET_DB)
    conn.row_factory = sqlite3.Row
    try:
        img = conn.execute(
            "SELECT id,name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        rows = conn.execute(
            "SELECT a.id,a.name,v.storage_path FROM assets a "
            "JOIN asset_versions v ON v.asset_id=a.id "
            "WHERE a.category='video' AND a.deleted_at IS NULL "
            "ORDER BY a.created_at DESC").fetchall()
    finally:
        conn.close()
    vid = None
    for r in rows:
        rel = r["storage_path"] or ""
        full = rel if rel.startswith("/") else str(ASSET_ROOT / rel)
        if not Path(full).exists():
            continue
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-count_frames", "-show_entries", "stream=nb_read_frames",
                 "-of", "csv=p=0", full],
                capture_output=True, text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out.isdigit() and int(out) >= MIN_REF_VIDEO_FRAMES:
            vid = {"id": r["id"], "name": r["name"], "frames": int(out)}
            break
    return (dict(img) if img else None), vid


def submit(mode: str, params: dict, asset_refs: list):
    """提交任务（multipart —— 与前端同格式）。"""
    boundary = "----h3studioSeg2" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def add_field(name: str, value: str):
        parts.append(
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode() + b"\r\n")

    add_field("mode", mode)
    add_field("params", json.dumps(params, ensure_ascii=False))
    add_field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def comfy_workflow_for(prompt_id: str):
    """从 ComfyUI history 取实际下发的工作流 JSON（权威来源）。"""
    try:
        with urllib.request.urlopen(f"{COMFY}/history/{prompt_id}",
                                    timeout=120) as resp:
            hist = json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}
    entry = hist.get(prompt_id) or {}
    prompt = entry.get("prompt") or []
    # ComfyUI 的 history 里 prompt 是 [number, prompt_dict, extra, outputs]
    for item in prompt:
        if isinstance(item, dict) and item:
            return item
    return {"error": "history 里没找到 prompt 字典", "keys": list(entry.keys())}


def analyze_workflow(wf: dict) -> dict:
    """从工作流里判定采样节点。"""
    cts = {}
    for nid, node in wf.items():
        if isinstance(node, dict) and node.get("class_type"):
            cts[nid] = node["class_type"]
    samplers = {nid: ct for nid, ct in cts.items()
                if ct in ("SamplerCustomAdvanced", "SelfLiftH3Sampler",
                         "KSampler", "KSamplerAdvanced")}
    return {
        "class_types": cts,
        "sampler_nodes": samplers,
        "has_standard_sampler": "SamplerCustomAdvanced" in cts.values(),
        "has_selflift": any("SelfLift" in (ct or "") for ct in cts.values()),
        "selflift_nodes": [nid for nid, ct in cts.items()
                           if "SelfLift" in (ct or "")],
    }


def run_chain(tag: str, mode: str, params1: dict, refs1: list,
              params2: dict, refs2: list, out_dir: Path) -> dict:
    result = {"tag": tag, "mode": mode}
    print(f"\n=== [{tag}] 段1 提交 ===", flush=True)
    resp1 = submit(mode, params1, refs1)
    job1 = resp1.get("id") or resp1.get("job_id")
    result["job1"] = job1
    print(f"[{tag}] 段1 job={job1}", flush=True)
    status, error, outputs = wait_done(job1)
    result["job1_status"] = status
    result["job1_error"] = error
    result["job1_outputs"] = outputs
    print(f"[{tag}] 段1 -> {status} {error or ''}", flush=True)
    if status != "done":
        return result

    row1 = job_row(job1)
    p1 = json.loads(row1["params"] or "{}")
    result["job1_prompt_id"] = row1.get("prompt_id")
    result["job1_twopass_in_db"] = p1.get("twopass")
    result["job1_normalize_notes"] = p1.get("normalize_notes")

    # 接续上下文
    ctx = api(f"/api/jobs/{job1}/continuation")
    result["continuation_ctx_ok"] = ctx.get("ok")
    result["continuation_ctx_error"] = ctx.get("error")
    if not ctx.get("ok"):
        return result

    params2 = dict(params2)
    params2["continuation"] = ctx["continuation"]
    print(f"[{tag}] 段2 提交 ===", flush=True)
    resp2 = submit(mode, params2, refs2)
    job2 = resp2.get("id") or resp2.get("job_id")
    result["job2"] = job2
    print(f"[{tag}] 段2 job={job2}", flush=True)
    status2, error2, outputs2 = wait_done(job2)
    result["job2_status"] = status2
    result["job2_error"] = error2
    result["job2_outputs"] = outputs2
    print(f"[{tag}] 段2 -> {status2} {error2 or ''}", flush=True)

    row2 = job_row(job2)
    p2 = json.loads(row2["params"] or "{}")
    result["job2_prompt_id"] = row2.get("prompt_id")
    result["job2_twopass_in_db"] = p2.get("twopass")
    result["job2_normalize_notes"] = p2.get("normalize_notes")
    result["job2_continuation"] = p2.get("continuation") or {}

    # ---- 权威取证：ComfyUI history 里的实际工作流 ----
    for label, pid in (("job1", result.get("job1_prompt_id")),
                       ("job2", result.get("job2_prompt_id"))):
        if not pid:
            continue
        wf = comfy_workflow_for(pid)
        if isinstance(wf, dict) and "error" in wf and len(wf) <= 3:
            result[f"{label}_comfy_workflow_error"] = wf["error"]
            continue
        path = out_dir / f"workflow_{label}_{pid}.json"
        path.write_text(json.dumps(wf, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        analysis = analyze_workflow(wf)
        result[f"{label}_workflow_path"] = str(path)
        result[f"{label}_workflow_analysis"] = analysis
        print(f"[{tag}] {label} ComfyUI 工作流 -> {path}", flush=True)
        print(f"[{tag}] {label} 标准采样={analysis['has_standard_sampler']} "
              f"SelfLift={analysis['has_selflift']} "
              f"采样节点={analysis['sampler_nodes']}", flush=True)

    # 产物探测
    probes = {}
    for label, outs in (("job1", outputs), ("job2", outputs2)):
        for item in outs:
            if item.get("kind") != "video":
                continue
            rel = item.get("file") or ""
            path = Path("/home/ubuntu/comfyui/output") / rel
            if not path.is_file():
                path = Path(rel)
            key = f"{label}_{Path(rel).name}"
            try:
                probes[key] = ffprobe(path)
                probes[key]["is_full"] = bool(item.get("is_full"))
                probes[key]["rel"] = rel
            except Exception as exc:
                probes[key] = {"path": str(path), "error": str(exc)}
            print(f"[{tag}] {key}: {probes[key].get('width')}x"
                  f"{probes[key].get('height')} 帧={probes[key].get('frames')} "
                  f"时长={probes[key].get('video_duration')}s "
                  f"full={probes[key].get('is_full')}", flush=True)
    result["probes"] = probes
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="both",
                    choices=["flf", "r2v", "both"])
    ap.add_argument("--duration", type=float, default=5.0)
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"live-seg2-std-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    img, vid = pick_assets()
    print(f"素材: img={img} vid={vid}")
    if not img:
        print("!! 素材库里没有图片素材")
        return 2

    base_params = {
        "prompt": "一位少女站在日式教室讲台前，镜头缓慢推近，薄荷绿短发，粉色背心，细腻光影",
        "width": 1280, "height": 736,
        "duration": args.duration,
        "seed": 20261118,
        "aspect": "16:9",
        "steps": 4, "sampler": "euler", "scheduler": "beta",
        "cfg": 1.0,
        # 【关键】入参**显式开二采** —— 用来证明「即使入参开了，
        # 接续段也必须走标准采样」（这是本轮的核心不变量）。
        "twopass": {"enabled": True, "transition_step": 4,
                     "lowres_scale": 0.7, "rho": 0.0,
                     "w_min": 0.5, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_bf16.safetensors",
                     "cfg": 1.0},
    }

    results = []
    modes = ["flf", "r2v"] if args.mode == "both" else [args.mode]
    for mode in modes:
        if mode == "flf":
            refs = [{"asset_id": img["id"], "slot": "first_frame", "ordinal": 0},
                    {"asset_id": img["id"], "slot": "last_frame", "ordinal": 1}]
        else:
            if not vid:
                print("跳过 r2v（没有合格的视频素材）")
                continue
            refs = [{"asset_id": img["id"], "slot": "ref_image", "ordinal": 0},
                    {"asset_id": vid["id"], "slot": "ref_video", "ordinal": 0}]
        p = dict(base_params)
        if mode == "r2v":
            p["model_key"] = "hybrid_fl2va_ref2va"
        results.append(run_chain(mode, mode, p, refs, p, refs, out_dir))

    summary = {"target": "接续段默认走标准采样（等价 nonebot seg2）",
               "results": results}
    (out_dir / "live_seg2_std.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总 =====")
    for r in results:
        a1 = r.get("job1_workflow_analysis") or {}
        a2 = r.get("job2_workflow_analysis") or {}
        print(f"  {r['mode']}: 段1 标准采样={a1.get('has_standard_sampler')} "
              f"SelfLift={a1.get('has_selflift')} | "
              f"段2 标准采样={a2.get('has_standard_sampler')} "
              f"SelfLift={a2.get('has_selflift')}")
        print(f"       段2 落库 twopass = {r.get('job2_twopass_in_db')}")
        print(f"       段2 归一留痕 = {r.get('job2_normalize_notes')}")
    print(f"\n  证据目录: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
