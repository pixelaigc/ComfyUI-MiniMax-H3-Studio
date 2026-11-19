#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷 2 的**确定性端到端证据**：把**真实采集到的 OOM history**
喂给线上服务自己的 worker 走一遍，断言「判失败 + 无黑图产物」。

【为什么需要这个（真机 OOM 的代价太大）】

真实 OOM 有两种结局，**都不是**我们想要的证据形态：

  1. ComfyUI 报 `status_str='error'` + `execution_error`（OOM 文本）
     -> 这是我们要的判定路径；
  2. ComfyUI 进程被显存打爆**直接死掉**（连接被拒 / 服务重启）
     -> history 被冲掉，服务端只拿到「连接失败」这种兜底异常，
        判成 failed 但**原因不是 OOM**，而且顺手把本机 ComfyUI 打挂
        （实测两次：20:56:19 与 21:12:00，都打挂了别人正在跑的任务）。

第 2 种在 48GB 卡上跑 1920x1088 x 15s 的 r2v（带参考音频）时**必现**。

【本脚本的做法】

用 `tests/fake_comfyui.py` 那种「假 ComfyUI」，但把 **/history 的返回
内容换成线上真实采集到的那条 OOM history**（存在
evidence/gen-status-20261119/comfy_oom_history.json 里）。

这样：
  * 走的是**线上服务自己的 worker 代码路径**（server.py 的轮询循环、
    gen_outcome 判定、产物下载、落库）；
  * 输入是**真实 ComfyUI 形状**（status_str='error' +
    execution_error 里带 OOM 文本 + outputs 里只有文本节点）；
  * **不占 GPU、不打挂 ComfyUI**、可重复跑。

【断言清单】
  1. 服务端把任务判成 **failed**（不是 done / 生成成功）；
  2. 失败原因里能看到 **显存不足 / OOM**（不是「未能给出具体原因」）；
  3. **outputs 为空** —— 前端拿不到任何产物 -> 不会渲染出黑图；
  4. **旧逻辑复算**：同一条 history，旧口径（只看 outputs 非空）判「成功」
     —— 证明这确实是原缺陷的触发条件；
  5. 前端「done + 空产物」分支有**明确失败占位文案**（不是空黑框）。

用法：python3 scripts/evidence_oom_failure_e2e.py [--shots]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO = Path("/root/projects/h3s-merge-20261119")
LIVE = Path("/root/projects/h3studio-live")
OUT = REPO / "evidence" / "gen-status-20261119"
OOM_HISTORY = OUT / "oom_history_production_shape.json"

RESULTS: list[dict] = []


def record(ok, name, detail):
    RESULTS.append({"ok": bool(ok), "name": name, "detail": str(detail)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return bool(ok)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------
# 假 ComfyUI：/history 回放**真实采集到的 OOM history**
# ------------------------------------------------------------------

OOM_ENTRY: dict = {}
OOM_PROMPT_ID = "oom-replay-prompt-0001"


class ReplayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path == "/prompt":
            self._send(200, {"prompt_id": OOM_PROMPT_ID})
            return
        self._send(200, {})

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/queue":
            # 任务已「完成」（失败），队列里没有它 —— 与真实 OOM 一致。
            self._send(200, {"queue_running": [], "queue_pending": []})
            return
        if path == "/system_stats":
            self._send(200, {"system": {"os": "linux"}, "devices": []})
            return
        if path.startswith("/history"):
            self._send(200, {OOM_PROMPT_ID: OOM_ENTRY})
            return
        self._send(200, {})


def start_replay_server(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), ReplayHandler)
    t = __import__("threading").Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def submit_job(base: str, mode: str, params: dict):
    boundary = "----h3oomreplayboundary"
    parts = []
    for name, value in (("mode", mode),
                        ("params", json.dumps(params, ensure_ascii=False))):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/jobs", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def get_json(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    global OOM_ENTRY
    OOM_ENTRY = json.loads(OOM_HISTORY.read_text(encoding="utf-8"))
    st = OOM_ENTRY.get("status") or {}
    print("═══ 回放的 OOM history ═══")
    print(f"  status_str = {st.get('status_str')!r}")
    print(f"  outputs    = {list((OOM_ENTRY.get('outputs') or {}).keys())}")
    for m in st.get("messages") or []:
        if isinstance(m, (list, tuple)) and len(m) > 1 and m[0] == "execution_error":
            print(f"  exception  = {m[1].get('exception_message','')[:160]!r}")

    # ---- 起假 ComfyUI + 隔离的 Studio 实例 ----
    comfy_port = free_port()
    studio_port = free_port()
    data_dir = Path(f"/tmp/h3-oom-e2e-{os.getpid()}/data")
    data_dir.mkdir(parents=True, exist_ok=True)

    srv = start_replay_server(comfy_port)
    print(f"\n═══ 起隔离实例 ═══\n  fake ComfyUI : {comfy_port}\n  studio      : {studio_port}")
    print(f"  data_dir    : {data_dir}")

    env = {
        **os.environ,
        "H3STUDIO_PORT": str(studio_port),
        "H3STUDIO_DATA_DIR": str(data_dir),
        "H3STUDIO_COMFY_URL": f"http://127.0.0.1:{comfy_port}",
        "PYTHONPATH": str(LIVE),
    }
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(LIVE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{studio_port}"
    try:
        # 等服务起来
        ok = False
        for _ in range(60):
            time.sleep(1)
            if proc.poll() is not None:
                print("  !! 服务进程退出:")
                print((proc.stdout.read() or "")[-3000:])
                return 2
            try:
                # 用 /api/jobs 做健康检查 —— **不能**用 /api/status/stream
                # （那是 SSE 长连接，永远不结束 -> 健康检查必然超时）。
                get_json(f"{base}/api/jobs", timeout=5)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            print("  !! 服务 60 秒内没起来")
            return 2
        print("  服务已就绪")

        # ---- 提交一条出图任务（走真实 worker 路径）----
        print("\n═══ 提交任务（走线上 worker 代码路径）═══")
        resp = submit_job(base, "t2i", {
            "prompt": "a red cube on a white table, studio light",
            "width": 1024, "height": 1024, "seed": 42, "steps": 4,
        })
        job_id = resp.get("id")
        print(f"  任务已提交: {job_id}")

        deadline = time.time() + 180
        job = None
        while time.time() < deadline:
            job = get_json(f"{base}/api/jobs/{job_id}")
            if job.get("status") in ("done", "failed", "canceled"):
                break
            time.sleep(2)
        else:
            record(False, "任务在 3 分钟内到终态", f"仍是 {job.get('status')}")
            return 1

        print(f"  终态: status={job.get('status')}")
        print(f"  error: {job.get('error')}")
        print(f"  outputs: {json.dumps(job.get('outputs'), ensure_ascii=False)}")

        record(job.get("status") == "failed",
               "OOM 任务判定为 failed（不是 done / 生成成功）",
               f"status={job.get('status')}")

        err = str(job.get("error") or "")
        record("显存不足" in err or "out of memory" in err.lower(),
               "失败原因明确指出显存不足 / OOM（不是「未能给出具体原因」）",
               f"error={err[:180]}")

        outs = job.get("outputs") or []
        record(len(outs) == 0,
               "失败任务没有任何产物条目（前端不会渲染出黑图）",
               f"outputs={json.dumps(outs, ensure_ascii=False)[:200]}")

        # ---- 旧逻辑复算：同一条 history 的两种判定 ----
        sys.path.insert(0, str(REPO))
        from h3studio import gen_outcome  # noqa: E402
        legacy_success = bool(OOM_ENTRY.get("outputs"))
        new = gen_outcome.history_outcome(OOM_ENTRY)
        print(f"\n  【原缺陷触发条件】旧逻辑（只看 outputs 非空）会判成功 = "
              f"{legacy_success}")
        print(f"  【新逻辑】outcome={new['outcome']!r} failed={new['failed']}")
        record(legacy_success and new["failed"],
               "同一条 history：旧逻辑误判成功，新逻辑判失败（判定顺序修复生效）",
               f"legacy_success={legacy_success} new_outcome={new['outcome']}")

        # ---- 前端「done + 空产物」必须有明确占位，不是黑框 ----
        app_js = (LIVE / "web" / "app.js").read_text(encoding="utf-8")
        agent_js = (LIVE / "web" / "agent.js").read_text(encoding="utf-8")
        marker = "没有产物文件"
        record(marker in app_js and marker in agent_js,
               "前端对「结束但无产物」有明确失败占位文案（不是空黑框）",
               f"app.js/agent.js 均含 {marker!r}")

        out = {
            "job_id": job_id,
            "mode": "t2i",
            "replayed_history": {
                "status_str": st.get("status_str"),
                "completed": st.get("completed"),
                "outputs_keys": list((OOM_ENTRY.get("outputs") or {}).keys()),
                "messages": st.get("messages"),
                "source": str(OOM_HISTORY),
            },
            "studio_job": {
                "status": job.get("status"),
                "error": job.get("error"),
                "outputs": job.get("outputs"),
            },
            "legacy_would_succeed": legacy_success,
            "new_outcome": new["outcome"],
            "verdict_failed": job.get("status") == "failed",
            "verdict_no_outputs": len(outs) == 0,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (OUT / "evidence_oom_failure_e2e.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  证据已存: {OUT/'evidence_oom_failure_e2e.json'}")

        if args.shots:
            shot_job(base, job_id)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        srv.shutdown()

    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n通过 {passed}/{len(RESULTS)}")
    (OUT / "evidence_oom_e2e_summary.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passed == len(RESULTS) else 1


def shot_job(base: str, job_id: str):
    """真实浏览器打开失败卡片并截图，断言没有 <img>/<video>。"""
    print("\n═══ 真实浏览器截图 ═══")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        record(False, "浏览器截图", f"缺 playwright: {exc}")
        return
    shot = str(OUT / "oom-failed-card-e2e.png")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.goto(f"{base}/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            for sel in ("text=生成库", "text=作品", "a:has-text('生成')"):
                try:
                    page.locator(sel).first.click(timeout=4000)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(2500)
            card = page.locator(f'.job-card[data-jid="{job_id}"]')
            if card.count():
                card.first.scroll_into_view_if_needed()
                page.wait_for_timeout(800)
                card.first.screenshot(path=shot)
                print(f"  已截图失败卡片: {shot}")
                has_media = card.first.locator(
                    ".job-preview img, .job-preview video").count()
                record(has_media == 0,
                       "失败卡片里没有 <img>/<video>（不会显示全黑预览图）",
                       f"媒体元素数={has_media}")
                text = card.first.inner_text()
                print(f"  卡片文本: {text[:240]!r}")
                # 失败卡片显示的是「✕ + 失败原因」（显存不足），
                # 不一定带「失败」两个字 —— 断言要看**原因**在不在。
                record("显存不足" in text or "out of memory" in text.lower(),
                       "失败卡片上显示了明确失败原因（显存不足 / OOM）",
                       f"卡片文本片段={text[:160]!r}")
            else:
                record(False, "页面上找到该失败任务卡片", "找不到")
            browser.close()
    except Exception as exc:
        record(False, "浏览器截图", f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
