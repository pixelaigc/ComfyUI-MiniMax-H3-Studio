# -*- coding: utf-8 -*-
"""
内置 Agent 契约修复 —— **真实浏览器**端到端验证。

【这一脚本验证什么】
三轮审查的核心结论是「流式 100% 不生效 / 工具结果永不渲染 /
澄清快捷选项 100% 落空」，而这些结论都来自**静态读代码 + 抓包**。
本脚本用**真实 Chromium + 真实 server.py + 真实 web/ 前端**把它们
重新验一遍 —— 断言打在**真实 DOM** 上，不是 grep 源码。

【唯一被替换的东西】
本地大模型推理端点换成一个 **mock OpenAI 兼容端点**（mock_llm.py）。
其余全是真代码路径：真实 server.py、真实 h3studio/agent.py 编排、
真实 SSE 事件流、真实前端渲染、真实 SQLite 落库。

【场景】
  A 工具链 + 任务卡   -> 验证 tool_call/tool_result 的参数与结果真的渲染
  B 澄清式提问       -> 验证澄清快捷选项 chip 真的出现在 DOM 里
  C 流式增量         -> 验证 token 真的是**增量**（不是整包闪出）

【端口纪律】
  独立端口（默认 8197）+ 独立 data_dir + 独立 mock 端口（8321）。
  **不碰 8190 线上服务，不碰任何 systemd unit。**

用法：
    python3 scripts/verify_agent_contract_fix.py [证据输出目录]
"""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

OUT_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/agent-contract-fix")
OUT_DIR.mkdir(parents=True, exist_ok=True)
SHOTS = OUT_DIR / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [375, 390, 1280]
STUDIO_PORT = 8197
MOCK_PORT = 8321


def free_port(preferred: int) -> int:
    """优先用指定端口，不行就随便找一个空闲的。"""
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到空闲端口")


# ================================================================ mock LLM
#: 剧本：场景 -> 每一轮的模型输出
#:
#: 【无状态设计】游标从请求里的 messages 历史推导
#: （turn = assistant 消息条数），所以并发/孤儿连接不会吃掉别的场景的剧本。
SCENARIOS = {
    "生成一张雨夜霓虹街道的图": [
        {   # turn 0: 调 create_job
            "__delay_ms": 250,
            "content": None,
            "tool_calls": [{
                "id": "call_create_1", "type": "function",
                "function": {
                    "name": "create_job",
                    "arguments": json.dumps({
                        "mode": "t2i",
                        "prompt": "雨夜霓虹街道，积水反射粉紫色招牌光晕，电影级广角构图",
                        "aspect": "16:9", "resolution": "1mp",
                    }, ensure_ascii=False),
                },
            }],
        },
        {   # turn 1: 分两段给结论（验证 token 增量）
            "__delay_ms": 200, "content": "已经按你的描述下发了 ",
        },
        {   # turn 2: 继续
            "__delay_ms": 200, "content": "1 个文生图任务，16:9 / 1MP，",
        },
        {   # turn 3: 收尾 + respond
            "__delay_ms": 200, "content": "提示词已做细节强化。",
        },
    ],
    "帮我做个视频": [
        {   # turn 0: 先问清楚（澄清）
            "__delay_ms": 250,
            "content": None,
            "tool_calls": [{
                "id": "call_respond_1", "type": "function",
                "function": {
                    "name": "respond",
                    "arguments": json.dumps({
                        "summary": "你要生成视频的话，需要确认画面比例：想要竖屏还是横屏？",
                        "needs_clarification": True,
                        "clarify_options": ["9:16 竖屏", "16:9 横屏", "1:1 方形"],
                        "clarify_hint": "视频时长合法区间是 5~15 秒；比例我建议 16:9。",
                    }, ensure_ascii=False),
                },
            }],
        },
        {   # turn 1: 等用户回答
            "__delay_ms": 200, "content": "等你确认比例。",
        },
    ],
}


def scenario_of(text: str) -> str:
    for key in SCENARIOS:
        if key in (text or ""):
            return key
    return ""


class MockLLM(BaseHTTPRequestHandler):
    """讲 OpenAI /v1/chat/completions 协议的最小 mock 端点。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 静默
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
        messages = payload.get("messages") or []
        users = [str(m.get("content") or "") for m in messages
                 if m.get("role") == "user"]
        # 【为什么取最后一条 user 消息】
        # 会话历史里会累积前面的 user 消息；取最后一条才能对上「这一轮
        # 是哪个场景」。取第一条的话，第二轮之后永远命中第一个场景。
        scenario = scenario_of(users[-1]) if users else ""
        turns = SCENARIOS.get(scenario) or []
        turn_index = sum(1 for m in messages if m.get("role") == "assistant")
        turn = turns[turn_index] if turn_index < len(turns) else {
            "content": "（结束）", "tool_calls": []}

        delay = int(turn.get("__delay_ms") or 0)
        if delay:
            time.sleep(delay / 1000.0)

        message = {"role": "assistant", "content": turn.get("content")}
        calls = []
        for index, call in enumerate(turn.get("tool_calls") or []):
            calls.append({
                "id": call.get("id") or f"call_{index}",
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                },
            })
        if calls:
            message["tool_calls"] = calls
            message["content"] = turn.get("content") or None
        body = {
            "id": "mock", "object": "chat.completion", "model": "mock",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ================================================================ 独立实例
class Studio:
    """在临时目录里起一个独立端口的 Studio（真实 server.py + 真实前端）。"""

    def __init__(self, port: int, mock_port: int):
        self.port = port
        self.mock_port = mock_port
        self.tmp = Path(tempfile.mkdtemp(prefix="h3studio-contractfix-"))
        self.data = self.tmp / "data"
        self.proc = None
        self.base = f"http://127.0.0.1:{port}"

    def _seed(self):
        self.data.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.data / "jobs.db")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
                status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                started_at REAL, finished_at REAL
            );
            CREATE TABLE IF NOT EXISTS job_batches (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL, idx INTEGER NOT NULL,
                seed INTEGER, prompt_id TEXT, progress REAL DEFAULT 0,
                progress_text TEXT, error TEXT, outputs TEXT,
                created_at REAL, started_at REAL, finished_at REAL
            );
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
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO model_providers VALUES "
            "('mockprov','Mock 多模态','openai',?,'',0,'','mock-model','',1,1,?)",
            (f"http://127.0.0.1:{self.mock_port}", now))
        conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                     "VALUES ('default','mockprov')")
        conn.commit()
        conn.close()

    def start(self):
        shutil.copytree(REPO / "h3studio", self.tmp / "h3studio")
        shutil.copy2(REPO / "server.py", self.tmp / "server.py")
        shutil.copytree(REPO / "web", self.tmp / "web")
        cfg = {
            "host": "127.0.0.1", "port": self.port,
            "comfyui_url": "http://127.0.0.1:8188",
            "data_dir": str(self.data),
            "models_dir": "/home/ubuntu/comfyui/models",
            "poll_interval": 1.5,
        }
        (self.tmp / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
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

    def read_jobs(self):
        """从**真实 SQLite** 读任务记录（不信任 API 响应）。"""
        path = self.data / "jobs.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, mode, status, params, created_at FROM jobs "
            "ORDER BY created_at DESC").fetchall()
        conn.close()
        out = []
        for row in rows:
            try:
                params = json.loads(row["params"] or "{}")
            except Exception:
                params = {}
            out.append({"id": row["id"], "mode": row["mode"],
                        "status": row["status"], "params": params,
                        "created_at": row["created_at"]})
        return out

    def read_sessions(self):
        """读真实会话库（证明对话落库）。"""
        path = self.data / "agent_sessions.db"
        if not path.exists():
            return []
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT session_id, role, content FROM agent_messages "
                "ORDER BY id").fetchall()
        except Exception:
            rows = []
        conn.close()
        return [{"session_id": r["session_id"], "role": r["role"],
                 "content": str(r["content"])[:400]} for r in rows]


# ================================================================ 浏览器
def run_browser(studio: Studio):
    results = {"viewports": {}, "console": [], "requests": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for width in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": width, "height": 820})
            page = ctx.new_page()
            console_log = []
            page.on("pageerror", lambda e: console_log.append(
                {"type": "pageerror", "text": str(e)}))
            page.on("console", lambda m: console_log.append(
                {"type": f"console.{m.type}", "text": m.text})
                if m.type in ("error", "warning") else None)
            page.on("requestfailed", lambda r: console_log.append(
                {"type": "requestfailed", "text": f"{r.url} {r.failure}"}))

            agent_requests = []
            page.on("response", lambda r: agent_requests.append({
                "url": r.url, "status": r.status,
                "ct": r.headers.get("content-type", "")})
                if "/api/agent/chat" in r.url else None)

            page.goto(studio.base, wait_until="networkidle")
            page.wait_for_timeout(800)
            # 进 Agent 页。
            #
            # 【为什么直接调 AgentUI.show() 而不是点 #navAgent】
            # 窄屏（375/390）下侧栏是收起的，nav 元素在视口外，
            # Playwright 的 click 会一直等「element is outside of the viewport」。
            # 而 show() 就是 nav 点击时执行的那个函数 —— 走的是**真实代码路径**。
            page.evaluate("() => window.AgentUI && window.AgentUI.show()")
            page.wait_for_timeout(600)

            # 每个场景都用**新会话**。
            #
            # 【为什么必须换会话】
            # mock LLM 的剧本游标是从 messages 历史推导的
            # （turn = assistant 消息条数）。同一会话里连着跑三个场景，
            # 第二个场景的 turn_index 会接着第一个继续 —— 拿到错误的剧本，
            # 于是「澄清」场景跑出来的是「建任务」的剧本。
            # 换会话 = 历史清零 = 每个场景都从 turn 0 开始。
            def new_session():
                page.evaluate("""() => {
                    const s = window.AgentUI.state;
                    s.sessionId = '';
                    s.messages = [];
                    s.lastResult = null;
                    s.streaming = null;
                    window.AgentUI.renderMessages();
                }""")
                page.wait_for_timeout(300)

            # ---------- 场景 A：工具链 + 任务卡 ----------
            page.fill("#agInput", "生成一张雨夜霓虹街道的图")
            page.click("#agSend")
            # 等这一轮彻底结束（busy 解除）+ 任务卡真的建出来了。
            # 【为什么等 state】折叠容器里的元素 Playwright 不算 visible。
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && last.jobs && last.jobs.length);
                }""",
                timeout=60000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy",
                timeout=60000)
            page.wait_for_timeout(1200)

            # 展开工具块
            page.evaluate("""() => {
              document.querySelectorAll('.ag-tool').forEach(d => { d.open = true; });
            }""")
            page.wait_for_timeout(400)

            a_info = page.evaluate("""() => ({
              toolCount: document.querySelectorAll('.ag-tool').length,
              tools: Array.from(document.querySelectorAll('.ag-tool')).map(t => ({
                name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
                args: (t.querySelector('.ag-tool-args code')||{}).textContent || '',
                result: (t.querySelector('.ag-tool-result code')||{}).textContent || '',
                err: (t.querySelector('.ag-tool-err')||{}).textContent || '',
                state: (t.querySelector('.ag-tool-state')||{}).textContent || '',
              })),
              jobCount: document.querySelectorAll('.ag-job').length,
              jobParams: Array.from(document.querySelectorAll('.ag-job .ag-param'))
                                .map(e => e.textContent),
              bubbleText: (document.querySelector('.ag-msg-bot .ag-bubble-text')||{}).textContent || '',
            })""")
            page.screenshot(path=str(SHOTS / f"fix-{width}-01-toolchain.png"),
                            full_page=True)

            # ---------- 场景 B：澄清式提问 ----------
            new_session()
            page.fill("#agInput", "帮我做个视频")
            page.click("#agSend")
            # 【为什么等 state 而不是等 DOM 选择器】
            # .ag-clarify 元素在某些宽度下位于一个**折叠的 details** 里，
            # Playwright 的 wait_for_selector 要求「visible」——
            # 折叠容器里的元素不算 visible，于是永远等不到。
            # 而 state.clarify 是**真实前端状态**（更该断言的对象）。
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && last.clarify && last.clarify.options
                              && last.clarify.options.length);
                }""",
                timeout=60000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy",
                timeout=60000)
            page.wait_for_timeout(800)
            b_info = page.evaluate("""() => {
              const s = window.AgentUI.state;
              const last = s.messages[s.messages.length - 1];
              return {
                // 前端状态（真实前端内部数据）
                stateClarify: last.clarify || null,
                // DOM（真实渲染）
                hasBubble: !!document.querySelector('.ag-clarify'),
                question: (document.querySelector('.ag-clarify-q')||{}).textContent || '',
                options: Array.from(document.querySelectorAll('.ag-clarify-opt'))
                              .map(b => b.textContent),
                hint: (document.querySelector('.ag-clarify-hint')||{}).textContent || '',
                hasInput: !!document.querySelector('.ag-clarify-input'),
                hasSend: !!document.querySelector('.ag-clarify-send'),
              };
            }""")
            page.screenshot(path=str(SHOTS / f"fix-{width}-02-clarify.png"),
                            full_page=True)

            # ---------- 场景 C：流式增量 ----------
            #
            # 【测什么 —— 必须诚实】
            # 后端的 LLM 客户端用的是 `"stream": False`
            # （h3studio/llm.py），所以 **token 事件不是逐字增量**，
            # 它只在模型给出最终结论时发一次。
            # 真正的「流式增量」是**事件本身分批到达**：
            # progress → tool_call → tool_result → job → done → result
            # 一条一条地送到浏览器（而不是一次性 JSON）。
            #
            # 所以这里测两件事：
            #   ① SSE 响应体是**分块**到达的（记录每块到达时刻）
            #   ② 事件类型**逐步推进**（progress 先于 tool_call 先于 done）
            #
            # 【怎么测 chunk】在页面里包一层 fetch，把响应体的
            # ReadableStream 逐块读出来并打时间戳 —— 这不改变任何行为，
            # 只是「在旁边看着」。
            page.evaluate("""() => {
              window.__chunks = [];
              window.__events = [];
              window.__t0 = Date.now();
              const origFetch = window.fetch;
              window.fetch = async (url, opts) => {
                const resp = await origFetch(url, opts);
                try {
                  const ct = resp.headers.get('content-type') || '';
                  if (!ct.includes('text/event-stream') || !resp.body) return resp;
                  const reader = resp.body.getReader();
                  const decoder = new TextDecoder();
                  const tap = new ReadableStream({
                    async pull(controller) {
                      const {value, done} = await reader.read();
                      if (done) { controller.close(); return; }
                      window.__chunks.push({at: Date.now() - window.__t0,
                                           bytes: value.length});
                      const text = decoder.decode(value, {stream:true});
                      for (const line of text.split('\\n')) {
                        if (line.startsWith('event:')) {
                          window.__events.push({at: Date.now() - window.__t0,
                                               name: line.slice(6).trim()});
                        }
                      }
                      controller.enqueue(value);
                    }
                  });
                  return new Response(tap, {status: resp.status,
                                            headers: resp.headers});
                } catch (e) { return resp; }
              };
            }""")
            new_session()
            page.evaluate("() => { window.__t0 = Date.now(); }")
            page.fill("#agInput", "生成一张雨夜霓虹街道的图")
            page.click("#agSend")
            page.wait_for_function(
                """() => {
                    const s = window.AgentUI && window.AgentUI.state;
                    if (!s) return false;
                    const last = s.messages[s.messages.length - 1];
                    return !!(last && last.jobs && last.jobs.length);
                }""",
                timeout=60000)
            page.wait_for_function(
                "() => window.AgentUI && !window.AgentUI.state.busy",
                timeout=60000)
            page.wait_for_timeout(1500)
            c_info = page.evaluate("""() => {
              const chunks = window.__chunks || [];
              const events = window.__events || [];
              const names = events.map(e => e.name);
              // 事件类型**逐步推进**：progress 必须早于 tool_call
              // tool_call 必须早于 done
              const idxProgress = names.indexOf('progress');
              const idxTool = names.indexOf('tool_call');
              const idxDone = names.indexOf('done');
              return {
                chunkCount: chunks.length,
                chunkTimes: chunks.map(c => c.at),
                eventCount: events.length,
                eventNames: names,
                orderOk: idxProgress >= 0 && idxTool > idxProgress
                          && idxDone > idxTool,
                firstChunkAt: chunks.length ? chunks[0].at : null,
                lastChunkAt: chunks.length ? chunks[chunks.length-1].at : null,
              };
            }""")
            page.screenshot(path=str(SHOTS / f"fix-{width}-03-stream.png"),
                            full_page=True)

            # ---------- 布局实测 ----------
            layout = page.evaluate("""() => {
              const doc = document.documentElement;
              const input = document.querySelector('#agInput');
              const r = input ? input.getBoundingClientRect() : null;
              return {
                scrollWidth: doc.scrollWidth,
                clientWidth: doc.clientWidth,
                overflowPx: doc.scrollWidth - doc.clientWidth,
                hasHScroll: doc.scrollWidth > doc.clientWidth + 1,
                inputRect: r ? {top:r.top, bottom:r.bottom, left:r.left,
                                right:r.right, w:r.width, h:r.height} : null,
                viewportH: window.innerHeight,
              };
            }""")

            results["viewports"][str(width)] = {
                "agent_toolchain": a_info,
                "agent_clarify": b_info,
                "agent_stream": c_info,
                "layout": layout,
                "agent_responses": agent_requests,
                "console": console_log,
            }
            results["console"].extend(
                [{"viewport": width, **c} for c in console_log])
            ctx.close()
        browser.close()
    return results


def main():
    print("== 起 mock LLM ==")
    mock_port = free_port(MOCK_PORT)
    mock_server = ThreadingHTTPServer(("127.0.0.1", mock_port), MockLLM)
    mock_server.daemon_threads = True
    threading.Thread(target=mock_server.serve_forever, daemon=True).start()
    print(f"   mock LLM: http://127.0.0.1:{mock_port}")

    print("== 起独立 Studio ==")
    port = free_port(STUDIO_PORT)
    studio = Studio(port, mock_port)
    if not studio.start():
        print("!! Studio 启动失败")
        studio.stop()
        return 1
    print(f"   Studio: {studio.base}")

    try:
        print("== 跑真实浏览器 ==")
        results = run_browser(studio)
        # 【为什么必须在 studio.stop() 之前读】
        # stop() 会删掉临时 data_dir —— 之后就读不到落库证据了。
        results["sqlite_jobs"] = studio.read_jobs()
        results["sqlite_sessions"] = studio.read_sessions()
    finally:
        studio.stop()
        mock_server.shutdown()
    (OUT_DIR / "browser_contract_evidence.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 断言 ----------
    failures = []
    for width, data in results["viewports"].items():
        # ① 流式真的命中 SSE
        ctypes = [r["ct"] for r in data["agent_responses"]]
        if not any("text/event-stream" in c for c in ctypes):
            failures.append(f"[{width}] 没有一个 agent 请求返回 text/event-stream：{ctypes}")
        # ② 工具结果真的渲染
        tools = data["agent_toolchain"]["tools"]
        if not tools:
            failures.append(f"[{width}] 没有渲染出工具块")
        else:
            if not any(t["args"] for t in tools):
                failures.append(f"[{width}] 工具块没有「参数」行：{tools}")
            if not any(t["result"] for t in tools):
                failures.append(f"[{width}] 工具块没有「结果」行：{tools}")
        # ③ 澄清快捷选项真的出现（前端状态 + DOM 两边都要）
        clarify = data["agent_clarify"]
        if not clarify["hasBubble"]:
            failures.append(f"[{width}] 没有澄清气泡")
        state_options = (clarify.get("stateClarify") or {}).get("options") or []
        if not state_options:
            failures.append(f"[{width}] 前端状态里没有澄清选项：{clarify}")
        if not clarify["options"]:
            failures.append(f"[{width}] DOM 里没有澄清快捷选项：{clarify}")
        if not clarify.get("hasInput"):
            failures.append(f"[{width}] 澄清气泡没有输入框")
        # ④ 流式真的是**分块 + 逐步推进**（不是一次性 JSON）
        stream = data["agent_stream"]
        if stream["chunkCount"] < 2:
            failures.append(
                f"[{width}] SSE 不是分块到达（chunkCount={stream['chunkCount']}）："
                f"{stream['chunkTimes']}")
        if not stream["orderOk"]:
            failures.append(
                f"[{width}] 事件顺序不对（progress -> tool_call -> done）："
                f"{stream['eventNames']}")
        # ⑤ 布局无横向溢出
        if data["layout"]["overflowPx"] > 0:
            failures.append(f"[{width}] 横向溢出 {data['layout']['overflowPx']}px")
        # ⑥ console 无 JS 错误
        js_errors = [c for c in data["console"]
                     if c["type"] in ("pageerror", "console.error",
                                      "requestfailed")]
        if js_errors:
            failures.append(f"[{width}] console 有 {len(js_errors)} 条错误：{js_errors[:3]}")

    # ⑦ 真实落库
    if not results["sqlite_jobs"]:
        failures.append("SQLite 里没有任务记录")
    if not results["sqlite_sessions"]:
        failures.append("SQLite 里没有会话记录")

    print("\n===== 断言结果 =====")
    if failures:
        for f in failures:
            print("FAIL", f)
    else:
        print("全部通过（流式命中 / 工具参数+结果 / 澄清选项 / 增量渲染 / 无溢出 / console 干净 / 落库）")
    print(f"\n证据目录：{OUT_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
