# -*- coding: utf-8 -*-
"""
【一次性 DOM 探针】确认 375px 下会话侧栏的真实渲染情况。

用途：截图让 VLM 看图判断容易看走眼（375px 抽屉是全宽覆盖，
但截图里被主区盖住一半，VLM 容易误判成「没显示」）。
这个脚本直接读 DOM 的**几何尺寸与文本**，是确定性的证据。

用法：
  cd /root/projects/h3studio-agent-session
  VERIFY_SHOTS=/tmp/probe python scripts/probe_session_drawer_layout.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_png import make_png  # noqa: E402

REPO = Path(os.environ.get("VERIFY_REPO", "/root/projects/h3studio-agent-session"))
PORT = int(os.environ.get("VERIFY_PORT", "8271"))
SHOTS = Path(os.environ.get("VERIFY_SHOTS", "/tmp/probe-shots"))


def api(path, method="GET", body=None):
    data = None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    req.data = data
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def seed_fake_provider(data_dir: Path, fake_port: int) -> None:
    """
    种一个指向假 Provider 的模型 —— /api/agent/chat 需要可用的 provider。

    【为什么需要】Agent 的 provider 解析走「本地模型配置」，
    没有 provider 时 /api/agent/chat 直接 400。
    这个探针验的是**布局**，不需要真的推理，所以用最快的假端点。
    """
    db = data_dir / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS model_providers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
            base_url TEXT NOT NULL, llm_base_url TEXT DEFAULT '',
            use_proxy INTEGER DEFAULT 0, proxy_base_url TEXT DEFAULT '',
            model TEXT NOT NULL, api_key TEXT DEFAULT '',
            multimodal INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
            created_at REAL);
        CREATE TABLE IF NOT EXISTS model_provider_prefs (
            key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO model_providers VALUES "
        "('fakedrawer','假布局模型','openai',?,'',0,'','fake-model','',1,1,0)",
        (f"http://127.0.0.1:{fake_port}",))
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default','fakedrawer')")
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default_multimodal','fakedrawer')")
    conn.commit()
    conn.close()


class FakeProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：立刻返回一句话。"""

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {
            "id": "chatcmpl-fake", "object": "chat.completion",
            "created": int(time.time()), "model": "fake-model",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                  "content": "好的。"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                      "total_tokens": 20},
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    from http.server import HTTPServer
    import threading

    fake_port = PORT + 1
    fake_server = HTTPServer(("127.0.0.1", fake_port), FakeProviderHandler)
    threading.Thread(target=fake_server.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="probe-drawer-"))
    env = dict(os.environ)
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(tmp / "data")
    env["AGENT_SESSION_DB"] = str(tmp / "data" / "agent_sessions.db")
    env["ASSET_LIBRARY_ROOT"] = str(tmp / "asset_library")
    (tmp / "data" / "outputs" / "images").mkdir(parents=True, exist_ok=True)
    (tmp / "asset_library").mkdir(parents=True, exist_ok=True)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        started = False
        for _ in range(120):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{PORT}/api/agent/sessions", timeout=2) as r:
                    if r.status == 200:
                        started = True
                        break
            except Exception:
                time.sleep(0.5)
        if not started:
            print("服务没起来")
            return 1

        # 造 2 个会话 + 1 个带图片产物的任务
        seed_fake_provider(tmp / "data", fake_port)
        sids = []
        for text in ("帮我做一段雨夜的赛博朋克视频", "用素材库里的林黛玉做一段电影场景"):
            sid = api("/api/agent/sessions", "POST", {})["session_id"]
            api("/api/agent/chat", "POST", {"message": text, "session_id": sid})
            sids.append(sid)
        png_dir = tmp / "data" / "outputs" / "aaaa1111bbbb"
        png_dir.mkdir(parents=True, exist_ok=True)
        (png_dir / "preview.png").write_bytes(make_png(64, 36))
        with sqlite3.connect(tmp / "data" / "jobs.db") as conn:
            conn.execute(
                "INSERT INTO jobs (id, created_at, mode, params, status, session_id, outputs)"
                " VALUES (?,?,?,?,?,?,?)",
                ("aaaa1111bbbb", time.time(), "t2i",
                 json.dumps({"prompt": "雨夜霓虹", "aspect": "16:9",
                             "width": 1344, "height": 768}, ensure_ascii=False),
                 "done", sids[1],
                 json.dumps([{"kind": "image", "file": "preview.png",
                              "name": "preview.png"}], ensure_ascii=False)))
            conn.commit()

        SHOTS.mkdir(parents=True, exist_ok=True)
        report = {}
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            for width in (375, 390, 1280):
                ctx = browser.new_context(viewport={"width": width, "height": 780})
                page = ctx.new_page()
                errors = []
                page.on("console", lambda m: errors.append(m.text)
                        if m.type == "error" else None)
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                # 先切到 Agent 页 —— 默认打开的是创作页，#agMenu 在 Agent 页里，
                # 不切过去它「不可见」（page.click 会一直等到超时）。
                page.evaluate(
                    "() => { const el = document.querySelector('#navAgent');"
                    " if (el) el.click(); }")
                page.wait_for_timeout(900)
                page.click("#agMenu")
                page.wait_for_timeout(900)

                data = page.evaluate("""() => {
                  const drawer = document.querySelector('#agDrawer');
                  const dr = drawer.getBoundingClientRect();
                  const items = [...document.querySelectorAll('.ag-session-item')];
                  const metas = [...document.querySelectorAll('.ag-session-item-meta')];
                  return {
                    viewportW: window.innerWidth,
                    drawer: {left: dr.left, width: dr.width, right: dr.right},
                    docScrollW: document.documentElement.scrollWidth,
                    docClientW: document.documentElement.clientWidth,
                    itemCount: items.length,
                    items: items.map(it => {
                      const r = it.getBoundingClientRect();
                      const meta = it.querySelector('.ag-session-item-meta');
                      const mr = meta ? meta.getBoundingClientRect() : null;
                      const st = it.querySelector('.ag-session-status');
                      return {
                        title: (it.querySelector('.ag-session-item-title')||{}).textContent || '',
                        itemW: r.width,
                        itemRight: r.right,
                        metaText: meta ? meta.textContent.replace(/\\s+/g,' ').trim() : '',
                        metaH: mr ? mr.height : 0,
                        metaScrollW: mr ? mr.scrollWidth : 0,
                        metaClientW: mr ? mr.clientWidth : 0,
                        statusLabel: st ? st.textContent.trim() : '',
                        statusVisible: st ? (st.getBoundingClientRect().width > 0) : false,
                      };
                    }),
                    createdCount: document.querySelectorAll('.ag-session-item-created').length,
                    statusCount: document.querySelectorAll('.ag-session-status').length,
                  };
                }""")
                data["consoleErrors"] = errors
                report[width] = data
                page.screenshot(path=str(SHOTS / f"drawer-{width}.png"))
                ctx.close()
            browser.close()

        print(json.dumps(report, ensure_ascii=False, indent=2))

        # ---- 判定 ----
        problems = []
        for width, d in report.items():
            if d["docScrollW"] > d["docClientW"] + 1:
                problems.append(f"[{width}] 页面横向溢出 "
                                f"{d['docScrollW']} > {d['docClientW']}")
            # 抽屉宽度：窄屏（<=700px）是**全宽覆盖**，
            # 宽屏是 300px 的侧边栏（不占满主区）。
            # 断言要分档 —— 不能拿宽屏去套窄屏的规则。
            if width <= 700:
                if d["drawer"]["width"] < width * 0.9:
                    problems.append(f"[{width}] 抽屉宽度只有 {d['drawer']['width']}，"
                                    f"窄屏下应该全宽覆盖 {width}")
            else:
                if not (250 <= d["drawer"]["width"] <= 360):
                    problems.append(f"[{width}] 宽屏抽屉宽度 {d['drawer']['width']}，"
                                    f"应该在 250~360（侧边栏，不是全宽）")
            if d["createdCount"] < 1:
                problems.append(f"[{width}] 没有创建时间元素")
            if d["statusCount"] < 1:
                problems.append(f"[{width}] 没有状态徽章元素")
            for it in d["items"]:
                # metaScrollW / metaClientW 可能是 null（元素不存在）——
                # null > 1 会直接抛 TypeError，把探针自己搞崩。
                if (it["metaScrollW"] is not None
                        and it["metaClientW"] is not None
                        and it["metaScrollW"] > it["metaClientW"] + 1):
                    problems.append(f"[{width}] meta 行内容溢出 "
                                    f"{it['metaScrollW']} > {it['metaClientW']}")
                if not it["statusVisible"]:
                    problems.append(f"[{width}] 状态徽章不可见")
                if not it["statusLabel"]:
                    problems.append(f"[{width}] 状态徽章文案为空")
                # meta 行必须**换行**放得下（高度 > 单行高度说明确实折行了）
                if it["metaH"] and it["metaH"] < 14:
                    problems.append(f"[{width}] meta 行高度异常 {it['metaH']}")
            if d["consoleErrors"]:
                problems.append(f"[{width}] console 错误: {d['consoleErrors'][:3]}")
        print()
        if problems:
            for p in problems:
                print("PROBLEM:", p)
            return 1
        print("PROBE OK：抽屉全宽 / 创建时间 / 状态徽章 / 无横向溢出 / console 0 错误")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
