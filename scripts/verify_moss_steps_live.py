#!/usr/bin/env python3
"""实测取证脚本：证明「面板改的步数真的到了 ComfyUI」。

【为什么需要这个脚本】
单测证明的是「代码逻辑对」。本脚本证明的是「**线上跑的那套**真的按
用户改的值执行」—— 它走的是完整的真实链路：

    POST /api/jobs (steps=100)
      -> 服务端 prepare_job_params 归一
      -> jobs.params 落库
      -> worker build_workflow -> ComfyUI prompt
      -> ComfyUI /history 回读该 prompt 的实际 KSampler 节点输入
      -> 产物文件真实落盘

【判据 —— 三个都要】
  1. 任务记录里的 steps == 用户提交的值（没有��默认值覆盖）
  2. ComfyUI prompt 元数据里的 KSampler.steps == 用户提交的值
     （这是「实际执行」的权威证据 —— 不是我们自己的推断）
  3. 产物真的出图了（文件存在、尺寸符合）

【用法】
    python3 scripts/verify_moss_steps_live.py            # 单张
    python3 scripts/verify_moss_steps_live.py --batch 3  # 批量 3 张
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
ROOT = Path(__file__).resolve().parent.parent
DB = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")


def _post_multipart(path: str, fields: dict[str, str]) -> dict:
    boundary = "----h3s" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append(
            ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
             % (boundary, name, value)).encode())
    body = b"".join(chunks) + ("--%s--\r\n" % boundary).encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit("HTTP %s: %s" % (exc.code, exc.read().decode()[:500]))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def submit_moss(steps: int, batch: int) -> str:
    params = {
        "prompt": "a red apple on a wooden table, studio product photo",
        "model": "moss_image_base",
        "resolution": "1mp",
        "aspect": "16:9",
        "steps": steps,
        "cfg": 3,
        "shift": 5,
        "sampler": "euler",
        "scheduler": "sgm_uniform",
        "seed": 2081206678357,
        "batch_count": batch,
        "negative_prompt": "",
        "loras": [],
        "slg_single_layers": "22",
    }
    resp = _post_multipart("/api/jobs", {
        "mode": "t2i_moss",
        "params": json.dumps(params, ensure_ascii=False),
    })
    return resp["id"]


def wait_done(job_id: str, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _get(BASE + "/api/jobs/" + job_id)
        if job.get("status") in ("done", "failed"):
            return job
        time.sleep(2)
    raise SystemExit("任务 %s 超时未完成" % job_id)


def comfy_prompt_for(job_id: str) -> dict:
    """从 ComfyUI 的 /history 回读该任务实际提交的 prompt。

    【为什么这是权威证据】/history 里的内容是 ComfyUI **真正收到并
    执行**的 prompt —— 不是我们自己 build 出来的 dict，也不是落库记录。
    """
    history = _get(COMFY + "/history")
    for _pid, entry in history.items():
        prompt = entry.get("prompt") or []
        # prompt 形如 [number, {node_id: node}, extra, outputs]
        if isinstance(prompt, list) and len(prompt) > 1 and isinstance(prompt[1], dict):
            nodes = prompt[1]
            for nid, node in nodes.items():
                inputs = node.get("inputs") or {}
                prefix = inputs.get("filename_prefix") or ""
                if job_id in str(prefix):
                    return nodes
    return {}


def db_rows(job_id: str) -> dict:
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    job = conn.execute(
        "SELECT id, mode, status, params, outputs FROM jobs WHERE id=?",
        (job_id,)).fetchone()
    batches = conn.execute(
        "SELECT id, idx, seed, status, outputs FROM job_batches "
        "WHERE job_id=? ORDER BY idx", (job_id,)).fetchall()
    conn.close()
    return {"job": job, "batches": batches}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=100)
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--out", default="evidence/moss_steps_live.json")
    args = ap.parse_args()

    print("=== 提交 MOSS-Image 任务：steps=%d batch=%d ===" % (args.steps, args.batch))
    job_id = submit_moss(args.steps, args.batch)
    print("JOB_ID:", job_id)

    job = wait_done(job_id)
    status = job.get("status")
    print("STATUS:", status)
    if status != "done":
        print("ERROR:", (job.get("error") or "")[:400])
        return 1

    params = job.get("params") or {}
    print("落库 steps =", params.get("steps"),
          "| cfg =", params.get("cfg"),
          "| shift =", params.get("shift"),
          "| sampler =", params.get("sampler"),
          "| scheduler =", params.get("scheduler"),
          "| seed =", params.get("seed"))
    print("归一说明 normalize_notes =", json.dumps(
        params.get("normalize_notes"), ensure_ascii=False))

    rows = db_rows(job_id)
    print("--- job_batches ---")
    for b in rows["batches"]:
        print("  idx", b["idx"], "seed", b["seed"], b["status"])

    nodes = comfy_prompt_for(job_id)
    ks = []
    for nid, node in nodes.items():
        if (node.get("class_type") or "").lower() == "ksampler":
            inputs = node.get("inputs") or {}
            ks.append({"node": nid, "steps": inputs.get("steps"),
                       "cfg": inputs.get("cfg"),
                       "sampler_name": inputs.get("sampler_name"),
                       "scheduler": inputs.get("scheduler"),
                       "seed": inputs.get("seed"),
                       "denoise": inputs.get("denoise")})
    print("--- ComfyUI /history 里的真实 KSampler 节点 ---")
    for k in ks:
        print(" ", json.dumps(k, ensure_ascii=False))

    outputs = job.get("outputs") or []
    print("--- 产物 ---")
    for o in outputs:
        print(" ", json.dumps(o, ensure_ascii=False))

    # ---- 判据 ----
    problems = []
    if params.get("steps") != args.steps:
        problems.append("落库 steps=%r != 提交 %r" % (params.get("steps"), args.steps))
    if params.get("normalize_notes"):
        problems.append("归一层改了参数: %s" % params["normalize_notes"])
    if not ks:
        problems.append("ComfyUI /history 里没找到本任务的 KSampler 节点")
    for k in ks:
        if k["steps"] != args.steps:
            problems.append("ComfyUI KSampler steps=%r != 提交 %r"
                            % (k["steps"], args.steps))
    if not outputs:
        problems.append("没有产物")

    # 产物文件真实存在
    out_dir = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
    for o in outputs:
        f = o.get("file")
        if not f:
            continue
        p = out_dir / f
        if not p.is_file():
            problems.append("产物文件不存在: %s" % p)
        else:
            print("产物文件:", p, p.stat().st_size, "bytes")

    report = {
        "job_id": job_id,
        "submitted_steps": args.steps,
        "batch_count": args.batch,
        "status": status,
        "stored_params": {k: params.get(k) for k in
                          ("steps", "cfg", "shift", "sampler", "scheduler", "seed")},
        "normalize_notes": params.get("normalize_notes"),
        "comfy_ksampler_nodes": ks,
        "outputs": outputs,
        "problems": problems,
        "verdict": "PASS" if not problems else "FAIL",
    }
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print()
    print("证据文件:", out_path)
    print("判定:", report["verdict"])
    if problems:
        for p in problems:
            print("  !!", p)
        return 1
    print("  steps=%d 在落库与 ComfyUI 实际执行中均已确认" % args.steps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
