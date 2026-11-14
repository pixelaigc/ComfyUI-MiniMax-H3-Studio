#!/usr/bin/env python3
"""A5 的**浏览器**验证：队列面板真的显示「已超时 / 状态未知」而不是永远「运行中」。

【为什么必须用真浏览器】
「界面显示的是不是真实状态」是**渲染**层的事 ——
单元测试只能验逻辑，验不了「用户看到的字」。
这里起真 server.py（假 ComfyUI，不占 GPU）+ 真 chromium，
往库里塞一条「卡住的 running」（started_at 在很久以前），
然后看队列面板**实际显示**什么字。

【不碰 GPU】用 tests/fake_comfyui.py。

运行：python3 tests/verify_a5_queue_panel_browser.py
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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import fake_comfyui  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:
    sync_playwright = None
    HAS_PLAYWRIGHT = False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    if not HAS_PLAYWRIGHT:
        print("SKIP：未安装 playwright")
        return 0

    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f"  —— {detail}" if detail else ""))
        if not cond:
            failures.append(name)

    comfy_port = free_port()
    comfy = fake_comfyui.serve(comfy_port)
    tmp = Path(tempfile.mkdtemp(prefix="h3-a5-browser-"))
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    cfg = {
        "host": "127.0.0.1", "port": port,
        "comfyui_url": f"http://127.0.0.1:{comfy_port}",
        "data_dir": str(data_dir), "models_dir": "",
        "poll_interval": 1.5,
        # 运行上限 600 秒 -> 我们塞一条「started_at 在 2 小时前」的 running，
        # 它必然超限 -> 面板必须显示「已超时」。
        "running_timeout_sec": 600,
    }
    (tmp / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                     encoding="utf-8")
    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")

    db_path = data_dir / "jobs.db"

    def seed_running(job_id, prompt_id, started_at):
        """往库里塞一条 status='running' 的行。"""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,"
            "status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,"
            "progress_text TEXT, error TEXT, outputs TEXT,"
            "started_at REAL, finished_at REAL)")
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO jobs (id, created_at, mode, params, status,"
            " prompt_id, progress, progress_text, outputs, started_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, now, "t2v",
             json.dumps({"prompt": f"卡住的 running 面板验证 {job_id}"}),
             "running", prompt_id, 0.4, "生成中", "[]", started_at))
        conn.commit()
        conn.close()

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=env)
    pw = browser = None
    try:
        deadline = time.time() + 40
        up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    up = True
                    break
            except OSError:
                time.sleep(0.2)
        check("服务启动", up, f"端口 {port}")
        if not up:
            return 1

        pw = sync_playwright().start()
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")

        # 等队列面板出现
        try:
            page.wait_for_selector("#queuePanel:not([hidden])", timeout=20000)
        except Exception:
            pass
        time.sleep(2.0)

        # ---- 关键：这些行是**服务启动之后**才塞进去的 ----
        #
        # 【为什么】启动时塞进去的话，会被「残留 running 自愈」直接判失败
        # （那是**另一个**要验的东西，已经在 verify_a5_running_truth_live.py 里验了）。
        # 这里要验的是**前端兜底**：DB 里明明还是 running，
        # 但服务端算出来的真相是「已超时 / 已丢失」—— 前端必须照实显示。
        now = time.time()
        # ① 超时：started_at 在 2 小时前，上限 600 秒 -> 必然超限
        seed_running("stuckjob01", "prompt-gone-forever", now - 7200)
        # ② 丢失：started_at 在 60 秒前（超过 10 秒宽限期、没超 600 秒上限），
        #    而 prompt_id 不在 ComfyUI 队列里、history 里也没有 -> 判定为「已丢失」
        seed_running("lostjob01", "prompt-not-in-queue", now - 60)
        time.sleep(4.0)

        states = page.evaluate(
            "() => Array.from(document.querySelectorAll('#queueList .queue-item'))"
            ".map(li => ({"
            "  state: li.querySelector('.queue-state')?.textContent.trim(),"
            "  cls: li.className,"
            "  jid: li.dataset.jid || '',"
            "}))")
        print(f"  队列条目: {states}")
        stuck = next((s for s in states if s["jid"] == "stuckjob01"), None)
        check("队列面板里能看到那条卡住的任务", stuck is not None,
              f"states={states}")
        if stuck:
            check("超限的任务显示「已超时」而不是「运行中」",
                  stuck["state"] == "已超时",
                  f"实际显示={stuck['state']!r}")
            check("超限的任务带警示 class",
                  "is-warn-timeout" in (stuck["cls"] or ""),
                  f"class={stuck['cls']!r}")

        lost = next((s for s in states if s["jid"] == "lostjob01"), None)
        check("队列面板里能看到那条丢失的任务", lost is not None)
        if lost:
            check("ComfyUI 里已无的任务显示「已丢失」而不是「运行中」",
                  lost["state"] == "已丢失",
                  f"实际显示={lost['state']!r}")
            check("丢失的任务带警示 class",
                  "is-warn-lost" in (lost["cls"] or ""),
                  f"class={lost['cls']!r}")

        summary = page.evaluate(
            "() => (document.getElementById('queueSummary')||{}).textContent || ''")
        check("摘要里点出了「状态异常」", "状态异常" in (summary or ""),
              f"summary={summary!r}")
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        comfy.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

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
