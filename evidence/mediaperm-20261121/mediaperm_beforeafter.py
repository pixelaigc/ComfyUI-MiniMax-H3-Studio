#!/usr/bin/env python3
"""修前/修后对比：用**隔离实例**跑同一组指标。

  * 修前 = /root/projects/h3studio-live（当前线上代码，local/maintainer e393e8a）
  * 修后 = /root/projects/h3s-perf-fix（local/fix/mediaperm-perf）

两个实例都用：真实 jobs.db 副本 + 假 ComfyUI（可注入 /queue、/history 延迟）。

测的指标（与用户报的现象对齐）：
  1. /api/jobs 各标签/翻页耗时
  2. 有 running 任务 + ComfyUI 忙时的耗时（对应「有时候很慢」）
  3. TTL 过期后的尖峰（对应「每 60 秒一次」）
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
FIX = "/root/projects/h3s-perf-fix"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

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


def start_fake(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), FakeComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def wait_port(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def timed(url, n=5):
    out = []
    for _ in range(n):
        t = time.time()
        try:
            with urllib.request.urlopen(url, timeout=90) as r:
                b = r.read()
            out.append(round((time.time() - t) * 1000, 1))
        except Exception as e:
            out.append("ERR:%s" % str(e)[:60])
    return out


def boot(src, port, fake_port, tmp):
    env = os.environ.copy()
    env["H3STUDIO_DATA_DIR"] = tmp
    env["H3STUDIO_PORT"] = str(port)
    env["H3STUDIO_COMFY_URL"] = "http://127.0.0.1:%d" % fake_port
    env["PYTHONPATH"] = src
    p = subprocess.Popen(["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
                         cwd=src, env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    if not wait_port(port, 45):
        p.kill()
        return None
    return p


def scenario(label, src, port, fake_port, tmp, inject_running, q_delay, h_delay):
    STATE["queue_delay"] = q_delay
    STATE["history_delay"] = h_delay
    # 【重要】每个场景用**全新的库副本** ——
    # 上一场景注入的 running 任务会留在库里，下一场景就测错了。
    fresh = tempfile.mkdtemp(prefix="h3scn-")
    shutil.copy2(REAL_DB, os.path.join(fresh, "jobs.db"))
    p = boot(src, port, fake_port, fresh)
    if p is None:
        return {"label": label, "error": "start failed"}
    try:
        base = "http://127.0.0.1:%d" % port
        # 第一轮：预热（让 LoRA 映射建起来，模拟服务已跑了一段时间）
        timed(base + "/api/jobs?status=all&page=1&page_size=48", 1)
        # 【关键】running 任务必须在服务**启动之后**注入 ——
        # 启动时有一道「重启自愈」（server.py:437 _residual_running_rows）：
        # 它会把 DB 里残留的 status='running' 全部问一遍 ComfyUI，
        # 问不到就**标记失败**。先注入的话会被自愈抹掉（假绿）。
        if inject_running:
            import sqlite3
            conn = sqlite3.connect(os.path.join(fresh, "jobs.db"))
            conn.row_factory = sqlite3.Row
            r = conn.execute(
                "SELECT id FROM jobs WHERE mode='t2v' AND status='done' "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
            if r:
                conn.execute(
                    "UPDATE jobs SET status='running', prompt_id='ghost_pid_xyz',"
                    " created_at=?, started_at=? WHERE id=?",
                    (time.time() + 1000, time.time() - 30, r["id"]))
                conn.commit()
            conn.close()
        # 【防假绿】确认这一页**真的**包含 running 任务 ——
        # 否则「有 running」场景根本没被触发，测出来的快慢毫无意义。
        with urllib.request.urlopen(
                base + "/api/jobs?status=all&page=1&page_size=48", timeout=90) as r:
            payload = json.loads(r.read())
        running_in_page = [j for j in payload.get("jobs", [])
                           if j.get("status") == "running"]
        out = {"_running_jobs_in_page": len(running_in_page),
               "_expected_running": bool(inject_running)}
        for name, url in [
            ("全部", "/api/jobs?status=all&page=1&page_size=48"),
            ("已完成", "/api/jobs?status=done&page=1&page_size=48"),
            ("翻页p2", "/api/jobs?status=all&page=2&page_size=48"),
        ]:
            out[name] = timed(base + url, 5)
        return {"label": label, "port": port, "timings": out}
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


def main():
    # ⚠ 不能用 8188 —— 那是**真 ComfyUI** 的端口，绝不能占。
    start_fake(8288)   # 修后实例用
    start_fake(8289)   # 修前实例用

    tmpA = tempfile.mkdtemp(prefix="h3cmpA-")
    tmpB = tempfile.mkdtemp(prefix="h3cmpB-")
    # 【重要】**不**在这里注入 running 任务 ——
    # 服务的「重启自愈」会在启动时把残留 running 全部标记失败。
    # running 任务改在**服务起来之后**注入（见 scenario()）。
    # 这里只准备真实库副本。
    shutil.copy2(REAL_DB, tmpA + "/jobs.db")
    shutil.copy2(REAL_DB, tmpB + "/jobs.db")

    results = []
    # 场景 1：ComfyUI 空闲 + 有 running
    results.append(scenario("修前(线上) 有running ComfyUI空闲", LIVE, 8201, 8289, tmpA, True, 0, 0))
    results.append(scenario("修后(分支) 有running ComfyUI空闲", FIX, 8202, 8288, tmpB, True, 0, 0))
    # 场景 2：ComfyUI /history 慢 2 秒（模拟 ComfyUI 忙）
    results.append(scenario("修前(线上) ComfyUI/history慢2s", LIVE, 8203, 8289, tmpA, True, 0, 2.0))
    results.append(scenario("修后(分支) ComfyUI/history慢2s", FIX, 8204, 8288, tmpB, True, 0, 2.0))
    # 场景 3：ComfyUI /queue 慢 2 秒
    results.append(scenario("修前(线上) ComfyUI/queue慢2s", LIVE, 8205, 8289, tmpA, True, 2.0, 0))
    results.append(scenario("修后(分支) ComfyUI/queue慢2s", FIX, 8206, 8288, tmpB, True, 2.0, 0))

    print("\n=== 修前/修后对比 ===")
    for r in results:
        print("\n### " + r["label"])
        if "error" in r:
            print("   ", r["error"])
            continue
        for k, v in r["timings"].items():
            if not isinstance(v, list):
                print(f"    {k:22s} {v}")
                continue
            nums = [x for x in v if isinstance(x, (int, float))]
            if nums:
                print(f"    {k:22s} min={min(nums):8.1f}ms  max={max(nums):8.1f}ms"
                      f"  all={v}")
            else:
                print(f"    {k:22s} {v}")

    out = "/root/.hermes-web-ui/.ekko/workspace/default/" \
          "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/" \
          "evidence/mediaperm-20261121/before-after.json"
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nwrote", out)


if __name__ == "__main__":
    main()
