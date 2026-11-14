#!/usr/bin/env python3
"""A5 的**真机**端到端验证：真 server.py 进程 + 真 SQLite + 假 ComfyUI。

【为什么跑真进程】前面的单测都是**在进程内**调用 server 的函数。
但 A5 的核心场景之一是「服务重启后的残留 running 自愈」——
那条路径只在 **main() 的启动流程**里被触发（init_db 之后）。
不跑一次真进程就等于没验。

【验证三件事】
  1. 启动时残留的 running（ComfyUI 侧没有）-> 自愈成 failed + 中文原因；
  2. /api/jobs 下发 running_truth / running_timeout_sec；
  3. /api/queue 下发 running_truth + running_state。

【不碰 GPU】用 tests/fake_comfyui.py（假 ComfyUI），真 ComfyUI 完全不参与。

运行：python3 tests/verify_a5_running_truth_live.py
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import fake_comfyui  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url: str, timeout: int = 20):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def read_status(db_path: Path, job_id: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT status, error FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        return (row["status"], row["error"]) if row else (None, None)
    finally:
        conn.close()


def seed_running(db_path: Path, job_id: str, prompt_id: str):
    """预置一条 status='running' 的行（模拟「服务重启前的残留」）。"""
    conn = sqlite3.connect(db_path)
    now = time.time()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        "id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,"
        "status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,"
        "progress_text TEXT, error TEXT, outputs TEXT,"
        "started_at REAL, finished_at REAL)")
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
        " prompt_id, progress, progress_text, outputs, started_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, now, "t2v", json.dumps({"prompt": "残留 running 验证"}),
         "running", prompt_id, 0.3, "生成中", "[]", now - 120))
    conn.commit()
    conn.close()


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  —— {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    comfy_port = free_port()
    comfy = fake_comfyui.serve(comfy_port)
    comfy_url = f"http://127.0.0.1:{comfy_port}"

    tmp = Path(tempfile.mkdtemp(prefix="h3-a5-live-"))
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    cfg = {
        "host": "127.0.0.1",
        "port": port,
        "comfyui_url": comfy_url,
        "data_dir": str(data_dir),
        "models_dir": "",
        "poll_interval": 1.5,
        # 运行上限压到 60 秒：验证超时判定不需要真的等半小时。
        "running_timeout_sec": 60,
    }
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                     encoding="utf-8")
    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")

    # 先建库（服务启动会 init_db，但我们要**先**塞进残留行）。
    db_path = data_dir / "jobs.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        "id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,"
        "status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,"
        "progress_text TEXT, error TEXT, outputs TEXT,"
        "started_at REAL, finished_at REAL)")
    conn.commit()
    conn.close()

    stale_id = "a5stale001"
    seed_running(db_path, stale_id, "prompt-that-no-longer-exists")

    # PYTHONUNBUFFERED=1 —— 否则 stdout 是管道时会被缓冲，
    # 服务端的 print（「[自愈] ...」）要等缓冲区满/进程退出才吐出来，
    # 于是我们**看不到**自愈日志（但自愈本身是生效的）。
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env, bufsize=1)
    log_lines = []

    def pump(seconds=3.0):
        """把子进程输出抽干（防止管道写满卡死）。"""
        import select
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([proc.stdout], [], [], 0.2)
            if r:
                line = proc.stdout.readline()
                if line:
                    log_lines.append(line.rstrip())
                else:
                    break

    try:
        # 等端口起来
        deadline = time.time() + 40
        up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    up = True
                    break
            except OSError:
                pump(0.3)
        check("服务启动", up, f"端口 {port}")
        if not up:
            print("\n".join(log_lines[-30:]))
            return 1
        pump(2.0)

        # ---- 1. 残留 running 自愈 ----
        deadline = time.time() + 20
        status = error = None
        while time.time() < deadline:
            status, error = read_status(db_path, stale_id)
            if status != "running":
                break
            time.sleep(0.5)
        check("残留 running 被自愈（不再是 running）", status != "running",
              f"status={status}")
        check("残留 running 自愈后状态是 failed", status == "failed",
              f"status={status}")
        check("失败原因说清了「为什么」", bool(error) and "运行中" in (error or ""),
              f"error={error!r}")
        check("失败原因不含原始 Python 异常文本",
              "Traceback" not in (error or "")
              and "object has no attribute" not in (error or ""),
              f"error={error!r}")

        # ---- 2. /api/jobs 下发运行真相字段 ----
        listing = http_json(f"http://127.0.0.1:{port}/api/jobs")
        jobs = listing.get("jobs") or []
        stale = next((j for j in jobs if j["id"] == stale_id), None)
        check("/api/jobs 能查到自愈后的任务", stale is not None)
        if stale:
            check("任务 payload 带 running_truth", "running_truth" in stale)
            check("任务 payload 带 running_timeout_sec",
                  "running_timeout_sec" in stale,
                  f"值={stale.get('running_timeout_sec')}")
            check("running_timeout_sec 等于配置的 60",
                  stale.get("running_timeout_sec") == 60,
                  f"值={stale.get('running_timeout_sec')}")
            check("已失败的任务 running_truth.state 为空",
                  (stale.get("running_truth") or {}).get("state") == "",
                  f"state={(stale.get('running_truth') or {}).get('state')!r}")

        # ---- 3. /api/queue ----
        snap = http_json(f"http://127.0.0.1:{port}/api/queue")
        check("/api/queue 带 running_timeout_sec",
              snap.get("running_timeout_sec") == 60,
              f"值={snap.get('running_timeout_sec')}")
        check("/api/queue 带 running_state 字段",
              "running_state" in snap,
              f"值={snap.get('running_state')!r}")

        # ---- 4. 服务端日志里能看到自愈记录 ----
        joined = "\n".join(log_lines)
        check("日志里有自愈记录", "[自愈]" in joined,
              joined[-400:] if "[自愈]" not in joined else "已记录")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        comfy.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=== 服务端日志（自愈段）===")
    for line in log_lines:
        if "[自愈]" in line:
            print("  " + line)
    print()
    if failures:
        print(f"=== {len(failures)} 项未通过 ===")
        for item in failures:
            print(" -", item)
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
