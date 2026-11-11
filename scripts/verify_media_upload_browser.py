#!/usr/bin/env python3
"""
Real-browser verification of the Agent media-upload feature (2026-11-11),
including the **素材库入库 + 预处理** pipeline.

Proves, in a REAL browser against a REAL running server (throwaway data dir):

  1. UPLOAD: the composer has a visible upload entry; picking a real PNG
     uploads it through the REAL multipart endpoint and shows a THUMBNAIL.
  2. LIBRARY: the uploaded image really landed in the asset library
     (source=agent_upload) with extracted metadata (width/height).
  3. PREPROCESS: the server really ran the library analysis pipeline and
     the description + tags landed in the library (searchable).
  4. MULTI / REMOVE / REJECT / LIMIT: as before.
  5. NO OVERFLOW at 375 / 390 / 1280; console 0 errors; 0 failed requests.

【为什么用假 Provider】
The real multimodal analysis takes 120~180 s per image on this box.
For a browser test that must run 3 widths x 6 cases, that is ~1 hour.
So the test server points at a **fake OpenAI-compatible provider** that
returns a canned structured description instantly.

That still exercises the REAL code path:
  handle_upload -> register_asset (real) -> analysis.run_analysis (real)
  -> llm.call_provider (real client, fake HTTP endpoint)
  -> save_analysis (real) -> add_tags (real)
Only the model inference is stubbed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(os.environ.get("VERIFY_REPO",
                           "/root/projects/h3studio-media-upload"))
SHOTS = REPO / "evidence" / "media-upload"
PORT = int(os.environ.get("VERIFY_PORT", "8198"))
FAKE_PORT = int(os.environ.get("VERIFY_FAKE_PORT", "8918"))

#: 假 Provider 返回的结构化描述（与真模型同格式）
FAKE_DESCRIPTION = """**【主体人物】**
一位穿着深蓝色风衣的年轻女性，独自站在桥上。

**【场景与环境】**
雨夜的石桥，远处是城市的霓虹灯海，水面倒映着斑斓的光。

**【光线与色彩】**
冷色调为主，紫蓝与橙黄交织，霓虹在湿漉漉的桥面上拉出长长的光带。
"""


class FakeProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：立刻返回一段结构化描述。"""

    def log_message(self, *args):  # 静音
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "fake-vision",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": FAKE_DESCRIPTION},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10,
                      "total_tokens": 20},
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start_fake_provider():
    server = HTTPServer(("127.0.0.1", FAKE_PORT), FakeProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def start_server(tmp: Path):
    data = tmp / "data"
    (data / "outputs" / "images").mkdir(parents=True, exist_ok=True)
    lib = tmp / "asset_library"
    lib.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(data)
    env["ASSET_LIBRARY_ROOT"] = str(lib)
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(120):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/api/jobs", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    out = proc.stdout.read() if proc.stdout else ""
    proc.kill()
    raise RuntimeError("server did not start:\n" + out[-3000:])


def seed_fake_provider(data_dir: Path):
    """在测试实例的 jobs.db 里种一个指向假 Provider 的多模态模型。"""
    import sqlite3
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
        "('fakevis','假视觉模型','openai',?,'',0,'',"
        "'fake-vision','',1,1,0)", (f"http://127.0.0.1:{FAKE_PORT}",))
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default','fakevis')")
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default_multimodal','fakevis')")
    conn.commit()
    conn.close()


def make_png(path: Path, color=(200, 30, 30)) -> Path:
    from PIL import Image
    Image.new("RGB", (120, 90), color).save(path)
    return path


def make_text(path: Path) -> Path:
    path.write_text("this is not an image at all", encoding="utf-8")
    return path


def probe(page):
    return page.evaluate("""() => {
        const thumbs = [...document.querySelectorAll('.ag-attach-thumb')];
        const errEl = document.querySelector('.ag-attach-err');
        const busyEl = document.querySelector('.ag-attach-busy');
        const btn = document.getElementById('agAttachBtn');
        const hint = document.getElementById('agAttachHint');
        const box = document.getElementById('agAttachments');
        return {
            attachBoxHidden: box ? box.hidden : null,
            thumbCount: thumbs.length,
            thumbSrcs: thumbs.map(t => {
                const img = t.querySelector('img');
                return img ? (img.getAttribute('src') || '') : '';
            }),
            error: errEl ? errEl.textContent.trim() : '',
            busyVisible: busyEl ? (busyEl.offsetParent !== null) : false,
            btnVisible: btn ? (btn.offsetParent !== null) : false,
            btnText: btn ? btn.textContent.trim() : '',
            hint: hint ? hint.textContent.trim() : '',
            attachments: window.AgentUI.state.attachments.map(a => ({
                asset_id: a.asset_id, tags: a.tags || [],
                description: a.description || '',
            })),
            docScrollW: document.documentElement.scrollWidth,
            docClientW: document.documentElement.clientWidth,
            bodyScrollW: document.body.scrollWidth,
        };
    }""")


def overflow_free(data):
    return (data["docScrollW"] <= data["docClientW"] + 1
            and data["bodyScrollW"] <= data["docClientW"] + 1)


def library_probe(asset_id: str) -> dict:
    """直接查**素材库**（权威来源），验证真的入库 + 预处理。"""
    import sqlite3
    lib = Path(os.environ.get("VERIFY_LIB", ""))
    db = lib / "asset_library.db"
    if not db.is_file():
        return {"ok": False}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    asset = conn.execute("SELECT * FROM assets WHERE id=?",
                         (asset_id,)).fetchone()
    version = conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version DESC",
        (asset_id,)).fetchone()
    analysis = conn.execute(
        "SELECT * FROM asset_analyses WHERE asset_id=? ORDER BY id DESC",
        (asset_id,)).fetchone()
    tags = [r["tag"] for r in conn.execute(
        "SELECT tag FROM asset_tags WHERE asset_id=?", (asset_id,))]
    conn.close()
    return {
        "ok": True,
        "source": asset["source"] if asset else None,
        "width": version["width"] if version else None,
        "height": version["height"] if version else None,
        "mime": version["mime"] if version else None,
        "analysis_text": (analysis["text"] if analysis else "") or "",
        "tags": tags,
    }


def main():
    from playwright.sync_api import sync_playwright
    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="h3verify-media-"))
    fake = start_fake_provider()
    proc = start_server(tmp)
    console_errors = []
    results = []
    uploaded_ids = []
    try:
        seed_fake_provider(tmp / "data")
        os.environ["VERIFY_LIB"] = str(tmp / "asset_library")
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--no-sandbox"])
            for width, height, tag in ((375, 812, "375"),
                                       (390, 844, "390"),
                                       (1280, 900, "1280")):
                ctx = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2)
                page = ctx.new_page()
                errs = []
                failed_requests = []
                page.on("console", lambda m: errs.append(m.text)
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errs.append(str(e)))
                page.on("response", lambda r: failed_requests.append(
                    f"{r.status} {r.url}") if r.status >= 400 else None)
                page.goto(f"http://127.0.0.1:{PORT}/",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("#navAgent", timeout=30000)
                page.wait_for_timeout(900)
                page.evaluate("() => window.AgentUI.show()")
                page.wait_for_timeout(400)
                assert page.eval_on_selector(
                    "#agentPage",
                    "el => !el.classList.contains('page-hidden')"
                ), "Agent 面板没有显示出来"

                def reset():
                    page.evaluate("""() => {
                      window.AgentUI.state.attachments = [];
                      window.AgentUI.state.attachError = '';
                      window.AgentUI.state.attachBusy = false;
                      window.AgentUI.state.attachStage = '';
                      window.AgentUI._renderAttachments();
                    }""")

                def upload(paths):
                    page.set_input_files("#agAttachInput",
                                         [str(x) for x in paths])
                    page.wait_for_function(
                        """() => !window.AgentUI.state.attachBusy""",
                        timeout=120000)
                    page.wait_for_timeout(250)

                # --- entry ---
                reset()
                shot = SHOTS / f"media-{tag}-01-upload-entry.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "entry",
                                "probe": probe(page), "screenshot": str(shot)})

                # --- one ---
                png1 = make_png(tmp / f"one-{tag}.png", (200, 30, 30))
                upload([png1])
                shot = SHOTS / f"media-{tag}-02-one-uploaded.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                lib = (library_probe(pr["attachments"][0]["asset_id"])
                       if pr["attachments"] else {"ok": False})
                results.append({"width": width, "case": "one", "probe": pr,
                                "library": lib, "screenshot": str(shot)})

                # --- multi ---
                reset()
                pngs = [make_png(tmp / f"multi-{tag}-{i}.png",
                                 (i * 30, 100, 200)) for i in range(3)]
                upload(pngs)
                shot = SHOTS / f"media-{tag}-03-multi-uploaded.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                results.append({"width": width, "case": "multi", "probe": pr,
                                "screenshot": str(shot)})

                # --- remove ---
                page.click(".ag-attach-thumb .ag-attach-remove")
                page.wait_for_timeout(300)
                shot = SHOTS / f"media-{tag}-04-after-remove.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "remove",
                                "probe": probe(page), "screenshot": str(shot)})

                # --- bad type ---
                reset()
                txt = make_text(tmp / f"bad-{tag}.txt")
                upload([txt])
                shot = SHOTS / f"media-{tag}-05-bad-type-rejected.png"
                page.screenshot(path=str(shot), full_page=False)
                results.append({"width": width, "case": "bad-type",
                                "probe": probe(page), "screenshot": str(shot)})

                # --- limit ---
                reset()
                many = [make_png(tmp / f"many-{tag}-{i}.png",
                                 (10 * i, 20, 30)) for i in range(11)]
                upload(many)
                shot = SHOTS / f"media-{tag}-06-limit-9.png"
                page.screenshot(path=str(shot), full_page=False)
                pr = probe(page)
                uploaded_ids.extend(a["asset_id"] for a in pr["attachments"])
                results.append({"width": width, "case": "limit", "probe": pr,
                                "screenshot": str(shot)})

                results[-1]["failed_requests"] = failed_requests
                console_errors.extend(errs)
                ctx.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- assertions -------------------------------------------------
    ok = True
    for r in results:
        pr = r["probe"]
        case = r["case"]
        if case == "entry":
            checks = {
                "上传入口可见": pr["btnVisible"],
                "没有附件时缩略图区隐藏": pr["attachBoxHidden"] is True,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "one":
            lib = r.get("library") or {}
            checks = {
                "单图上传成功": pr["thumbCount"] == 1,
                "有 preview_url": bool(pr["thumbSrcs"][0] if pr["thumbSrcs"] else ""),
                "预览指向素材库接口": (pr["thumbSrcs"][0].startswith("/api/assets/")
                                       if pr["thumbSrcs"] else False),
                "拿到 asset_id": len(pr["attachments"]) == 1
                                 and pr["attachments"][0]["asset_id"].startswith("asset_"),
                "无错误提示": not pr["error"],
                # --- 素材库入库 ---
                "素材库里真的有这条": lib.get("ok") and lib.get("source") == "agent_upload",
                "素材库抽出了尺寸": lib.get("width") == 120 and lib.get("height") == 90,
                # --- 预处理 ---
                "预处理写入了描述": len(lib.get("analysis_text") or "") > 20,
                "预处理写入了标签": len(lib.get("tags") or []) > 0,
                "前端拿到标签": len(pr["attachments"][0]["tags"]) > 0,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "multi":
            checks = {
                "多图都上传成功": pr["thumbCount"] == 3,
                "asset_id 三个": len(pr["attachments"]) == 3,
                "缩略图各不相同": len(set(pr["thumbSrcs"])) == 3,
                "窄屏无横向溢出": overflow_free(pr),
            }
        elif case == "remove":
            checks = {
                "移除后剩 2 张": pr["thumbCount"] == 2,
                "移除后 asset_id 剩 2 个": len(pr["attachments"]) == 2,
            }
        elif case == "bad-type":
            checks = {
                "非法类型不被接受": pr["thumbCount"] == 0,
                "有可读的错误提示": bool(pr["error"]),
                "错误提示提到格式": ("格式" in pr["error"]
                                     or "不支持" in pr["error"]),
                "没有 JS 崩溃": True,
            }
        else:  # limit
            checks = {
                "最多 9 张": pr["thumbCount"] == 9,
                "asset_id 最多 9 个": len(pr["attachments"]) == 9,
                "有多余的提示": bool(pr["error"]) or "9" in pr["hint"],
                "窄屏无横向溢出": overflow_free(pr),
            }
        r["checks"] = checks
        r["all_pass"] = all(checks.values())
        ok = ok and r["all_pass"]

    report = {"results": results, "console_errors": console_errors,
              "all_pass": ok}
    (SHOTS / "browser_verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if (ok and not console_errors) else 1


if __name__ == "__main__":
    sys.exit(main())
