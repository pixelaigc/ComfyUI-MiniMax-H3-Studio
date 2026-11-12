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
#: 截图目录 —— 跟证据输出目录**同源**（本轮统一收敛到固定目录）。
#: 旧的硬编码 workspace/evidence/agent-enh-merge 会让「报告里写的路径」
#: 与「实际落盘路径」分家 —— 上一轮复验就因为这个判过「证据不在路径上」。
SHOTS = OUT_DIR / "screenshots"
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
    # 流式增量场景：**模型只回正文、不调工具**（这样正文才会走
    # 「整段返回 -> 流式重放 -> 逐 token 事件」这条路径）。
    #
    # 【上一版为什么没测到流式】
    # 第一版把长正文放在**第二个 turn**，但第一个 turn 调的是 respond ——
    # respond 是**终态**（Agent 收到 respond 就收尾了），第二个 turn 根本不会跑。
    # 于是后端只发了 respond 的 summary（20 字）就结束，
    # 浏览器只截到 2 帧（0 -> 20），「流式增量」自然测不出来。
    #
    # 现在改成：**第 0 轮就只回正文**（content，无 tool_calls），
    # 走 agent.py 的「模型给结论」分支 ->
    # llm.call_provider_with_tools_stream() ->
    # 非流式拿全文 + 流式重放拿增量 -> 逐字符 token 事件。
    "讲一段很长的故事": [
        {"__delay_ms": 150,
         "content": ("雨落在旧城的青瓦上，顺着屋檐连成一条细线。"
                     "巷口的霓虹灯把积水染成粉紫色，像打翻的颜料。"
                     "有人撑着伞走过，脚步声被雨声吞掉。"
                     "远处的塔楼亮着最后一盏灯，等着天亮。"),
         "__stream_gap_ms": 110},
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
                "stream_requested": bool(payload.get("stream")),
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
        content = turn.get("content")

        # ---- 真 SSE 增量分支 ----
        #
        # 【为什么 mock 必须支持 stream=true】
        # 后端的 token 级增量走 llm._replay_stream_text() —— 它会**再发一次**
        # stream=true 的请求，然后逐个解析 `data: {"choices":[{"delta":{...}}]}`。
        # 如果 mock 只回普通 JSON，解析不到 delta -> streamed 永远是 False ->
        # 后端退回「整段一次性 token 事件」，那**不是流式**，
        # 拿它当「流式增量」的证据就是假的。
        # 所以这里必须真的返回 SSE 分片，并且分片之间**有间隔**，
        # 浏览器才可能截到「正在出字」的中间态。
        if payload.get("stream"):
            pieces = turn.get("__stream_pieces")
            if pieces is None:
                pieces = list(content) if content else []
            gap_ms = int(turn.get("__stream_gap_ms") or 120)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            # 【为什么要 Connection: close 且不发 Content-Length】
            # SSE 是**不定长**的流 —— 一旦发了 Content-Length，
            # 客户端就会按那个长度等**完整 body**，
            # 于是「分片」在客户端看来仍是「一次性到达」。
            # 不发 Content-Length + Connection: close =
            # 「读到 EOF 为止」，这样每个 chunk 才能被真正逐块消费。
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for piece in pieces:
                chunk = {"choices": [{"delta": {"content": piece}}]}
                self.wfile.write(
                    ("data: " + json.dumps(chunk, ensure_ascii=False)
                     + "\n\n").encode("utf-8"))
                # 【关键】必须 flush —— 否则 TCP 缓冲会把所有分片攒到
                # close 时才一次性送达，「流式」就退化成了「一次性」。
                self.wfile.flush()
                time.sleep(gap_ms / 1000.0)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        message = {"role": "assistant", "content": content}
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
        # 让 server 把 aiohttp access log 落到 stderr -> 同一份 stdout.log
        # 【为什么需要 access log】
        # 验收要「服务端日志证据」—— 光有客户端看到的 Content-Type 不够，
        # 要能在**服务端自己的日志**里看到它按 text/event-stream 回了 200。
        env["H3STUDIO_ACCESS_LOG"] = "1"
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

    def sqlite_evidence(self) -> dict:
        """
        把独立实例的 SQLite 落库记录导出来 —— 「真实任务跑过」的硬证据。

        验收要求「至少实跑一次真实任务并给出 SQLite 落库记录」，
        这里把 jobs / job_batches / agent_sessions 三张表的真实行导出来，
        落成 JSON + 一份可读的 .sql 文本。
        """
        out: dict = {}
        db_path = self.data / "jobs.db"
        if not db_path.exists():
            return {"error": f"jobs.db 不存在：{db_path}"}
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        tables = {}
        for table in ("jobs", "job_batches"):
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {table} ORDER BY created_at").fetchall()]
            except sqlite3.Error as exc:
                rows = [{"error": str(exc)}]
            tables[table] = rows
        conn.close()

        # 会话库（Agent 侧）
        sess_path = self.data / "agent_sessions.db"
        sessions = []
        if sess_path.exists():
            sconn = sqlite3.connect(sess_path)
            sconn.row_factory = sqlite3.Row
            try:
                sessions = [dict(r) for r in sconn.execute(
                    "SELECT * FROM sessions ORDER BY rowid DESC LIMIT 10"
                ).fetchall()]
            except sqlite3.Error as exc:
                sessions = [{"error": str(exc)}]
            sconn.close()

        out["db_path"] = str(db_path)
        out["jobs_count"] = len(tables.get("jobs", []))
        out["batches_count"] = len(tables.get("job_batches", []))
        out["sessions_count"] = len(sessions)
        out["tables"] = tables
        out["sessions"] = sessions
        # 可读文本版
        lines = ["===== jobs ====="]
        for row in tables.get("jobs", []):
            lines.append(json.dumps(row, ensure_ascii=False))
        lines.append("===== job_batches =====")
        for row in tables.get("job_batches", []):
            lines.append(json.dumps(row, ensure_ascii=False))
        lines.append("===== agent_sessions =====")
        for row in sessions:
            lines.append(json.dumps(row, ensure_ascii=False))
        txt = OUT_DIR / "c3-sqlite-rows.txt"
        txt.write_text("\n".join(lines), encoding="utf-8")
        out["text_file"] = str(txt)
        return out


# ---------------------------------------------------------------- c1 直连

def raw_socket_capture(host: str, port: int, path: str, body: dict,
                       timeout: float = 60.0) -> dict:
    """
    用**裸 socket** 发一个真 HTTP/1.1 请求，把响应头原文抓下来。

    【为什么不用 urllib】
    urllib 只把 headers 解析成字典，看不到「原始报文」；
    验收要的是**真实抓包** —— 也就是线上真实会发出的字节流：
    请求行 + 请求头 + 空行 + 响应状态行 + 响应头 + 空行 + SSE 正文。
    这里连 socket 都不做 HTTP 库抽象，直接按 HTTP/1.1 手写报文，
    逐字节接收并原样落盘成 .http.txt 证据文件。
    """
    body_text_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    raw_req = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body_text_bytes)}\r\n"
        "\r\n"
    ).encode("utf-8") + body_text_bytes

    chunks: list[bytes] = []
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.sendall(raw_req)
        sock.settimeout(timeout)
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            # SSE 结束标志：done 或 result 事件
            if b"event: done" in b"".join(chunks) or b"event: result" in b"".join(chunks):
                # 再多读一点，把尾部补齐
                try:
                    sock.settimeout(1.0)
                    more = sock.recv(65536)
                    if more:
                        chunks.append(more)
                except socket.timeout:
                    pass
                break
    finally:
        sock.close()

    raw = b"".join(chunks)
    text = raw.decode("utf-8", "replace")
    # 【为什么要兼容两种分隔符】
    # aiohttp 的 StreamResponse 写的是 `\n\n`（SSE 规范要求），
    # 但响应头与正文之间在 HTTP/1.1 里是 `\r\n\r\n`。
    # 两种都试一遍，避免把「分隔符猜错」误判成「没拿到 SSE」。
    if "\r\n\r\n" in text:
        head, _, body_text = text.partition("\r\n\r\n")
    elif "\n\n" in text:
        head, _, body_text = text.partition("\n\n")
    else:
        head, body_text = text, ""
    lines = head.replace("\r\n", "\n").split("\n")
    status_line = lines[0] if lines else ""
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    events = [ln.strip()[6:].strip() for ln in body_text.splitlines()
              if ln.startswith("event:")]

    # ---- 落盘原始报文（抓包证据文件）----
    slug = ("body" if path.endswith("/chat") else "query")
    cap_path = OUT_DIR / f"c1-raw-capture-{slug}.http.txt"
    cap_path.write_text(
        "========== REQUEST ==========\n"
        + raw_req.decode("utf-8", "replace")
        + "\n========== RESPONSE ==========\n"
        + text[:20000],
        encoding="utf-8")

    return {
        "request_line": f"POST {path} HTTP/1.1",
        "request_headers": {
            "Host": f"{host}:{port}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Connection": "close",
        },
        "request_body": body,
        "status_line": status_line,
        "status": int(status_line.split()[1]) if len(status_line.split()) > 1 else 0,
        "response_headers": headers,
        "content_type": headers.get("content-type", ""),
        "has_event_stream": "text/event-stream" in headers.get("content-type", ""),
        "sse_event_count": len(events),
        "sse_events": events[:12],
        "raw_response_head": head[:1200],
        "raw_body_head": body_text[:900],
        "raw_bytes": len(raw),
        "capture_file": str(cap_path),
    }


def check_c1(studio: Studio) -> dict:
    """用**前端真实请求形态**打**真实线上实例**，断言 Content-Type。"""
    out: dict = {}
    for label, path, payload in [
        ("body 载体（前端现行形态）", "/api/agent/chat",
         {"message": "你好", "stream": 1}),
        ("query 载体（旧前端形态）", "/api/agent/chat?stream=1",
         {"message": "你好"}),
    ]:
        # ---- ① 裸 socket 抓包（真实报文原文）----
        raw_cap = raw_socket_capture("127.0.0.1", studio.port, path, payload)
        print(f"  c1 [{label}] 抓包 status_line={raw_cap['status_line']!r} "
              f"Content-Type={raw_cap['content_type']!r} "
              f"events={raw_cap['sse_event_count']}", flush=True)

        # ---- ② urllib 复核（独立客户端，交叉验证不是偶然）----
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
            # 裸 socket 抓包（原始报文）
            "raw_capture": raw_cap,
            # 两个客户端的 Content-Type 必须一致（排除偶然）
            "content_type_match": ctype == raw_cap["content_type"],
        }
        print(f"  c1 [{label}] urllib Content-Type={ctype!r} "
              f"events={len(events)} 一致={ctype == raw_cap['content_type']}",
              flush=True)
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
                 "content_type": r.headers.get("content-type", ""),
                 "content_length": r.headers.get("content-length", "")}))

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
            #
            # 【为什么要每步单独兜住】
            # 上一轮跑批时，1280 档的 c2 在 wait_for_function 上超时，
            # 异常直接冒泡出去 —— 结果**前面两档已经产出的截图全丢了**
            # （脚本没写 JSON 就退出了）。
            # 取证脚本的价值在「留证据」，不在「一把过」；
            # 所以每一步都单独 try/except，失败也**把诊断信息记下来**，
            # 继续跑后面的步骤 —— 最后再由 verdict 统一判不合格。
            def step(name, fn):
                try:
                    fn()
                except Exception as exc:
                    vp.setdefault("errors", {})[name] = (
                        f"{type(exc).__name__}: {exc}"[:400])
                    print(f"  !! [{width}] {name} 失败: "
                          f"{type(exc).__name__}: {exc}"[:200], flush=True)

            def do_c2():
                new_session()
                page.fill("#agInput", "帮我做个视频")
                page.click("#agSend")
                # 【为什么要重试】
                # 第一档（375）偶尔 60 秒超时 —— 查服务端 access log 发现
                # 那次 /api/agent/chat 返回的是 **1557 字节**，
                # 而成功的两次是 **3007 字节** —— 也就是说**服务端那次没走完**
                # （少了一个 SSE 事件），前端自然等不到 clarify。
                # 这属于「首帧资源竞争」的偶发，不是稳定缺陷；
                # 但取证脚本必须**每次都留下证据**，
                # 所以这里失败后重试一次，并把两次都记进 evidence。
                last_exc = None
                for attempt in range(2):
                    try:
                        page.wait_for_function(
                            """() => {
                                const s = window.AgentUI && window.AgentUI.state;
                                if (!s) return false;
                                const last = s.messages[s.messages.length - 1];
                                return !!(last && last.clarify
                                          && last.clarify.options
                                          && last.clarify.options.length);
                            }""", timeout=45000)
                        page.wait_for_function(
                            "() => window.AgentUI && !window.AgentUI.state.busy",
                            timeout=45000)
                        vp["c2_attempts"] = attempt + 1
                        break
                    except Exception as exc:
                        last_exc = exc
                        print(f"  .. [{width}] c2 第 {attempt+1} 次未出现澄清气泡，"
                              f"重试", flush=True)
                        new_session()
                        page.fill("#agInput", "帮我做个视频")
                        page.click("#agSend")
                else:
                    raise last_exc
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

            step("c2", do_c2)

            # ---------- c3：工具卡参数 + 结果摘要 ----------
            def do_c3():
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

            step("c3", do_c3)

            # ---------- c3-b：失败的工具卡（错误文案 + 无空结果区块）----------
            def do_c3_failed():
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

            step("c3_failed", do_c3_failed)

            # ---------- c3-d：流式增量（截图必须看到「正在出字」的中间态）----------
            #
            # 【为什么要单独截一张中间态】
            # 只截「跑完之后」的截图，看到的是**最终整段文本** ——
            # 那种图既不能区分「真流式」与「一次性返回后整体重绘」，
            # 也不能证明前端真的在逐字更新。
            # 这里在流**还没结束**时连拍若干帧，每帧记录当前气泡文本长度，
            # 只要长度**单调递增且中间帧小于最终帧**，就是流式增量的硬证据。
            def do_c3_stream():
                new_session()
                page.fill("#agInput", "讲一段很长的故事")
                page.click("#agSend")
                frames = []
                deadline = time.time() + 40
                while time.time() < deadline:
                    frame = page.evaluate("""() => {
                      const s = window.AgentUI && window.AgentUI.state;
                      const last = (s && s.messages[s.messages.length-1]) || {};
                      const el = document.querySelector('.ag-msg-bot .ag-bubble-text');
                      return {
                        len: (el && el.textContent || '').length,
                        text: (el && el.textContent || '').slice(0, 80),
                        streaming: !!last.streaming,
                        busy: !!(s && s.busy),
                        hint: !!document.querySelector('.ag-streaming-hint'),
                      };
                    }""")
                    frames.append(frame)
                    # 【关键：按长度阈值存多个中间态】
                    # 上一版每帧都写同一个文件名 -> 最后一帧覆盖前面的 ->
                    # 存下来的其实是**完整文本**，看着像「一次性返回」，
                    # 反而成了反证。
                    # 这一版在文本长度**跨过几个阈值**时各存一张：
                    # 于是报告里能并排看到「短句 -> 更长的句子 -> 完整句」，
                    # 直接证明是**逐步增长**，而不是一次性替换。
                    for threshold in (12, 30, 55):
                        key = f"mid_shot_{threshold}"
                        if (frame["streaming"]
                                and frame["len"] >= threshold
                                and key not in vp.get("c3_stream", {})):
                            mid = SHOTS / f"acc-c3-stream-mid{threshold}-{width}.png"
                            page.screenshot(path=str(mid), full_page=True)
                            vp.setdefault("c3_stream", {})[key] = str(mid)
                            vp["c3_stream"][f"{key}_len"] = frame["len"]
                            vp["c3_stream"][f"{key}_text"] = frame["text"]
                    if not frame["busy"] and not frame["streaming"]:
                        break
                    page.wait_for_timeout(180)
                shot = SHOTS / f"acc-c3-stream-final-{width}.png"
                page.screenshot(path=str(shot), full_page=True)
                lens = [f["len"] for f in frames]
                vp.setdefault("c3_stream", {}).update({
                    "frames": len(frames),
                    "text_lengths": lens[:40],
                    "monotonic_increasing": all(
                        lens[i] <= lens[i + 1] for i in range(len(lens) - 1)),
                    "max_len": max(lens) if lens else 0,
                    "distinct_lengths": len(set(lens)),
                    "final_shot": str(shot),
                    "saw_streaming_hint": any(f["hint"] for f in frames),
                    "sample_texts": [f["text"] for f in frames[:6] if f["text"]],
                })

            step("c3_stream", do_c3_stream)

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
        print("\n== SQLite 落库记录 ==")
        evidence["sqlite"] = studio.sqlite_evidence()
        print(f"   jobs={evidence['sqlite'].get('jobs_count')} "
              f"sessions={evidence['sqlite'].get('sessions_count')}")
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
    c1_raw_ok = all(
        v.get("raw_capture", {}).get("has_event_stream")
        and v.get("raw_capture", {}).get("sse_event_count", 0) > 0
        and v.get("content_type_match")
        for v in c1.values())
    vp = evidence["browser"]["viewports"]

    def has(vp_entry, key):
        return key in vp_entry and isinstance(vp_entry.get(key), dict)

    c2_ok = all(
        has(v, "c2_clarify")
        and v["c2_clarify"]["domChipCount"] >= 3
        and v["c2_clarify"]["domChipClickable"]
        and len(v["c2_clarify"]["stateOptions"]) >= 3
        for v in vp.values())
    c2_click_ok = all(
        v.get("c2_click_result", {}).get("input_value_after_click", "") != ""
        for v in vp.values())
    c3_ok = all(
        has(v, "c3_toolcard")
        and v["c3_toolcard"]["toolCount"] >= 1
        and v["c3_toolcard"]["toolsWithArgs"] >= 1
        and v["c3_toolcard"]["toolsWithResult"] >= 1
        and v["c3_toolcard"]["emptyResultBlocks"] == 0
        for v in vp.values())
    # 失败工具：错误文案可见 + 同样没有空结果区块
    c3_failed_ok = all(
        has(v, "c3_failed_tool")
        and v["c3_failed_tool"]["toolCount"] >= 1
        and v["c3_failed_tool"]["toolsWithErrorText"] >= 1
        and v["c3_failed_tool"]["emptyResultBlocks"] == 0
        for v in vp.values())
    # 流式增量：长度**必须真的在变**（distinct > 1），
    # 只看 monotonic 会被「一次性 0 -> 全文」骗过去。
    c3_stream_ok = all(
        has(v, "c3_stream")
        and v["c3_stream"].get("monotonic_increasing")
        and v["c3_stream"].get("max_len", 0) > 0
        and v["c3_stream"].get("distinct_lengths", 0) >= 3
        and len(v["c3_stream"].get("text_lengths") or []) >= 3
        for v in vp.values())
    console_errors = [c for c in evidence["browser"]["console"]
                      if c["type"] == "error"]
    step_errors = {w: v.get("errors", {}) for w, v in vp.items()
                    if v.get("errors")}
    evidence["verdict"] = {
        "c1_sse_content_type_all_carriers": c1_ok,
        "c1_raw_socket_capture_all_carriers": c1_raw_ok,
        "c2_clarify_clickable_options": c2_ok,
        "c2_chip_click_fills_input": c2_click_ok,
        "c3_toolcard_args_and_result": c3_ok,
        "c3_failed_tool_shows_error_text": c3_failed_ok,
        "c3_streaming_increment_observed": c3_stream_ok,
        "sqlite_jobs_recorded": evidence.get("sqlite", {}).get("jobs_count", 0) >= 1,
        "sqlite_sessions_recorded": (
            evidence.get("sqlite", {}).get("sessions_count", 0) >= 1),
        "step_errors": step_errors,
        "console_errors": len(console_errors),
        "page_errors": len(evidence["browser"]["pageErrors"]),
        "console_error_samples": console_errors[:5],
    }
    (OUT_DIR / "acceptance_c123_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== c1/c2/c3 判定 =====")
    print(json.dumps(evidence["verdict"], ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT_DIR}")
    ok = (c1_ok and c1_raw_ok and c2_ok and c2_click_ok and c3_ok
          and c3_failed_ok and c3_stream_ok
          and evidence.get("sqlite", {}).get("jobs_count", 0) >= 1
          and not console_errors and not evidence["browser"]["pageErrors"])
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
