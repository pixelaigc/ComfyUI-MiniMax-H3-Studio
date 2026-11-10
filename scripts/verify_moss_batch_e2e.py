"""【C 实测 · 端到端】真起 server（独立端口 + 独立 data_dir），
用**同一个面板参数**分别跑「1 张」与「5 张批次」，
然后从 ComfyUI 的 /history 读回**实际执行**的 KSampler 参数。

这是真实链路：HTTP -> server.py -> worker -> build_workflow -> ComfyUI。
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path("/root/projects/h3studio-agent-enh-merge")
PORT = int(os.environ.get("H3_TEST_PORT", "8197"))
DATA = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-moss-batch-e2e"))
DATA.mkdir(parents=True, exist_ok=True)

PANEL = {
    "prompt": "a young woman in a sunlit cafe, portrait photo, 85mm lens",
    "resolution": "2k", "aspect": "16:9",
    "width": 3136, "height": 1344,
    "model": "moss_image_base",
    "sampler": "euler", "scheduler": "sgm_uniform",
    "steps": 100, "cfg": 4.3, "shift": 5,
    "negative_prompt": "",
}

COMFY = "http://127.0.0.1:8188"


def http_json(url, method="GET", data=None, timeout=60):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def start_server():
    """起一个**独立实例**（独立端口 + 独立 data_dir）—— 不碰 8190 线上服务。

    ⚠️ server.py 的端口来自**仓库根目录的 config.json**（不是环境变量），
    所以不能直接改仓库里的 config.json（那是被 git 跟踪的）。
    做法：把整个 worktree **软链接**到临时目录，在临时目录里放自己的
    config.json —— server.py 用 `Path(__file__).resolve().parent` 定位
    仓库根，但 `resolve()` 会把软链接解析成**真实路径**……
    所以改用**复制一份最小可运行集**的方式不可行（太大）。

    正确做法：直接改 server.py 里的端口读取优先级 ——
    加一个环境变量 `H3STUDIO_PORT` / `H3STUDIO_DATA_DIR` 的覆盖，
    这本来就是独立测试实例该有的能力（agent 分支的 run_live_8199.sh
    已经在用 H3STUDIO_DATA_DIR）。
    """
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(DATA)
    env["AGENT_SESSION_DB"] = str(DATA / "agent_sessions.db")
    env["AGENT_MEMORY_DB"] = str(DATA / "agent_memory.db")
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(120):
        time.sleep(0.5)
        try:
            http_json(f"http://127.0.0.1:{PORT}/api/system", timeout=5)
            return proc
        except Exception:
            if proc.poll() is not None:
                print(proc.stdout.read()[-3000:])
                raise RuntimeError("server 启动失败")
    raise RuntimeError("server 启动超时")


def submit(count):
    """POST /api/jobs（multipart: mode + params JSON）。"""
    boundary = "----h3test"
    params = dict(PANEL)
    params["batch_count"] = count
    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"mode\"\r\n\r\nt2i_moss\r\n".encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"params\"\r\n\r\n"
                  + json.dumps(params, ensure_ascii=False) + "\r\n").encode())
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def job_state(job_id):
    return http_json(f"http://127.0.0.1:{PORT}/api/jobs/{job_id}")


def wait_done(job_id, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = job_state(job_id)
        if j.get("status") in ("done", "failed", "canceled"):
            return j
        time.sleep(2)
    raise TimeoutError(f"任务 {job_id} 超时")


def comfy_ksampler(prompt_id):
    """从 ComfyUI 的 history 读回**实际执行**的 KSampler 参数。"""
    try:
        h = http_json(f"{COMFY}/history/{prompt_id}", timeout=30)
    except Exception:
        return None
    for entry in h.values():
        prompt = entry.get("prompt")
        nodes = prompt[2] if isinstance(prompt, (list, tuple)) and len(prompt) > 2 else prompt
        if not isinstance(nodes, dict):
            continue
        for nid, node in nodes.items():
            if node.get("class_type") == "KSampler":
                return {"node": nid, **node["inputs"]}
    return None


def run_case(count):
    print(f"\n{'='*70}")
    print(f"=== 跑 {count} 张（batch_count={count}）===")
    print('='*70)
    r = submit(count)
    job_id = r["id"]
    print(f"  job_id={job_id}  服务端回 batch_count={r.get('batch_count')}")
    t0 = time.time()
    j = wait_done(job_id)
    print(f"  状态={j.get('status')}  耗时={time.time()-t0:.0f}s")
    print(f"  progress_text={j.get('progress_text')}")
    batches = j.get("batches") or []
    print(f"  批次数={len(batches)}")
    for b in batches:
        print(f"    #{b.get('idx')} status={b.get('status')} seed={b.get('seed')} "
              f"prompt_id={b.get('prompt_id')}")
    return j, batches


if __name__ == "__main__":
    proc = start_server()
    print(f"server 起在 127.0.0.1:{PORT}（pid {proc.pid}）")
    try:
        results = {}
        for count in (1, 5):
            j, batches = run_case(count)
            results[count] = (j, batches)
            # 读回 ComfyUI 实际执行的 KSampler
            print("\n  --- ComfyUI 实际执行的 KSampler 参数 ---")
            steps_seen = []
            for b in batches:
                pid = b.get("prompt_id")
                if not pid:
                    continue
                ks = comfy_ksampler(pid)
                if ks:
                    steps_seen.append(ks.get("steps"))
                    print(f"    批次#{b.get('idx')}: node={ks.get('node')} "
                          f"steps={ks.get('steps')} cfg={ks.get('cfg')} "
                          f"sampler={ks.get('sampler_name')} scheduler={ks.get('scheduler')} "
                          f"seed={ks.get('seed')}")
                else:
                    print(f"    批次#{b.get('idx')}: 读不到 history（prompt_id={pid}）")
            if steps_seen:
                print(f"  >>> 实际步数集合: {set(steps_seen)}  "
                      f"（期望 {{100}}）-> {'✅ 一致' if set(steps_seen)=={100} else '❌ 不一致'}")
            results[count] = (j, batches, steps_seen)

        # 汇总
        print(f"\n{'='*70}")
        print("=== 汇总 ===")
        print('='*70)
        for count, item in results.items():
            j, batches = item[0], item[1]
            steps_seen = item[2] if len(item) > 2 else []
            outs = j.get("outputs") or []
            print(f"  {count} 张: status={j.get('status')} 批次数={len(batches)} "
                  f"实际步数={set(steps_seen) if steps_seen else 'N/A'} "
                  f"产物数={len(outs)}")
            for o in outs[:6]:
                print(f"      {o.get('kind')}: {o.get('file')}")
        Path("/tmp/moss_batch_e2e_result.json").write_text(json.dumps({
            str(k): {"status": v[0].get("status"),
                     "batches": len(v[1]),
                     "steps": v[2] if len(v) > 2 else [],
                     "outputs": [o.get("file") for o in (v[0].get("outputs") or [])]}
            for k, v in results.items()}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print("\n  结果已存 /tmp/moss_batch_e2e_result.json")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
