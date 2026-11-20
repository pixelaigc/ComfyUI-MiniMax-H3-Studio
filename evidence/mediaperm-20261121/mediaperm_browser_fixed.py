#!/usr/bin/env python3
"""在**隔离实例**上跑真实浏览器，验证修复后的前端体验。

打的是修好后的分支实例（不是线上），用真实 Chromium 采集：
  * 首屏 / 卡片出现 / networkidle 时间
  * /api/jobs 的浏览器 network timings
  * 每卡片产生的额外请求数 + 响应头
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import sync_playwright

SRC = "/root/projects/h3s-perf-fix"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
PORT = 8211
FAKE = 8286


class FakeComfy(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"{}"
        if self.path.startswith("/queue"):
            body = json.dumps({"queue_running": [], "queue_pending": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(OUT, exist_ok=True)
    fake = ThreadingHTTPServer(("127.0.0.1", FAKE), FakeComfy)
    import threading
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="h3browser-")
    shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))

    env = os.environ.copy()
    env["H3STUDIO_DATA_DIR"] = tmp
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_COMFY_URL"] = "http://127.0.0.1:%d" % FAKE
    env["PYTHONPATH"] = SRC
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=SRC, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        t0 = time.time()
        while time.time() - t0 < 45:
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            print("instance failed to start")
            return

        reqs = []
        base = "http://127.0.0.1:%d" % PORT
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
            page = ctx.new_page()

            def on_response(resp):
                try:
                    t = resp.request.timing
                    reqs.append({
                        "url": resp.url, "status": resp.status,
                        "type": resp.request.resource_type,
                        "total_ms": round(
                            t.get("receiveHeadersEnd", 0) - t.get("requestStart", 0), 1),
                    })
                except Exception:
                    pass

            page.on("response", on_response)
            t = time.time()
            page.goto(base + "/", wait_until="domcontentloaded")
            marks = {"dcl": round(time.time() - t, 3)}
            page.wait_for_selector(".job-card", timeout=30000)
            marks["first_card"] = round(time.time() - t, 3)
            page.wait_for_load_state("networkidle", timeout=30000)
            marks["networkidle"] = round(time.time() - t, 3)
            marks["cards"] = page.eval_on_selector_all(".job-card", "e => e.length")

            page.screenshot(path=os.path.join(OUT, "fixed-media-library.png"))

            api = [r for r in reqs if "/api/jobs" in r["url"]]
            files = [r for r in reqs if "/files/" in r["url"]]
            print("marks:", json.dumps(marks, ensure_ascii=False))
            print("api/jobs 请求数:", len(api))
            print("  总耗时 ms:", [r["total_ms"] for r in api])
            print("/files 请求数:", len(files))
            print("  总耗时 ms:", round(sum(r["total_ms"] for r in files), 1))
            out = {"marks": marks, "api": api, "files_count": len(files),
                   "files_total_ms": round(sum(r["total_ms"] for r in files), 1)}
            with open(os.path.join(OUT, "fixed-browser-probe.json"), "w") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        fake.shutdown()


if __name__ == "__main__":
    main()
