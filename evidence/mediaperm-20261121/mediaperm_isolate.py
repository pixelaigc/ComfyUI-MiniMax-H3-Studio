#!/usr/bin/env python3
"""隔离实验：起一个独立 8199 实例（假 ComfyUI + 真实库副本），
对比「有 running 任务」与「无 running 任务」时 /api/jobs 的真实耗时。

假 ComfyUI 可以被配置成「延迟 /queue」或「延迟 /history/{id}」，
用来复现「ComfyUI 正忙时卡片载入很慢」。

只读：真实线上库被复制，不写线上。
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

LIVE = "/root/projects/h3studio-live"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
PORT = 8199
FAKE_PORT = 8189

STATE = {"queue_delay": 0.0, "history_delay": 0.0}


class FakeComfy(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/queue"):
            time.sleep(STATE["queue_delay"])
            body = json.dumps({"queue_running": [], "queue_pending": []}).encode()
        elif self.path.startswith("/history/"):
            time.sleep(STATE["history_delay"])
            body = b"{}"
        else:
            body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_fake():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_port(port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def timed(url, n=3):
    out = []
    for _ in range(n):
        t = time.time()
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                b = r.read()
            out.append({"ms": round((time.time() - t) * 1000, 1), "bytes": len(b)})
        except Exception as e:
            out.append({"ms": None, "error": str(e)[:120]})
    return out


def main():
    start_fake()
    tmp = tempfile.mkdtemp(prefix="h3iso-")
    shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))
    # 复制 outputs 目录的**符号链接**即可（我们不读产物，只测列表接口）
    os.makedirs(os.path.join(tmp, "outputs"), exist_ok=True)

    env = os.environ.copy()
    env["H3STUDIO_DATA_DIR"] = tmp
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_COMFY_URL"] = f"http://127.0.0.1:{FAKE_PORT}"
    env["PYTHONPATH"] = LIVE
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=LIVE, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if not wait_port(PORT, 40):
            print("server failed to start")
            print(proc.stdout.read()[-3000:] if proc.stdout else "")
            return
        base = f"http://127.0.0.1:{PORT}"
        print(f"isolated instance up at {base}\n")

        # 让 DB 里有一条 running 任务（改副本，不动线上）
        import sqlite3
        conn = sqlite3.connect(os.path.join(tmp, "jobs.db"))
        conn.row_factory = sqlite3.Row
        # 找一条 done 的 t2v，改成 running 并给一个不存在的 prompt_id
        r = conn.execute(
            "SELECT id FROM jobs WHERE mode='t2v' AND status='done' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        if r:
            conn.execute(
                "UPDATE jobs SET status='running', prompt_id='ghost_prompt_xyz', "
                "started_at=? WHERE id=?", (time.time() - 30, r["id"]))
            conn.commit()
            print(f"injected running job {r['id']}\n")

        cases = [
            ("无 running / ComfyUI 空闲",
             {"state": "idle", "queue_delay": 0.0, "history_delay": 0.0}),
            ("无 running / ComfyUI /queue 慢 2s",
             {"state": "idle", "queue_delay": 2.0, "history_delay": 0.0}),
            ("有 running / ComfyUI 空闲",
             {"state": "running", "queue_delay": 0.0, "history_delay": 0.0}),
            ("有 running / ComfyUI /queue 慢 2s",
             {"state": "running", "queue_delay": 2.0, "history_delay": 0.0}),
            ("有 running / ComfyUI /history 慢 2s",
             {"state": "running", "queue_delay": 0.0, "history_delay": 2.0}),
        ]
        results = []
        for label, cfg in cases:
            STATE["queue_delay"] = cfg["queue_delay"]
            STATE["history_delay"] = cfg["history_delay"]
            # 缓存期过后再测（live state cache TTL = 3s）
            time.sleep(3.2)
            url = f"{base}/api/jobs?status=all&page=1&page_size=100"
            r = timed(url, 3)
            print(f"### {label}")
            print(f"    {r}")
            results.append({"label": label, **cfg, "timings": r})

        print("\n=== JSON ===")
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
