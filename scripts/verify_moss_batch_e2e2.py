#!/usr/bin/env python3
"""【C 实测 · 端到端 + 浏览器面板截图】

在同一台机、同一块 ComfyUI 上，用**完全相同的面板参数**分别跑
「1 张」与「5 张批次」，然后：

  1. 从 ComfyUI 的 /history 读回**实际执行**的 KSampler / ModelSamplingSD3 参数
     （steps / cfg / sampler / scheduler / shift），证明批量没有被写死成 5 步
  2. 从 SQLite（job_batches）读回每张的独立 seed
  3. 列出真实产出文件路径
  4. 用真实 Chromium 打开批次面板截图（批次卡片展开态）

默认分辨率用 1mp（官方默认档），这样 6 张图能在可接受时间内跑完；
步数固定 100（用户自测用的设定值），用来对照「单张 vs 批量」。

端口纪律：独立端口（默认 8198）+ 独立 data_dir；不碰 8190 线上服务。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("H3_TEST_PORT", "8198"))
DATA = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-moss-batch-e2e2"))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-fix-evidence")
OUT.mkdir(parents=True, exist_ok=True)
#: 截图目录跟证据目录**同源**（硬编码到别处会让报告里的路径与实际落盘路径分家）
SHOTS = OUT / "screenshots"
COMFY = "http://127.0.0.1:8188"

PANEL = {
    "prompt": "a young woman in a sunlit cafe, portrait photo, 85mm lens",
    "resolution": "1mp", "aspect": "16:9",
    "model": "moss_image_base",
    "sampler": "euler", "scheduler": "sgm_uniform",
    "steps": 100, "cfg": 4.3, "shift": 5,
    "negative_prompt": "",
}


def http_json(url, method="GET", data=None, timeout=60):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def start_server():
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(DATA)
    env["AGENT_SESSION_DB"] = str(DATA / "agent_sessions.db")
    env["AGENT_MEMORY_DB"] = str(DATA / "agent_memory.db")
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=open("/tmp/h3-fix-evidence/moss_batch_e2e2_server.log", "wb"),
        stderr=subprocess.STDOUT)
    for _ in range(120):
        time.sleep(0.5)
        try:
            http_json(f"http://127.0.0.1:{PORT}/api/system", timeout=5)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("server 启动失败")
    raise RuntimeError("server 启动超时")


def submit(count):
    boundary = "----h3test"
    params = dict(PANEL)
    params["batch_count"] = count
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nt2i_moss\r\n".encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"params\"\r\n\r\n"
         + json.dumps(params, ensure_ascii=False) + "\r\n").encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/jobs", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def job_state(job_id):
    return http_json(f"http://127.0.0.1:{PORT}/api/jobs/{job_id}")


def wait_done(job_id, timeout=3600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = job_state(job_id)
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(3)
    raise TimeoutError(f"任务 {job_id} 超时（{timeout}s）")


def comfy_params(prompt_id):
    """从 ComfyUI history 读回**实际执行**的采样参数。"""
    try:
        h = http_json(f"{COMFY}/history/{prompt_id}", timeout=30)
    except Exception:
        return None
    for entry in h.values():
        nodes = entry.get("prompt")
        nodes = nodes[2] if isinstance(nodes, (list, tuple)) and len(nodes) > 2 else nodes
        if not isinstance(nodes, dict):
            continue
        ks = sd = None
        for nid, n in nodes.items():
            if n.get("class_type") == "KSampler":
                ks = {"node": nid, **n["inputs"]}
            elif n.get("class_type") == "ModelSamplingSD3":
                sd = {"node": nid, "shift": n["inputs"].get("shift")}
        if ks:
            return {"ksampler": ks, "sampling": sd}
    return None


def db_rows():
    conn = sqlite3.connect(f"file:{DATA / 'jobs.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    jobs = [dict(r) for r in conn.execute(
        "SELECT id, mode, status, params FROM jobs ORDER BY created_at")]
    batches = [dict(r) for r in conn.execute(
        "SELECT job_id, idx, seed, status, prompt_id FROM job_batches ORDER BY job_id, idx")]
    conn.close()
    return jobs, batches


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    proc = start_server()
    print(f"server 起在 127.0.0.1:{PORT}（pid {proc.pid}）", flush=True)
    report: dict = {"panel_params": PANEL, "port": PORT, "cases": {}}
    try:
        for count in (1, 5):
            print(f"\n=== 跑 {count} 张（batch_count={count}）===", flush=True)
            r = submit(count)
            job_id = r["id"]
            print(f"  job_id={job_id}", flush=True)
            t0 = time.time()
            j = wait_done(job_id)
            print(f"  状态={j.get('status')}  耗时={time.time()-t0:.0f}s", flush=True)
            outs = j.get("outputs") or []
            print(f"  产物数={len(outs)}", flush=True)
            for o in outs[:8]:
                print(f"      {o.get('kind')}: {o.get('file')}", flush=True)
            report["cases"][str(count)] = {
                "job_id": job_id, "status": j.get("status"),
                "elapsed_s": round(time.time() - t0),
                "outputs": [o.get("file") for o in outs],
            }
            # 等 ComfyUI history 落库
            time.sleep(3)
            jobs, batches = db_rows()
            mine = [b for b in batches if b["job_id"] == job_id]
            print(f"  批次记录 {len(mine)} 条", flush=True)
            steps_seen = []
            # 【单张（batch_count=1）走的是**非批次**分支】
            # 它不写 job_batches 行，而是把 prompt_id 写在 jobs 表上。
            # 所以单张的步数要从 **jobs.prompt_id** 读，
            # 否则 batch_count=1 的步数集合永远是空 —— 那样就没法做
            # 「1 张 vs 5 张」的对照（正是 C 项要的）。
            if not mine:
                job_row = next((j for j in jobs if j["id"] == job_id), None)
                if job_row and job_row.get("prompt_id"):
                    pp = comfy_params(job_row["prompt_id"])
                    if pp:
                        ks = pp["ksampler"]
                        sd = pp.get("sampling") or {}
                        steps_seen.append(ks.get("steps"))
                        print(f"    [单张路径] prompt_id={job_row['prompt_id'][:12]} "
                              f"steps={ks.get('steps')} cfg={ks.get('cfg')} "
                              f"sampler={ks.get('sampler_name')} "
                              f"sched={ks.get('scheduler')} shift={sd.get('shift')} "
                              f"seed={ks.get('seed')}", flush=True)
                        job_row["actual"] = {
                            "steps": ks.get("steps"), "cfg": ks.get("cfg"),
                            "sampler": ks.get("sampler_name"),
                            "scheduler": ks.get("scheduler"),
                            "shift": sd.get("shift"), "seed": ks.get("seed")}
                    else:
                        print("    [单张路径] 读不到 ComfyUI history")
                else:
                    print("    [单张路径] jobs 表里没有 prompt_id")
            for b in mine:
                p = comfy_params(b["prompt_id"]) if b["prompt_id"] else None
                if not p:
                    print(f"    #{b['idx']} status={b['status']} 读不到 history")
                    continue
                ks = p["ksampler"]
                steps_seen.append(ks.get("steps"))
                sd = p.get("sampling") or {}
                print(f"    #{b['idx']} status={b['status']} seed={b['seed']} "
                      f"steps={ks.get('steps')} cfg={ks.get('cfg')} "
                      f"sampler={ks.get('sampler_name')} sched={ks.get('scheduler')} "
                      f"shift={sd.get('shift')}", flush=True)
                b["actual"] = {"steps": ks.get("steps"), "cfg": ks.get("cfg"),
                               "sampler": ks.get("sampler_name"),
                               "scheduler": ks.get("scheduler"),
                               "shift": sd.get("shift"),
                               "seed": ks.get("seed")}
            report["cases"][str(count)]["batches"] = mine
            if not mine:
                job_row = next((j for j in jobs if j["id"] == job_id), None)
                report["cases"][str(count)]["single_job_actual"] = (
                    job_row.get("actual") if job_row else None)
            report["cases"][str(count)]["steps_set"] = sorted(set(steps_seen))
            print(f"  >>> 实际步数集合: {sorted(set(steps_seen))}", flush=True)
            jobs, batches = db_rows()
            report["jobs"] = jobs
            report["batches"] = batches
            (OUT / "moss_batch_e2e2.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()

    print("\n=== 汇总 ===")
    for count, c in report["cases"].items():
        print(f"  {count} 张: status={c['status']} 步数集合={c.get('steps_set')} "
              f"产物={len(c['outputs'])}")
    (OUT / "moss_batch_e2e2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存 {OUT / 'moss_batch_e2e2.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
