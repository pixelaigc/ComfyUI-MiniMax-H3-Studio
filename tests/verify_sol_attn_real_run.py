#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实跑一次 sol-attn 近似路由的文生视频（第 2 项的实证）。

【为什么需要这个脚本】
用户怀疑「sol-attn 近似路由」不可用。单看代码/清单不算数 ——
必须**真的**把一条文生视频任务跑完，并从 ComfyUI 的 history 里
逐字核对：
  * MiniMaxH3MemoryEfficientSolAttentionPatch 节点真的进了 prompt；
  * tau / min_tokens / sink_conditioning 等参数**逐项下发**；
  * 任务 status = done（kernel 没炸、没回落到稠密路径报错）。

同时记录耗时与显存表现（用户明确要求「允许在本任务内跑一次真实
生成并记录耗时/显存表现」）。

用法：
  python3 tests/verify_sol_attn_real_run.py --base http://127.0.0.1:8199
输出：JSON 打印到 stdout（同时落盘到 --out 指定的文件）
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _get(url: str, timeout: int = 30):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_multipart(url: str, fields: dict, timeout: int = 120):
    """POST 一个 multipart/form-data（不用 requests，只用标准库）。"""
    boundary = "----h3studioverify%d" % int(time.time())
    body = bytearray()
    for name, value in fields.items():
        body += ("--%s\r\n" % boundary).encode()
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % name).encode()
        body += ("%s\r\n" % value).encode()
    body += ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _job_prompt_id(base: str, job_id: str) -> str:
    """取任务在 Studio 库里的 prompt_id（jobs 表的列）。

    【为什么绕这一下】/api/jobs 不下发 prompt_id，
    但 ComfyUI history 的 key 就是它 —— 必须精确对上，
    否则「在 history 里随便找一条含该节点的」会拿错任务的 prompt。
    """
    import sqlite3
    db = Path(base.replace("http://", "").replace("https://", "").split(":")[0])
    # 直接问 Studio 的 API 拿不到 -> 读它用的库（由 H3STUDIO_DATA_DIR 指定）
    data_dir = os.environ.get("H3STUDIO_DATA_DIR")
    if not data_dir:
        return ""
    db_path = Path(data_dir) / "jobs.db"
    if not db_path.is_file():
        return ""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT prompt_id FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        return (row["prompt_id"] or "") if row else ""
    finally:
        conn.close()


def _gpu_used_mib() -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
             "-i", "0"],
            capture_output=True, text=True, timeout=10).stdout
        return float(out.strip().splitlines()[0])
    except Exception:
        return -1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8199",
                    help="Studio 实例（独立实例，不要打线上 8190）")
    ap.add_argument("--comfy", default="http://127.0.0.1:8188")
    ap.add_argument("--out", default="")
    ap.add_argument("--width", type=int, default=864)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--duration", type=float, default=5)
    ap.add_argument("--model", default="fl2va_pruned_int8")
    ap.add_argument("--attention", default="sol")
    args = ap.parse_args()

    result = {
        "base": args.base,
        "attention": args.attention,
        "width": args.width,
        "height": args.height,
        "duration": args.duration,
    }

    # ---- 0. 前置：本机 ComfyUI 真的有那个节点吗 ----
    node = "MiniMaxH3MemoryEfficientSolAttentionPatch"
    try:
        info = _get("%s/object_info/%s" % (args.comfy, node))
        result["node_present"] = node in info
        result["node_slots"] = sorted(
            (info.get(node, {}).get("input", {}).get("required") or {}).keys())
    except Exception as exc:
        result["node_present"] = False
        result["node_error"] = str(exc)

    # ---- 1. 提交一条真实任务 ----
    params = {
        "prompt": "A red panda walking slowly through a bamboo forest, "
                  "morning sunlight, cinematic, static camera.",
        "model_key": args.model,
        "width": args.width,
        "height": args.height,
        "aspect": "16:9",
        "duration": args.duration,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "shift_video": 12.0,
        "shift_audio": 3.0,
        "seed": 20261112,
        "fixed_accel_lora": "ckpt850",
        "accel_loras": [],
        "attention": args.attention,
        "twopass": {"enabled": False},
    }
    vram_before = _gpu_used_mib()
    result["vram_used_mib_before"] = vram_before

    t0 = time.time()
    try:
        created = _post_multipart(
            "%s/api/jobs" % args.base,
            {"mode": "t2v", "params": json.dumps(params, ensure_ascii=False)})
    except Exception as exc:
        result["submit_error"] = str(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    job_id = created.get("id")
    result["job_id"] = job_id
    result["submit_error"] = created.get("error")

    # ---- 2. 轮询到终态 ----
    vram_peak = vram_before
    status = ""
    error = ""
    while True:
        time.sleep(3)
        try:
            data = _get("%s/api/jobs?limit=1" % args.base)
            jobs = data.get("jobs") or []
            row = next((j for j in jobs if j.get("id") == job_id), None)
        except Exception:
            row = None
        vram_peak = max(vram_peak, _gpu_used_mib())
        if row is None:
            continue
        status = row.get("status", "")
        error = row.get("error") or ""
        if status in ("done", "failed", "canceled"):
            break
        if time.time() - t0 > 1800:
            status = "timeout"
            break

    elapsed = time.time() - t0
    result["status"] = status
    result["error"] = error
    result["elapsed_sec"] = round(elapsed, 1)
    result["vram_peak_mib"] = vram_peak

    # ---- 3. 从 ComfyUI history 逐字核对下发的 prompt ----
    #
    # 【关键】必须按**这条任务自己的** prompt_id 去查 ——
    # 不能「在 history 里找最近一条含该节点的」（那会拿到上一条任务的
    # prompt，看起来参数也对，但证明不了本次下发正确）。
    try:
        prompt_id = _job_prompt_id(args.base, job_id)
        result["prompt_id"] = prompt_id
        if prompt_id:
            hist = _get("%s/history/%s" % (args.comfy, prompt_id))
            entry = hist.get(prompt_id) or {}
            prompt = entry.get("prompt")
            nodes = prompt[2] if isinstance(prompt, list) and len(prompt) > 2 else None
            if isinstance(nodes, dict):
                found = next((n for n in nodes.values()
                              if n.get("class_type") == node), None)
                result["node_found_in_prompt"] = found is not None
                if found is not None:
                    result["node_inputs"] = found.get("inputs")
            status = entry.get("status") or {}
            result["comfy_status"] = status.get("status_str")
            result["comfy_messages"] = [
                (m or [{}])[0].get("x", "") for m in (status.get("messages") or [])
                if m and m[0] == "execution_error"]
    except Exception as exc:
        result["history_error"] = str(exc)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
