#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 1 的**真机**证据：一条真实的长时视频任务，跑超 30 分钟仍是「进行中」。

【为什么不能只改 DB 的 started_at】
「running_truth」会**实时**查 ComfyUI 的 /queue 与 /history：
如果任务根本不在 ComfyUI 队列里，判定结果是「已丢失」（lost）——
那是正确行为，不是缺陷 1 的证据。
所以证据必须来自一条**真的在 ComfyUI 队列里**的任务。

【做法】
  1. 通过线上服务 POST /api/jobs 提交一条真实的文生视频任务；
  2. 等它出现在 ComfyUI 的 /queue 里（拿到 prompt_id）；
  3. 把它的 started_at 改到 **2 小时前**（= 模拟「已经跑了很久」）；
  4. 读 /api/jobs/{id} 与 /api/queue：
       修复前 -> running_truth.state = 'timeout'（前端显示「已超时」）
       修复后 -> running_truth.state = 'active'（= 进行中）
  5. 收尾：等任务跑完（或取消），把 started_at 还原。

用法：python3 scripts/evidence_video_long_running.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
import uuid

BASE = "http://127.0.0.1:8190"
COMFY = "http://127.0.0.1:8188"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
OUT = "/root/projects/h3s-merge-20261119/evidence/gen-status-20261119"

PROMPT = ("A slow cinematic aerial shot over a quiet coastal town at dawn, "
          "misty harbour, gulls, warm light, gentle camera drift")


def get_json(url, timeout=120):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def submit_video():
    """POST /api/jobs（multipart）提交一条真实的文生视频任务。"""
    boundary = uuid.uuid4().hex
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", "t2v")
    field("params", json.dumps({
        "prompt": PROMPT,
        "duration": 5,
        "width": 1344,
        "height": 768,
    }, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("═══ 提交真实视频任务 ═══")
    resp = submit_video()
    job_id = resp.get("id")
    print(f"  任务已提交: {job_id}  mode=t2v")
    if not job_id:
        print("  !! 提交失败:", resp)
        return 1

    # 等它进 ComfyUI 队列
    prompt_id = ""
    deadline = time.time() + 120
    while time.time() < deadline:
        row = None
        conn = sqlite3.connect(DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT status, prompt_id, started_at FROM jobs "
                           "WHERE id=?", (job_id,)).fetchone()
        conn.close()
        if row and row["prompt_id"]:
            prompt_id = row["prompt_id"]
            q = get_json(f"{COMFY}/queue")
            ids = []
            for key in ("queue_running", "queue_pending"):
                for item in (q.get(key) or []):
                    if isinstance(item, (list, tuple)) and len(item) > 1:
                        ids.append(item[1])
            if prompt_id in ids:
                print(f"  已在 ComfyUI 队列中: prompt_id={prompt_id}")
                break
        time.sleep(3)
    else:
        print("  !! 3 分钟内没进 ComfyUI 队列")
        return 1

    # 记录原始 started_at（收尾时还原）
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT started_at FROM jobs WHERE id=?",
                       (job_id,)).fetchone()
    original_started_at = row["started_at"] if row else None
    conn.close()

    # 把 started_at 改到 2 小时前 —— 模拟「已经跑了很久」
    two_hours_ago = time.time() - 7200
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE jobs SET started_at=? WHERE id=?",
                 (two_hours_ago, job_id))
    conn.commit()
    conn.close()
    print(f"  started_at 已改到 2 小时前（{two_hours_ago:.0f}）")

    print("\n═══ 修复后应当是「进行中」 ═══")
    job = get_json(f"{BASE}/api/jobs/{job_id}")
    truth = job.get("running_truth") or {}
    print(f"  /api/jobs/{job_id}")
    print(f"    status        = {job.get('status')}")
    print(f"    running_truth = {json.dumps(truth, ensure_ascii=False)}")
    ok_a = truth.get("state") == "active"
    print(f"    [{'PASS' if ok_a else 'FAIL'}] running_truth.state == 'active' "
          f"（= 仍判为进行中，而不是「已超时」）")

    snap = get_json(f"{BASE}/api/queue")
    item = next((i for i in (snap.get("items") or [])
                 if i.get("id") == job_id), None)
    ok_q = bool(item) and (item.get("running_truth") or {}).get("state") == "active"
    if item:
        print(f"  /api/queue 里的同一条: "
              f"{json.dumps(item.get('running_truth'), ensure_ascii=False)}")
    print(f"    [{'PASS' if ok_q else 'FAIL'}] 队列面板里也是 active")

    # 还原 started_at
    conn = sqlite3.connect(DB)
    conn.execute("UPDATE jobs SET started_at=? WHERE id=?",
                 (original_started_at, job_id))
    conn.commit()
    conn.close()
    print(f"\n  started_at 已还原为 {original_started_at}")

    out = {
        "job_id": job_id,
        "prompt_id": prompt_id,
        "simulated_elapsed_sec": 7200,
        "running_truth": truth,
        "job_status": job.get("status"),
        "queue_running_truth": (item or {}).get("running_truth"),
        "verdict_state_is_active": ok_a,
        "verdict_queue_is_active": ok_q,
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    import os
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/evidence_video_long_running.json", "w",
              encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"  证据已存: {OUT}/evidence_video_long_running.json")

    # 取消任务，避免占用 GPU
    try:
        req = urllib.request.Request(
            f"{BASE}/api/jobs/{job_id}/cancel", method="POST")
        urllib.request.urlopen(req, timeout=15)
        print("  已取消该证据任务（避免占用 GPU）")
    except Exception as exc:
        print(f"  取消失败（不影响结论）: {exc}")

    return 0 if (ok_a and ok_q) else 1


if __name__ == "__main__":
    sys.exit(main())
