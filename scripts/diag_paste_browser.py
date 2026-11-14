#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真 Chromium 诊断：粘贴一张图后，界面到底停在哪一步。

【为什么需要这个】浏览器测试 test_paste_image_creates_real_attachment
红了（hint='正在入库素材库…'）。必须查清是：
  (a) 上传请求根本没发出；
  (b) 发出���服务端慢/失败；
  (c) 上传成功但前端没渲染卡片。
不能靠猜 —— 这里把每一层都打出来。

【产出】docs/agent-power/evidence/T3_paste_browser_diag.json + 截图。
"""

import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1] if len(sys.argv) > 1
           else REPO / "docs" / "agent-power" / "evidence")
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO))
from playwright.sync_api import sync_playwright      # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tiny_png() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
        "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="agp-paste-diag-"))
    port = _free_port()
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "H3STUDIO_PORT": str(port),
        "H3STUDIO_DATA_DIR": str(data_dir),
        "AGENT_SESSION_DB": str(data_dir / "agent_sessions.db"),
        "AGENT_MEMORY_DB": str(data_dir / "agent_memory.db"),
        "PYTHONPATH": str(REPO),
    })
    server = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    diag = {"port": port}
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    ready = True
                    break
            except OSError:
                if server.poll() is not None:
                    break
                time.sleep(0.5)
        if not ready:
            diag["fatal"] = "服务端起不来"
            print(json.dumps(diag, ensure_ascii=False))
            return 1
        diag["server_ready"] = True

        # 预置 Provider（图片入库要跑多模态分析）
        try:
            with urllib.request.urlopen("http://127.0.0.1:5000/v1/models",
                                        timeout=8) as resp:
                models = json.loads(resp.read().decode())
            model = models["data"][0]["id"]
        except Exception as exc:
            diag["fatal"] = f"Qwen 桥不可达：{exc!r}"
            print(json.dumps(diag, ensure_ascii=False))
            return 1
        conn = sqlite3.connect(data_dir / "jobs.db")
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
            """)
        conn.execute("INSERT OR REPLACE INTO model_providers VALUES "
                     "('diagprov','本机多模态','openai','http://127.0.0.1:5000',"
                     "'',0,'',? ,'',1,1,0)", (model,))
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs VALUES "
                     "('default','diagprov')")
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs VALUES "
                     "('default_multimodal','diagprov')")
        conn.commit()
        conn.close()
        diag["provider_model"] = model

        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page()
            reqs = []
            errors = []
            page.on("request", lambda r: reqs.append(
                {"method": r.method, "url": r.url,
                 "post_data_len": len(r.post_data or "")}))
            page.on("console", lambda m: errors.append(f"console:{m.type}:{m.text}"))
            page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))
            page.on("requestfailed", lambda r: errors.append(
                f"requestfailed:{r.url}:{r.failure}"))
            # 【关键】抓上传请求的**响应体** —— 只看 request 看不到服务端
            # 到底返回了什么（成功/失败/错误信息）。
            uploads = []

            def _on_response(resp):
                if "/api/agent/upload" in resp.url:
                    try:
                        body = resp.text()
                    except Exception as exc:
                        body = f"<body read failed: {exc!r}>"
                    uploads.append({"status": resp.status, "body": body[:1500]})

            page.on("response", _on_response)

            page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
            page.evaluate("() => { if (window.AgentUI) "
                          "window.AgentUI.show && window.AgentUI.show(); }")
            page.wait_for_timeout(800)
            diag["agent_ui_shown"] = page.evaluate(
                "() => !!document.querySelector('#agInput')")
            diag["handlePaste_present"] = page.evaluate(
                "() => typeof window.AgentUI !== 'undefined'")

            png_b64 = base64.b64encode(_tiny_png()).decode()
            t0 = time.time()
            page.evaluate(
                """(b64) => {
                    const bin = atob(b64);
                    const bytes = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
                    const file = new File([bytes], "clipboard_pasted.png",
                                          { type: "image/png" });
                    const dt = new DataTransfer();
                    dt.items.add(file);
                    const input = document.querySelector("#agInput");
                    input.focus();
                    const evt = new ClipboardEvent("paste", {
                        clipboardData: dt, bubbles: true, cancelable: true,
                    });
                    input.dispatchEvent(evt);
                }""", png_b64)
            diag["paste_dispatched_at"] = t0

            # 每 5 秒采一次状态，看它停在哪
            samples = []
            deadline = time.time() + 400
            found = False
            while time.time() < deadline:
                s = page.evaluate(
                    """() => ({
                        hint: (document.querySelector('#agAttachHint')||{}).textContent||'',
                        thumbs: document.querySelectorAll(
                            '#agAttachments .ag-attach-thumb').length,
                        boxes: document.querySelectorAll('#agAttachments *').length,
                        attachHtmlLen: (document.querySelector('#agAttachments')||{}).innerHTML.length||0,
                        busy: (document.querySelector('#agAttachBtn')||{}).disabled,
                    })""")
                s["t"] = round(time.time() - t0, 1)
                samples.append(s)
                if s["thumbs"] > 0:
                    found = True
                    break
                page.wait_for_timeout(5000)
            diag["samples"] = samples[-14:]
            diag["upload_requests"] = [r for r in reqs
                                       if "/api/agent/upload" in r["url"]]
            diag["upload_responses"] = uploads
            diag["errors"] = errors[:20]
            diag["found_thumb"] = found
            diag["elapsed"] = round(time.time() - t0, 1)

            page.screenshot(path=str(OUT / "T3_paste_browser.png"),
                            full_page=False)
            if found:
                diag["asset_ids"] = page.evaluate(
                    """() => [...document.querySelectorAll(
                        '#agAttachments .ag-attach-thumb')]
                        .map(n => n.getAttribute('data-asset-id')).filter(Boolean)""")
            browser.close()

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/assets?page=1&page_size=20",
                timeout=30) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("assets") or data.get("items") or []
        diag["asset_names"] = [str(a.get("name") or "") for a in items]

        (OUT / "T3_paste_browser_diag.json").write_text(
            json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(diag, ensure_ascii=False, indent=2)[:4000])
        return 0 if found else 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
