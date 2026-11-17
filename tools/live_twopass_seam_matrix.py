#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二采（twopass）接续链的**参数矩阵**线上实测（2026-11-17 接缝色档残缺陷）。

【为什么要有这个脚本】
上一轮的根因结论是「SelfLift 低分辨率段把钉住区降采样后当锚点」。
本轮要求**不沿用历史结论**，所以先用独立测量器重新取证，
发现归因方向**不同**：色档断点发生在**段2 内部**（帧0→1），
不在拼接点；且 ComfyUI 日志给出决定性机制：

    steps=4 transition_step=3 -> low_nfe=3 high_nfe=1 sigma_resume=0.7207
    steps=7 transition_step=5 -> low_nfe=5 high_nfe=2 sigma_resume=0.7699
    steps=4 transition_step=3 rho=0.6 -> 同一 NFE 分配

也就是说：**高分辨率二采只剩 1 步 NFE**（起始 sigma 高达 0.72），
这一步没有足够预算把低分辨率段定下的色档收敛回高分辨率空间。

本脚本跑一个**参数矩阵**，每条链都是「段1 → 点接续 → 段2」，
然后用 tools/seam_metric.py 统一测量，输出可直接对拍的 JSON。

【用法】
python3 tools/live_twopass_seam_matrix.py                 # 跑全矩阵
python3 tools/live_twopass_seam_matrix.py --only rho06    # 只跑一档
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
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_BASE", "http://127.0.0.1:8190")
REPO = Path("/root/projects/comfyui-minimax-h3-studio")
DATA = REPO / "data"
OUT_ROOT = Path("/root/projects/h3studio-live/evidence")
HERE = Path(__file__).resolve().parent

PROMPT = "一只猫在草地上快速奔跑，镜头跟拍，阳光明亮"


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


def submit(mode, params):
    boundary = "----h3matrix" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
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


def job_row(job_id):
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def wait_done(job_id, timeout=2400):
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


def base_params(twopass, steps, seed):
    return {
        "prompt": PROMPT,
        "model_key": "fl2va_pruned_int8",
        "accel_lora": "ckpt850",
        "accel_lora_strength": 1.0,
        "steps": steps, "sampler": "euler", "scheduler": "beta",
        "width": 1280, "height": 736, "duration": 5.0,
        "resolution": "736p", "aspect": "16:9",
        "shift_video": 12.0, "shift_audio": 3.0,
        "attention": "default",
        "twopass": twopass,
        "audio_track_source": "generated",
        "seed": seed,
    }


def run_chain(tag, twopass, steps, seed):
    """跑一条接续链：段1 -> 段2 -> 测量接缝。"""
    out = {"tag": tag, "twopass": twopass, "steps": steps, "seed": seed}
    print(f"\n=== [{tag}] steps={steps} twopass={twopass} ===", flush=True)

    p1 = base_params(twopass, steps, seed)
    job1 = submit("t2v", p1)
    out["job1"] = job1
    print(f"[{tag}] 段1 job={job1}", flush=True)
    st1, err1, outs1 = wait_done(job1)
    out["job1_status"] = st1
    out["job1_error"] = err1
    print(f"[{tag}] 段1 -> {st1} {err1 or ''}", flush=True)
    if st1 != "done":
        return out

    row = job_row(job1)
    cont1 = (json.loads(row["params"] or "{}") or {}).get("continuation") or {}
    latent_dir = cont1.get("latent_run_id") or ""
    stage = Path("/home/ubuntu/comfyui/output/relay_kit") / latent_dir / "stage_00000.safetensors"
    out["job1_latent_path"] = str(stage)
    out["job1_latent_exists"] = stage.is_file()
    print(f"[{tag}] 段1 接力棒: {stage} 存在={out['job1_latent_exists']}", flush=True)

    ctx = api(f"/api/jobs/{job1}/continuation")
    out["continuation_ok"] = ctx.get("ok")
    if not ctx.get("ok"):
        out["continuation_error"] = ctx.get("error")
        print(f"[{tag}] 接续上下文失败: {ctx.get('error')}", flush=True)
        return out
    cont_ctx = ctx.get("continuation")
    if not isinstance(cont_ctx, dict):
        out["continuation_error"] = "接续上下文里没有 continuation 段"
        print(f"[{tag}] 接续上下文里没有 continuation 段", flush=True)
        return out

    p2 = base_params(twopass, steps, seed + 1)
    p2["continuation"] = cont_ctx
    job2 = submit("t2v", p2)
    out["job2"] = job2
    print(f"[{tag}] 段2 job={job2}", flush=True)
    st2, err2, outs2 = wait_done(job2)
    out["job2_status"] = st2
    out["job2_error"] = err2
    print(f"[{tag}] 段2 -> {st2} {err2 or ''}", flush=True)
    if st2 != "done":
        return out

    files = {}
    for name, outs in (("seg1", outs1), ("seg2", outs2)):
        for o in outs:
            if o.get("kind") == "video" and not o.get("is_full"):
                files[name] = o["file"]
        for o in outs:
            if o.get("kind") == "video" and o.get("is_full"):
                files["full"] = o["file"]
    out["files"] = files
    if not all(k in files for k in ("full", "seg1", "seg2")):
        out["measure_error"] = f"文件不全: {files}"
        print(f"[{tag}] 文件不全: {files}", flush=True)
        return out

    jf = f"/tmp/matrix_{tag}.json"
    rc = subprocess.run(
        [sys.executable, str(HERE / "seam_metric.py"),
         str(DATA / "outputs" / files["full"]),
         str(DATA / "outputs" / files["seg1"]),
         str(DATA / "outputs" / files["seg2"]), "--json", jf],
        capture_output=True, text=True, timeout=900)
    if rc.returncode != 0:
        out["measure_error"] = rc.stderr[-400:]
        print(f"[{tag}] 测量失败: {rc.stderr[-300:]}", flush=True)
        return out
    m = json.loads(Path(jf).read_text())
    a = m["attribution"]
    out["metric"] = m
    print(f"[{tag}] 帧数 {m['frames']} 守恒={m['frames']['conserved']} "
          f"接续点帧={m['seam_frame_index']}", flush=True)
    print(f"[{tag}] 拼接点跳变={m['seam_rgb_jump']} 比值={m['seam_ratio_vs_all_median']}",
          flush=True)
    print(f"[{tag}] ★段2内部断点={a['seg2_head_break_l1']} "
          f"段2正常帧间={a['seg2_internal_scale_median']} "
          f"段内比值={a['seg2_head_break_ratio']}", flush=True)
    print(f"[{tag}] 高频能量 缝后/缝前={m['hf_energy_ratio_after_over_before']}", flush=True)
    return out


MATRIX = [
    # tag,          twopass,                                    steps, seed
    ("baseline",    {"enabled": True, "transition_step": 3, "lowres_scale": 0.75,
                     "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                     "cfg": 1.0}, 4, 20261201),
    ("rho06",       {"enabled": True, "transition_step": 3, "lowres_scale": 0.75,
                     "rho": 0.6, "w_min": 0.8, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                     "cfg": 1.0}, 4, 20261201),
    ("trans2",      {"enabled": True, "transition_step": 2, "lowres_scale": 0.75,
                     "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                     "cfg": 1.0}, 4, 20261201),
    ("steps7",      {"enabled": True, "transition_step": 5, "lowres_scale": 0.75,
                     "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
                     "cfg": 1.0}, 7, 20261201),
    ("twopass_off", {"enabled": False}, 4, 20261201),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"twopass-seam-matrix-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"started_at": stamp, "base": BASE, "runs": []}

    for tag, tp, steps, seed in MATRIX:
        if a.only and tag not in a.only:
            continue
        try:
            report["runs"].append(run_chain(tag, tp, steps, seed))
        except Exception as exc:
            report["runs"].append({"tag": tag, "error": str(exc)})
            print(f"[{tag}] 异常: {exc}", flush=True)
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n════════ 矩阵汇总 ════════")
    hdr = f"{'tag':14} {'拼接跳变':>9} {'拼接比值':>9} {'段2断点':>9} {'段内比值':>9} {'高频比':>8}"
    print(hdr)
    for r in report["runs"]:
        m = (r.get("metric") or {}).get("attribution")
        if not m:
            print(f"{r.get('tag'):14} {r.get('error') or r.get('measure_error') or 'N/A'}")
            continue
        mm = r["metric"]
        print(f"{r['tag']:14} {mm['seam_rgb_jump']:>9} {mm['seam_ratio_vs_all_median']:>9} "
              f"{m['seg2_head_break_l1']:>9} {m['seg2_head_break_ratio']:>9} "
              f"{mm['hf_energy_ratio_after_over_before']:>8}")

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
