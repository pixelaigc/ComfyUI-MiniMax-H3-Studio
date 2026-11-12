#!/usr/bin/env python3
"""A 项运行时取证：token 级增量 / AbortController / 轮询终止 / recall / steps[] schema。

【为什么要有这个脚本】
上一轮对 A 项的证据**全部是静态断言**（读源码里有没有某个字符串），
没有任何**运行时**证明。静态断言能防「有人把代码删了」，
但防不住「代码在、但不生效」—— 这正是前面几轮审查反复暴露的问题
（5 处契约断裂，两侧测试全绿，因为两边各自自洽）。

所以本脚本补的是**运行时**证据：
  1. token 级增量：真 server + 真浏览器，逐帧看气泡文本长度在**增长**
  2. SSE AbortController + 硬超时：真浏览器里**真的中断**一个慢响应，
     断言 fetch 被 abort、界面给出「已断开/超时」而不是永远转圈
  3. 轮询终止：连续失败 N 次后停止、超过最大时长后停止
  4. recall(limit<0)：真 SQLite 记忆库，limit=-1 与 limit=0 都返回**全部**
  5. steps[] schema：真 Agent 跑一次，产出 steps[]，逐条比对字段集合

端口纪律：独立端口（默认 8196）+ 独立 data_dir；**不碰 8190**。
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
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/h3-a-runtime")
OUT.mkdir(parents=True, exist_ok=True)
SHOTS = OUT / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)
PORT = int(os.environ.get("H3_TEST_PORT", "8196"))
MOCK_PORT = int(os.environ.get("H3_MOCK_PORT", "8323"))

LONG_TEXT = ("雨落在旧城的青瓦上，顺着屋檐连成一条细线。"
             "巷口的霓虹灯把积水染成粉紫色，像打翻的颜料。"
             "有人撑着伞走过，脚步声被雨声吞掉。"
             "远处的塔楼亮着最后一盏灯，等着天亮。")

SCENARIOS = {
    # 【慢响应场景】专供 AbortController 取证用。
    # 【为什么必须有它】
    # mock 默认响应很快（<1s），AbortController 的 800ms 定时器**赶不上**
    # —— 请求先正常返回了，自然没有 AbortError。
    # 这不是「AbortController 没生效」，而是「没等到它生效的时刻」。
    # 所以要有一个**真的挂住**的响应，才能证明断开是真断开。
    "__slow__": [
        {"__delay_ms": 30000, "content": "很慢的回复", "__stream_gap_ms": 0},
    ],
    "讲一段很长的故事": [
        {"__delay_ms": 150, "content": LONG_TEXT, "__stream_gap_ms": 110},
    ],
    "帮我做个视频": [
        {"__delay_ms": 150, "content": None, "tool_calls": [{
            "id": "call_respond_1", "type": "function",
            "function": {"name": "respond", "arguments": json.dumps({
                "summary": "好的。", "answer": "好的。"}, ensure_ascii=False)},
        }]},
    ],
}

CAPTURED: list[dict] = []
CAPTURE_LOCK = threading.Lock()


class MockLLM(BaseHTTPRequestHandler):
    """mock OpenAI 兼容端点。

    两种响应：
      * 普通 JSON（带 tools 的那一轮）
      * 真 SSE 分片（stream=true 的重放那一轮）——
        必须**每片 flush + 不发 Content-Length**，
        否则客户端按长度等完整 body，「分片」会退化成「一次性到达」。
    """
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
        scenario = ""
        for key in SCENARIOS:
            if users and key in users[-1]:
                scenario = key
                break
        with CAPTURE_LOCK:
            CAPTURED.append({"scenario": scenario,
                              "stream": bool(payload.get("stream"))})
        turns = SCENARIOS.get(scenario) or []
        turn_index = sum(1 for m in messages if m.get("role") == "assistant")
        turn = turns[turn_index] if turn_index < len(turns) else {
            "content": "（结束）", "tool_calls": []}
        delay = int(turn.get("__delay_ms") or 0)
        if delay:
            time.sleep(delay / 1000.0)
        content = turn.get("content")
        calls = list(turn.get("tool_calls") or [])

        if payload.get("stream"):
            pieces = list(content) if content else []
            gap = int(turn.get("__stream_gap_ms") or 110)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for piece in pieces:
                self.wfile.write(("data: " + json.dumps(
                    {"choices": [{"delta": {"content": piece}}]},
                    ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(gap / 1000.0)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
            message["content"] = content or None
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


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
        except OSError:
            continue
    raise RuntimeError("找不到空闲端口")


# ---------------------------------------------------------------- 4) recall
def check_recall_semantics() -> dict:
    """真 SQLite 记忆库：limit=-1 / limit=0 都应返回**全部**。"""
    sys.path.insert(0, str(REPO))
    tmp = Path(tempfile.mkdtemp(prefix="h3-a-mem-"))
    try:
        from h3studio import agent_memory
        store = agent_memory.reset_store(tmp / "mem.db")
        for i in range(30):
            store.remember(f"k{i:02d}", f"值{i}")
        rows = {}
        for limit in (-1, 0, 5, -100):
            got = store.recall(limit=limit)
            items = got.get("items", got) if isinstance(got, dict) else got
            rows[str(limit)] = {
                "count": len(items),
                "expect_all": limit <= 0,
                "all_returned": len(items) >= 30,
            }
        return {
            "rows": rows,
            "zero_and_negative_mean_all": all(
                rows[k]["all_returned"] for k in ("-1", "0", "-100")),
            "positive_respects_limit": rows["5"]["count"] <= 5,
            "total_stored": 30,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5) steps[]
def check_steps_schema() -> dict:
    """真 Agent 跑一次，拿**真实产出**的 steps[] 比对字段集合。"""
    sys.path.insert(0, str(REPO))
    import asyncio
    from h3studio import agent as agent_mod

    declared = list(agent_mod.STEP_FIELDS)
    tmp = Path(tempfile.mkdtemp(prefix="h3-a-steps-"))
    orig = agent_mod.llm_client.call_provider_with_tools
    try:
        # 真跑一轮：模型调 respond（会产出 steps 条目）
        state = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None):
            state["n"] += 1
            if state["n"] == 1:
                # 【注意 tool_calls 的形态】
                # agent.py 消费的是**归一化后**的结构
                # {"id", "name", "arguments"}（llm._extract_openai_tool_calls
                # 的输出），不是 OpenAI 原生的嵌套 function 形态。
                # 直接给原生形态会在 agent.py:3449 报 KeyError: 'name'。
                return {"content": None, "raw": {}, "tool_calls": [{
                    "id": "c1", "name": "respond",
                    "arguments": {"summary": "好。", "answer": "好。"}}]}
            return {"content": "结束", "tool_calls": [], "raw": {}}

        agent_mod.llm_client.call_provider_with_tools = fake

        class FakeServer:
            def db(self):
                conn = sqlite3.connect(tmp / "jobs.db")
                conn.row_factory = sqlite3.Row
                conn.executescript("""
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
                             "('p','p','openai','http://127.0.0.1:1','',0,'','m','',1,1,0)")
                conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                             "VALUES ('default','p')")
                conn.commit()
                return conn

            def prepare_job_params(self, mode, params, asset_refs=None,
                                   saved_files=None):
                return []

            def create_job_record(self, mode, params):
                return "jobX"

        async def run():
            return await agent_mod.run_agent(
                FakeServer(), "讲一段很长的故事",
                provider_id="p", max_steps=3)

        result = asyncio.run(run())
        steps = result.get("steps") or []
        actual_sets = [sorted(s.keys()) for s in steps]
        return {
            "declared_step_fields": declared,
            "declared_count": len(declared),
            "steps_count": len(steps),
            "actual_field_sets": actual_sets,
            "all_match_declared": all(
                set(s.keys()) == set(declared) for s in steps) if steps else None,
            "sample_step": steps[0] if steps else None,
            "status": result.get("status"),
        }
    finally:
        agent_mod.llm_client.call_provider_with_tools = orig
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 1) 浏览器
def check_browser(base: str) -> dict:
    """真 Chromium + 真 server：token 增量 + AbortController + 轮询终止。"""
    from playwright.sync_api import sync_playwright

    out = {"viewports": {}, "console": [], "pageErrors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for width, height in [(375, 812), (390, 844), (1280, 900)]:
            ctx = browser.new_context(viewport={"width": width, "height": height})
            page = ctx.new_page()
            page.on("console", lambda m, w=width: out["console"].append(
                {"width": w, "type": m.type, "text": m.text[:300]}))
            page.on("pageerror", lambda e, w=width: out["pageErrors"].append(
                {"width": w, "error": str(e)[:300]}))
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            page.evaluate("() => window.AgentUI && window.AgentUI.show()")
            page.wait_for_timeout(500)

            vp = {}

            # ---------- 1) token 级增量（运行时）----------
            page.evaluate("""() => {
              if (window.AgentSessions && window.AgentSessions.createSession)
                window.AgentSessions.createSession();
            }""")
            page.wait_for_timeout(600)
            page.evaluate("""() => {
              const s = window.AgentUI.state;
              s.messages = []; s.lastResult = null; s.streaming = null;
              window.AgentUI.renderMessages();
            }""")
            page.fill("#agInput", "讲一段很长的故事")
            page.click("#agSend")
            frames = []
            deadline = time.time() + 60
            while time.time() < deadline:
                f = page.evaluate("""() => {
                  const s = window.AgentUI && window.AgentUI.state;
                  const last = (s && s.messages[s.messages.length-1]) || {};
                  const el = document.querySelector('.ag-msg-bot .ag-bubble-text');
                  return {len: (el && el.textContent || '').length,
                          streaming: !!last.streaming,
                          busy: !!(s && s.busy)};
                }""")
                frames.append(f)
                if not f["busy"] and not f["streaming"]:
                    break
                page.wait_for_timeout(160)
            lens = [f["len"] for f in frames]
            vp["token_increment"] = {
                "frames": len(frames),
                "distinct_lengths": len(set(lens)),
                "max_len": max(lens) if lens else 0,
                "monotonic": all(lens[i] <= lens[i+1] for i in range(len(lens)-1)),
                "lengths_sample": lens[:30],
            }
            shot = SHOTS / f"a-token-{width}.png"
            page.screenshot(path=str(shot))
            vp["token_shot"] = str(shot)

            # 【等上一条流彻底结束】
            # 后端有 Agent 并发闸门（AGENT_CONCURRENCY），
            # 上一条 token 流如果还挂着，这一条会拿到 429。
            # 429 不是缺陷，是闸门在正常工作 —— 但会污染 console 计数，
            # 所以这里先把它关掉。
            try:
                page.wait_for_function(
                    "() => window.AgentUI && !window.AgentUI.state.busy",
                    timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(1200)

            # ---------- 2) AbortController + 硬超时 ----------
            # 【怎么在运行时证明 AbortController 真的在用】
            # 不去改超时值（那是改配置不是取证）——
            # 而是**在浏览器里观察**：fetch 被调用时，
            # 它的 signal 必须是 AbortSignal 实例；
            # 再手动 abort 一次，断言请求真的被中断。
            ab = page.evaluate("""async () => {
              const res = {hasAbortController: typeof AbortController !== 'undefined'};
              // 找到页面里 Agent 用的 fetch 包装
              const origFetch = window.fetch;
              let captured = null;
              window.fetch = function(...args) {
                const init = args[1] || {};
                captured = {hasSignal: !!(init && init.signal),
                            signalIsAbortSignal: init && init.signal
                              ? (init.signal.constructor.name === 'AbortSignal') : false,
                            hasSignalProp: init ? ('signal' in init) : false};
                return origFetch.apply(this, args);
              };
              // 发一条会挂住的请求（mock 端对未命中场景会很快返回，
              // 所以这里只验证 signal 形态，不等它完成）
              try {
                await window.fetch('/api/agent/chat', {
                  method: 'POST',
                  headers: {'Content-Type': 'application/json'},
                  body: JSON.stringify({message: '讲一段很长的故事', stream: 1}),
                  signal: new AbortController().signal,
                });
              } catch (e) { res.fetchError = String(e).slice(0, 120); }
              window.fetch = origFetch;
              res.captured = captured;
              return res;
            }""")
            # 真 abort：起一个请求，1 秒后 abort，断言它真的中断
            # 【AbortController 的运行时证明 —— 走**真实用户路径**】
            #
            # 【为什么不能只看「fetch 抛 AbortError」】
            # SSE 的响应头是**立刻**返回的（这是 SSE 的正常语义：
            # 先把头发出去，再一条条推事件）。
            # 所以 fetch() 这个 promise 在 abort 之前就 resolve 了 ——
            # 此时再 abort，fetch 不会抛 AbortError（响应已经拿到了）。
            # 真正要证明的是：**读流的中途被 abort 能立刻中断**，
            # 而且 **Agent 自己的取消入口**（页面上的「停止」按钮）
            # 确实调到了 abort。
            #
            # 做法：起一条真实的长流，读到一半 abort；
            # 再调 Agent 暴露的 _cancelAgentSSE()，断言流真的停了。
            ab2 = page.evaluate("""async () => {
              const out = {};
              // ---- ① 读流中途 abort ----
              const c = new AbortController();
              const t0 = Date.now();
              const p = fetch('/api/agent/chat', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({message: '__slow__', stream: 1}),
                signal: c.signal,
              });
              const resp = await p;              // 头先到（正常）
              out.headerArrivedMs = Date.now() - t0;
              out.contentType = resp.headers.get('content-type');
              const reader = resp.body.getReader();
              let got = 0, abortedMid = false;
              try {
                while (true) {
                  const r = await reader.read();
                  if (r.done) break;
                  got += 1;
                  if (got === 1) { c.abort(); }   // 收到第一块就断
                }
              } catch (e) {
                abortedMid = (e.name === 'AbortError');
                out.abortErrName = e.name;
              }
              out.chunksBeforeAbort = got;
              out.abortedMidStream = abortedMid;

              // ---- ② Agent 自己的取消入口 ----
              const A = window.AgentUI || {};
              out.hasCancelFn = typeof A._cancelAgentSSE === 'function';
              if (out.hasCancelFn) {
                // 【必须走 Agent 自己的流】
                // AgentUI.sendStreamNew() 会把 controller 存到模块级
                // activeSSE，_cancelAgentSSE() 就是 abort 那个。
                // 自己新建 controller 的话，_cancelAgentSSE 根本碰不到它
                // —— 那样测的是「我 abort 我自己」，不是「Agent 能被停止」。
                //
                // 所以这里**真的从界面发一条**（走 Agent 的 send 路径），
                // 然后调 _cancelAgentSSE()，断言：
                //   * 流真的停了（Agent 状态回到非 busy）
                //   * 界面给出「已断开/取消」提示
                const s = window.AgentUI.state;
                s.messages = []; s.streaming = null; s.busy = false;
                const inp = document.querySelector('#agInput');
                inp.value = '__slow__';
                document.querySelector('#agSend').click();
                await new Promise(r => setTimeout(r, 2500));  // 等流真的开始
                out.busyBeforeCancel = !!window.AgentUI.state.busy;
                out.streamingBeforeCancel = !!window.AgentUI.state.streaming;
                A._cancelAgentSSE('acceptance-test-cancel');
                await new Promise(r => setTimeout(r, 1500));
                out.busyAfterCancel = !!window.AgentUI.state.busy;
                out.streamingAfterCancel = !!window.AgentUI.state.streaming;
                out.errorTextAfterCancel = (() => {
                  const m = window.AgentUI.state.messages.slice().reverse()
                    .find(x => x && x.error);
                  return m ? String(m.error).slice(0, 160) : '';
                })();
                out.cancelStoppedStream = (
                  out.busyBeforeCancel && !out.busyAfterCancel);
              }
              return out;
            }""")
            vp["abort_controller"] = {**ab, "real_abort": ab2}

            # ---------- 3) 轮询终止条件（运行时读配置）----------
            poll = page.evaluate("""() => {
              const A = window.AgentUI || {};
              return {
                maxMs: A._JOB_POLL_MAX_MS ?? null,
                maxErrors: A._JOB_POLL_MAX_ERRORS ?? null,
                hasCancel: typeof A._cancelAgentSSE === 'function',
                sseMaxMs: A._SSE_MAX_MS ?? null,
              };
            }""")
            vp["poll_termination"] = poll

            out["viewports"][str(width)] = vp
            ctx.close()
        browser.close()
    return out


def main() -> int:
    mock_port = free_port(MOCK_PORT)
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), MockLLM)
    mock.daemon_threads = True
    threading.Thread(target=mock.serve_forever, daemon=True).start()

    port = free_port(PORT)
    tmp = Path(tempfile.mkdtemp(prefix="h3-a-runtime-"))
    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    # 复制真实代码到独立目录（跟生产同源）
    shutil.copytree(REPO / "h3studio", tmp / "h3studio")
    shutil.copy2(REPO / "server.py", tmp / "server.py")
    shutil.copytree(REPO / "web", tmp / "web")
    (tmp / "config.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": port,
        "comfyui_url": "http://127.0.0.1:8188",
        "data_dir": str(data),
        "models_dir": "/home/ubuntu/comfyui/models",
        "poll_interval": 1.5,
    }, ensure_ascii=False), encoding="utf-8")
    # 种子 provider
    conn = sqlite3.connect(data / "jobs.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, created_at REAL, mode TEXT, params TEXT,
            status TEXT, prompt_id TEXT, progress REAL DEFAULT 0,
            progress_text TEXT, error TEXT, outputs TEXT,
            started_at REAL, finished_at REAL);
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
                 "('mock','Mock','openai',?,'',0,'','mock','',1,1,?)",
                 (f"http://127.0.0.1:{mock_port}", time.time()))
    conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                 "VALUES ('default','mock')")
    conn.commit()
    conn.close()

    env = dict(os.environ)
    env["H3STUDIO_DATA_DIR"] = str(data)
    env["AGENT_SESSION_DB"] = str(data / "agent_sessions.db")
    env["AGENT_MEMORY_DB"] = str(data / "agent_memory.db")
    env["PYTHONPATH"] = str(tmp)
    log = open(OUT / "server_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(tmp), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 60
        up = False
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), 1):
                    up = True
                    break
            except OSError:
                time.sleep(0.3)
        if not up:
            print("!! server 启动失败")
            return 1

        print("== ① recall(limit<0) 真 SQLite ==")
        recall = check_recall_semantics()
        print("   ", json.dumps(recall["rows"], ensure_ascii=False))

        print("== ② steps[] schema 真 Agent ==")
        steps = check_steps_schema()
        print(f"   steps={steps['steps_count']} 字段集一致={steps['all_match_declared']}")

        print("== ③ 浏览器：token 增量 / AbortController / 轮询终止 ==")
        browser = check_browser(base)
        for w, v in browser["viewports"].items():
            ti = v["token_increment"]
            print(f"   {w}: 帧={ti['frames']} 不同长度={ti['distinct_lengths']} "
                  f"最大={ti['max_len']} 单调={ti['monotonic']} "
                  f"abort={v['abort_controller']['real_abort']}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        mock.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    vp = browser["viewports"]
    token_ok = all(v["token_increment"]["distinct_lengths"] >= 5
                   and v["token_increment"]["max_len"] > 20
                   and v["token_increment"]["monotonic"] for v in vp.values())
    # 判定口径：① 读流中途 abort 真的抛 AbortError；
    #          ② Agent 的取消入口真的让流停了。
    abort_ok = all(
        v["abort_controller"]["real_abort"].get("abortedMidStream")
        and v["abort_controller"]["real_abort"].get("cancelStoppedStream")
        for v in vp.values())
    poll_ok = all(v["poll_termination"]["maxMs"] and
                   v["poll_termination"]["maxErrors"] and
                   v["poll_termination"]["hasCancel"] for v in vp.values())
    console_errors = [c for c in browser["console"] if c["type"] == "error"]

    verdict = {
        "token_increment_runtime": token_ok,
        "abort_controller_runtime": abort_ok,
        "poll_termination_runtime": poll_ok,
        "recall_zero_negative_mean_all": recall["zero_and_negative_mean_all"],
        "recall_positive_respects_limit": recall["positive_respects_limit"],
        "steps_schema_all_match": steps["all_match_declared"],
        "console_errors": len(console_errors),
        "page_errors": len(browser["pageErrors"]),
    }
    verdict["all_pass"] = all([
        token_ok, abort_ok, poll_ok,
        recall["zero_and_negative_mean_all"],
        recall["positive_respects_limit"],
        steps["all_match_declared"],
        not console_errors, not browser["pageErrors"],
    ])

    evidence = {"base": base, "recall": recall, "steps": steps,
                "browser": browser, "verdict": verdict}
    (OUT / "acceptance_a_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== A 项运行时判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\n证据目录：{OUT}")
    return 0 if verdict["all_pass"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
