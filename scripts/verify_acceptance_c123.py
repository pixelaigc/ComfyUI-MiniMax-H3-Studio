#!/usr/bin/env python3
"""c1 / c2 / c3 验收取证：真实独立端口 server + 真实 Chromium + 真实前端请求。

【c1 流式载体】
  真实前端请求形态打真实 server，断言响应 Content-Type 含 text/event-stream。
  两种载体都打：body {"stream":1} 与 query ?stream=1。

【c2 澄清快捷选项】
  needs_clarification 时，界面上真的出现「可点击」的快捷选项 chip
  （.ag-clarify-opt），且点击其中一项能把回答送出去。

【c3 工具卡】
  渲染后：工具卡显示**参数**与**结果摘要**；
  失败的工具显示**错误文案**；
  **不得**出现空结果区块。

【只 mock 什么】
  只 mock 后端**模型推理**那一步（mock OpenAI 兼容端点）。
  其余全是真代码路径：真实 server.py、真实 h3studio/agent.py 编排、
  真实 SSE 事件流、真实 web/ 前端渲染、真实 SQLite 落库。

【端口纪律】
  独立端口（默认 8197）+ 独立 data_dir + 独立 mock 端口（8321）。
  **不碰 8190 线上服务，不碰任何 systemd unit。**

用法：
    python3 scripts/verify_acceptance_c123.py [证据输出目录]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-acceptance-c123")
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/agent-enh-merge")
STUDIO_PORT = int(os.environ.get("H3_TEST_PORT", "8197"))
MOCK_PORT = int(os.environ.get("H3_MOCK_PORT", "8321"))
VIEWPORTS = [(375, 812), (390, 844), (1280, 900)]

CAPTURED: list[dict] = []
CAPTURE_LOCK = threading.Lock()

# ---------------------------------------------------------------- mock LLM

SCENARIOS: dict[str, list[dict]] = {
    # 澄清场景：respond 带 clarify_options / clarify_hint
    "帮我做个视频": [
        {"__delay_ms": 200, "content": None, "tool_calls": [{
            "id": "call_respond_1", "type": "function",
            "function": {"name": "respond", "arguments": json.dumps({
                "summary": "你要生成视频的话，需要确认画面比例：想要竖屏还是横屏？",
                "needs_clarification": True,
                "clarify_options": ["9:16 竖屏", "16:9 横屏", "1:1 方形"],
                "clarify_hint": "视频时长合法区间是 5~15 秒；比例我建议 16:9。",
            }, ensure_ascii=False)},
        }]},
        {"__delay_ms": 200, "content": "等你确认比例。"},
    ],
    # 工具链场景：create_job 成功 + 一个会失败的工具（未知工具）
    "生成一张雨夜霓虹街道的图": [
        {"__delay_ms": 200, "content": None, "tool_calls": [{
            "id": "call_create_1", "type": "function",
            "function": {"name": "create_job", "arguments": json.dumps({
                "mode": "t2i",
                "prompt": "雨夜霓虹街道，积水反射粉紫色招牌光晕，电影级广角构图",
                "aspect": "16:9", "resolution": "1mp",
            }, ensure_ascii=False)},
        }]},
        {"__delay_ms": 150, "content": "已经按你的描述下发了 "},
        {"__delay_ms": 150, "content": "1 个文生图任务，16:9 / 1MP。"},
    ],
    # 失败工具场景：调用一个**不存在的工具** —— 后端走 spec is None 分支，
    # result = {"error": "未知工具：xxx"}，ok=False。
    # 用于验证「失败的工具卡显示错误文案，且不出现空结果区块」。
    "触发一个不存在的工具": [
        {"__delay_ms": 200, "content": None, "tool_calls": [{
            "id": "call_bad_1", "type": "function",
            "function": {"name": "definitely_not_a_tool", "arguments": "{}"},
        }]},
        {"__delay_ms": 150, "content": "工具调用失败了。"},
    ],
}


def scenario_of(text: str) -> str:
    for key in SCENARIOS:
        if key in (text or ""):
            return key
    return ""


class MockLLM(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or "{}")
        except Exception:
            payload = {}
        messages = payload.get("messages") or []
        users = [str(m.get("content") or "") for m in messages
                 if m.get("role") == "user"]
        scenario = scenario_of(users[-1]) if users else ""
        with CAPTURE_LOCK:
            CAPTURED.append({
                "scenario": scenario,
                "messages": [{"role": m.get("role"),
                              "content": str(m.get("content") or "")}
                             for m in messages],
            })
        turns = SCENARIOS.get(scenario) or []
        turn_index = sum(1 for m in messages if m.get("role") == "assistant")
        turn = turns[turn_index] if turn_index < len(turns) else {
            "content": "（结束）", "tool_calls": []}
        delay = int(turn.get("__delay_ms") or 0)
        if delay:
            time.sleep(delay / 1000.0)
        calls = list(turn.get("tool_calls") or [])
        message = {"role": "assistant", "content": turn.get("content")}
        if calls:
            message["tool_calls"] = calls
            message["content"] = turn.get("content") or None
        body = json.dumps({
            "id": "mock", "object": "chat.completion", "model": "mock",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------- 独立实例

def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到空闲端口")


class Studio:
    def __init__(self, port: int, mock_port: int):
        self.port = port
        self.mock_port = mock_port
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-accept-c123-"))
        self.data = self.tmp / "data"
        self.proc = None
        self.base = f"http://127.0.0.1:{port}"

    def _seed(self):
        self.data.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.data / "jobs.db")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
                status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                started_at REAL, finished_at REAL);
            CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
                seed INTEGER, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                created_at REAL, started_at REAL, finished_at REAL);
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
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO model_providers VALUES "
            "('mockprov','Mock 多模态','openai',?,'',0,'','mock-model','',1,1,?)",
            (f"http://127.0.0.1:{self.mock_port}", now))
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default','mockprov')")
        conn.commit()
        conn.close()

    def start(self) -> bool:
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        (self.tmp / "config.json").write_text(json.dumps({
            "host": "127.0.0.1", "port": self.port,
            "comfyui_url": "http://127.0.0.1:8188",
            "data_dir": str(self.data),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }, ensure_ascii=False), encoding="utf-8")
        self._seed()
        env = dict(os.environ)
        env["H3STUDIO_DATA_DIR"] = str(self.data)
        env["AGENT_SESSION_DB"] = str(self.data / "agent_sessions.db")
        env["AGENT_MEMORY_DB"] = str(self.data / "agent_memory.db")
        env["PYTHONPATH"] = str(self.tmp)
        self.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=str(self.tmp), env=env,
            stdout=open(OUT_DIR / "server_stdout.log", "w", encoding="utf-8"),
            stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 1):
                    return True
            except OSError:
                time.sleep(0.3)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- c1 直连

def check_c1(studio: Studio) -> dict:
    """用**前端真实请求形态**打**真实线上实例**，断言 Content-Type。"""
    out: dict = {}
    for label, path, payload in [
        ("body 载体（前端现行形态）", "/api/agent/chat",
         {"message": "你好", "stream": 1}),
        ("query 载体（旧前端形态）", "/api/agent/chat?stream=1",
         {"message": "你好"}),
    ]:
        req = urllib.request.Request(
            studio.base + path, data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type": "application/json",
                                    "Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=60) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read().decode("utf-8", "replace")
        events = [ln for ln in raw.splitlines() if ln.startswith("event:")]
        out[label] = {
            "url": path, "request_body": payload,
            "status": 200, "content_type": ctype,
            "has_event_stream": "text/event-stream" in ctype,
            "sse_event_count": len(events),
            "sse_events": events[:8],
            "raw_head": raw[:300],
        }
        print(f"  c1 [{label}] Content-Type={ctype!r} "
              f"events={len(events)}", flush=True)
    return out


# ---------------------------------------------------------------- 浏览器

def run_browser(studio: Studio) -> dict:
    from playwright.sync_api import sync_playwright

    results: dict = {"viewports": {}, "console": [], "pageErrors": [],
                     "requests": [], "responses": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for width, height in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": width, "height": height})
            page = ctx.new_page()
            page.on("console", lambda m, w=width: results["console"].append(
                {"width": w, "type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e, w=width: results["pageErrors"].append(
                {"width": w, "error": str(e)[:300]}))
            page.on("request", lambda r, w=width: results["requests"].append(
                {"width": w, "method": r.method, "url": r.url,
                 "post_data": (r.post_data or "")[:400]}))
            page.on("response", lambda r, w=width: results["responses"].append(
                {"width": w, "status": r.status, "url": r.url,
                 "content_type": r.headers.get("content-type", "")}))

            page.goto(studio.base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            page.evaluate("() => window.AgentUI && window.AgentUI.show()")
            page.wait_for_timeout(600)

            def new_session():
                page.evaluate("""() => {
                  if (window.AgentSessions && window.AgentSessions.createSession) {
                    window.AgentSessions.createSession();
                  }
                }""")
                try:
                    page.wait_for_function(
                        "() => { const s = window.AgentSessions && "
                        "window.AgentSessions.state; return !!(s && s.currentId); }",
                        timeout=15000)
                except Exception:
                    pass
                page.evaluate("""() => {
                  const s = window.AgentUI.state;
                  s.messages = []; s.lastResult = null; s.streaming = null;
                  window.AgentUI.renderMessages();
                }""")
                page.wait_for_timeout(400)

            vp: dict = {}

            # ---------- c2：澄清快捷选项 ----------
            new_session()
            page.fill("#agInput", "帮我做个视频")
            page.click("#agSend")
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && last.clarify && last.clarify.options
                              && last.clarify.options.length);
                }""", timeout=60000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy", timeout=60000)
            page.wait_for_timeout(900)
            vp["c2_clarify"] = page.evaluate("""() => {
              const s = window.AgentUI.state;
              const last = s.messages[s.messages.length - 1] || {};
              const chips = Array.from(document.querySelectorAll('.ag-clarify-opt'));
              return {
                stateOptions: (last.clarify || {}).options || [],
                stateQuestion: (last.clarify || {}).question || '',
                stateHint: (last.clarify || {}).hint || '',
                domChipCount: chips.length,
                domChipTexts: chips.map(c => (c.textContent||'').trim()),
                domChipClickable: chips.every(c =>
                  c.tagName === 'BUTTON' || c.tagName === 'A' ||
                  !!c.getAttribute('data-act')),
                domChipDataAct: chips.map(c => c.getAttribute('data-act')),
                hasBubble: !!document.querySelector('.ag-clarify'),
                hasInput: !!document.querySelector('.ag-clarify-input'),
                hasSend: !!document.querySelector('.ag-clarify-send'),
              };
            }""")
            shot = SHOTS / f"acc-c2-clarify-{width}.png"
            page.screenshot(path=str(shot), full_page=True)
            vp["c2_shot"] = str(shot)

            # 点第一个 chip，验证真的能送出回答
            chip = page.query_selector(".ag-clarify-opt")
            if chip:
                before_n = page.evaluate(
                    "() => window.AgentUI.state.messages.length")
                page.evaluate("""() => {
                  const c = document.querySelector('.ag-clarify-opt');
                  if (c) c.click();
                }""")
                page.wait_for_timeout(1500)
                vp["c2_click_result"] = {
                    "messages_before": before_n,
                    "messages_after": page.evaluate(
                        "() => window.AgentUI.state.messages.length"),
                    "input_value_after_click": page.evaluate(
                        "() => (document.querySelector('.ag-clarify-input')||{}).value || ''"),
                }

            # ---------- c3：工具卡参数 + 结果摘要 ----------
            new_session()
            page.fill("#agInput", "生成一张雨夜霓虹街道的图")
            page.click("#agSend")
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && last.jobs && last.jobs.length);
                }""", timeout=90000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy", timeout=90000)
            page.wait_for_timeout(1500)
            page.evaluate("""() => {
              document.querySelectorAll('.ag-tool').forEach(d => { d.open = true; });
            }""")
            page.wait_for_timeout(500)
            vp["c3_toolcard"] = page.evaluate("""() => {
              const tools = Array.from(document.querySelectorAll('.ag-tool'));
              const parsed = tools.map(t => ({
                name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
                argsText: (t.querySelector('.ag-tool-args code')||{}).textContent || '',
                resultText: (t.querySelector('.ag-tool-result code')||{}).textContent || '',
                errText: (t.querySelector('.ag-tool-err')||{}).textContent || '',
                stateText: (t.querySelector('.ag-tool-state')||{}).textContent || '',
              }));
              // 「空结果区块」判定：结果容器存在但内容为空/只有 null
              const emptyBlocks = tools.filter(t => {
                const box = t.querySelector('.ag-tool-result');
                if (!box) return false;
                const txt = (box.textContent||'').trim();
                return txt === '' || txt === 'null' || txt === 'undefined';
              }).length;
              return {
                toolCount: tools.length,
                tools: parsed,
                toolsWithArgs: parsed.filter(t => t.argsText.trim().length > 0).length,
                toolsWithResult: parsed.filter(t => t.resultText.trim().length > 0).length,
                toolsWithErrorText: parsed.filter(t => t.errText.trim().length > 0).length,
                emptyResultBlocks: emptyBlocks,
                rawResultText: Array.from(
                  document.querySelectorAll('.ag-tool-result')).map(
                    b => (b.textContent||'').trim().slice(0,120)),
              };
            }""")
            shot = SHOTS / f"acc-c3-toolcard-{width}.png"
            page.screenshot(path=str(shot), full_page=True)
            vp["c3_shot"] = str(shot)

            # ---------- c3-b：失败的工具卡（错误文案 + 无空结果区块）----------
            new_session()
            page.fill("#agInput", "触发一个不存在的工具")
            page.click("#agSend")
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && (last.tools || []).length);
                }""", timeout=60000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy", timeout=60000)
            page.wait_for_timeout(1200)
            page.evaluate("""() => {
              document.querySelectorAll('.ag-tool').forEach(d => { d.open = true; });
            }""")
            page.wait_for_timeout(500)
            vp["c3_failed_tool"] = page.evaluate("""() => {
              const tools = Array.from(document.querySelectorAll('.ag-tool'));
              const parsed = tools.map(t => ({
                name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
                argsText: (t.querySelector('.ag-tool-args code')||{}).textContent || '',
                resultText: (t.querySelector('.ag-tool-result code')||{}).textContent || '',
                errText: (t.querySelector('.ag-tool-err')||{}).textContent || '',
                stateText: (t.querySelector('.ag-tool-state')||{}).textContent || '',
              }));
              const emptyBlocks = tools.filter(t => {
                const box = t.querySelector('.ag-tool-result');
                if (!box) return false;
                const txt = (box.textContent||'').trim();
                return txt === '' || txt === 'null' || txt === 'undefined';
              }).length;
              return {
                toolCount: tools.length,
                tools: parsed,
                toolsWithErrorText: parsed.filter(t =>
                  t.resultText.includes('未知工具') ||
                  t.errText.trim().length > 0).length,
                emptyResultBlocks: emptyBlocks,
              };
            }""")
            shot = SHOTS / f"acc-c3-failed-tool-{width}.png"
            page.screenshot(path=str(shot), full_page=True)
            vp["c3_failed_shot"] = str(shot)

            results["viewports"][str(width)] = vp
            ctx.close()
        browser.close()
    return results


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    print("== 起 mock LLM ==")
    mock_port = free_port(MOCK_PORT)
    mock_server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockLLM)
    mock_server.daemon_threads = True
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()
    print(f"   mock LLM: http://127.0.0.1:{mock_port}")

    print("== 起独立 Studio（独立端口 + 独立 data_dir）==")
    port = free_port(STUDIO_PORT)
    studio = Studio(port, mock_port)
    if not studio.start():
        print("!! Studio 启动失败")
        studio.stop()
        return 1
    print(f"   Studio: {studio.base}（8190 线上**未触碰**）")

    evidence: dict = {"studio_base": studio.base, "mock_port": mock_port}
    try:
        print("\n== c1 流式载体（真实前端请求形态 -> 真实 server）==")
        evidence["c1"] = check_c1(studio)

        print("\n== c2 / c3 真实浏览器 ==")
        evidence["browser"] = run_browser(studio)
    finally:
        studio.stop()
        try:
            mock_server.shutdown()
        except Exception:
            pass

    # ---------------- 判定 ----------------
    c1 = evidence.get("c1", {})
    c1_ok = all(v["has_event_stream"] and v["sse_event_count"] > 0
                for v in c1.values())
    vp = evidence["browser"]["viewports"]
    c2_ok = all(
        (v["c2_clarify"]["domChipCount"] >= 3)
        and v["c2_clarify"]["domChipClickable"]
        and len(v["c2_clarify"]["stateOptions"]) >= 3
        for v in vp.values())
    c2_click_ok = all(
        v.get("c2_click_result", {}).get("input_value_after_click", "") != ""
        for v in vp.values())
    c3_ok = all(
        v["c3_toolcard"]["toolCount"] >= 1
        and v["c3_toolcard"]["toolsWithArgs"] >= 1
        and v["c3_toolcard"]["toolsWithResult"] >= 1
        and v["c3_toolcard"]["emptyResultBlocks"] == 0
        for v in vp.values())
    # 失败工具：错误文案可见 + 同样没有空结果区块
    c3_failed_ok = all(
        v["c3_failed_tool"]["toolCount"] >= 1
        and v["c3_failed_tool"]["toolsWithErrorText"] >= 1
        and v["c3_failed_tool"]["emptyResultBlocks"] == 0
        for v in vp.values())
    console_errors = [c for c in evidence["browser"]["console"]
                      if c["type"] == "error"]
    evidence["verdict"] = {
        "c1_sse_content_type_all_carriers": c1_ok,
        "c2_clarify_clickable_options": c2_ok,
        "c2_chip_click_fills_input": c2_click_ok,
        "c3_toolcard_args_and_result": c3_ok,
        "c3_failed_tool_shows_error_text": c3_failed_ok,
        "console_errors": len(console_errors),
        "page_errors": len(evidence["browser"]["pageErrors"]),
        "console_error_samples": console_errors[:5],
    }
    (OUT_DIR / "acceptance_c123_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== c1/c2/c3 判定 =====")
    print(json.dumps(evidence["verdict"], ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    ok = (c1_ok and c2_ok and c2_click_ok and c3_ok and c3_failed_ok
          and not console_errors and not evidence["browser"]["pageErrors"])
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
