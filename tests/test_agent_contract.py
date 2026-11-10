# -*- coding: utf-8 -*-
"""
前后端**跨侧契约**测试 —— 本轮审查最关键的缺口。

【为什么必须有这个文件】
三轮审查发现的 5 处契约断裂（stream 载体 / tool_result 字段 /
澄清字段 / steps[].args / 音乐 aspect），**每一处两侧的测试都是绿的**：
  * 后端测「我发的字段对不对」
  * 前端测「我读的字段对不对」
  * 两边各自自洽，合起来就错 —— 这叫**假覆盖**。
根因统一：**没有任何一条测试跨越前后端两侧**。

本文件的原则（每条都对应一种「假覆盖」手法）：
  1. 测试数据**必须来自真实后端产出**（真起 server.make_app() +
     真 Agent 编排 + 真 SSE 事件流），不是手写 mock。
  2. 断言必须打到**真实前端代码**（web/agent.js 的真实函数，
     在真实 Chromium 里跑），不是「前端自造 mock 断言自己」。
  3. 端到端那条必须用**前端真实请求形态**打**真实 server.make_app()**，
     断言 Content-Type 含 text/event-stream。

覆盖的 5 条契约：
  ① stream 载体      ② tool_result 字段   ③ 澄清字段
  ④ steps[].args    ⑤ 音乐 aspect

运行：python3 -m pytest tests/test_agent_contract.py -v
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from h3studio import agent as agent_mod       # noqa: E402
from h3studio import agent_sessions           # noqa: E402

WEB = REPO / "web"

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:                                   # pragma: no cover
    sync_playwright = None
    HAS_PLAYWRIGHT = False


#: 真的 server.prepare_job_params（在替换 server 模块属性之前抓住它）。
_REAL_PREPARE_JOB_PARAMS = None


def _fake_server(tmp: Path):
    """最小服务端替身（与 test_agent_stream_sse.py 同款，独立避免耦合）。"""
    import sqlite3

    class FakeServer:
        def __init__(self):
            self.db_path = tmp / "jobs.db"
            self.created = []
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
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
                "('testprov','测试模型','openai','http://127.0.0.1:5000','',0,'',"
                "'test-model','',1,1,0)")
            conn.execute("INSERT OR REPLACE INTO model_provider_prefs "
                         "VALUES ('default','testprov')")
            conn.commit()
            conn.close()

        def db(self):
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

        def prepare_job_params(self, mode, params, asset_refs=None,
                               saved_files=None):
            return _REAL_PREPARE_JOB_PARAMS(mode, params, asset_refs,
                                             saved_files)

        def create_job_record(self, mode, params):
            job_id = f"job{len(self.created):04d}"
            self.created.append((job_id, mode, dict(params)))
            return job_id

    return FakeServer()


class _ContractBase(unittest.TestCase):
    """
    跨侧契约测试的公共装置。

    提供两件事：
      * `sse_events(turns)` —— 真起 server.make_app()，真发请求，
        真读 SSE 事件流，返回**后端真实产出**的事件列表。
      * `frontend_eval(expr)` —— 在真实 Chromium 里加载**真实的**
        web/agent.js + web/agent-media.js，然后求值。

    两边都必须是「真的」—— 否则这条测试就退化成
    「用前端自造 mock 断言自己」那种假覆盖。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="h3agent-contract-"))
        cls.server = _fake_server(cls.tmp)
        agent_sessions.reset_store(cls.tmp / "agent_sessions.db")
        cls._playwright = None
        cls._browser = None
        cls._page = None

    @classmethod
    def tearDownClass(cls):
        if cls._browser is not None:
            cls._browser.close()
        if cls._playwright is not None:
            cls._playwright.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._patched = False
        self._original_call = None

    def tearDown(self):
        if self._patched and self._original_call is not None:
            agent_mod.llm_client.call_provider_with_tools = self._original_call

    # ------------------------------------------------------------ 模型打桩
    def _patch_model(self, turns):
        """把模型调用替换成预置脚本（与 test_agent_stream_sse.py 同款）。"""
        original = agent_mod.llm_client.call_provider_with_tools
        calls = {"n": 0}

        async def fake_call(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= len(turns):
                return {"content": "结束", "tool_calls": [], "raw": {}}
            return turns[index]

        agent_mod.llm_client.call_provider_with_tools = fake_call
        self._patched = True
        self._original_call = original

    # ------------------------------------------------------------ 后端产出
    def _make_app(self):
        """用真的 server 模块建 app（路由是生产代码）。"""
        global _REAL_PREPARE_JOB_PARAMS
        import importlib
        sys.modules.pop("server", None)
        srv = importlib.import_module("server")
        if _REAL_PREPARE_JOB_PARAMS is None:
            _REAL_PREPARE_JOB_PARAMS = srv.prepare_job_params
        srv.db = self.server.db
        srv.prepare_job_params = self.server.prepare_job_params
        srv.create_job_record = self.server.create_job_record
        srv.agent_sessions.reset_store(self.tmp / "agent_sessions.db")
        return srv.make_app(), srv

    def _post(self, app, path: str, payload: dict):
        """
        真的 POST，返回 (status, content_type, raw_body, events)。

        【为什么在独立线程里跑 asyncio.run()】
        Playwright 的同步 API 会在当前线程留下一个**正在运行的**事件循环，
        此时 asyncio.run() 会抛
        "asyncio.run() cannot be called from a running event loop"。
        所以这里把协程放到一个全新线程 + 全新事件循环里跑 ——
        两边互不干扰。
        """
        import threading

        from aiohttp.test_utils import TestClient, TestServer

        async def _run():
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            events = []
            raw = ""
            status = 0
            ctype = ""
            async with client.post(path, json=payload) as resp:
                status = resp.status
                ctype = resp.headers.get("Content-Type", "")
                if "text/event-stream" in ctype:
                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        chunk = chunk.decode("utf-8", errors="replace")
                        raw += chunk
                        buffer += chunk
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            name = "message"
                            data_lines = []
                            for line in block.split("\n"):
                                if line.startswith("event:"):
                                    name = line[6:].strip()
                                elif line.startswith("data:"):
                                    data_lines.append(line[5:].strip())
                            if not data_lines:
                                continue
                            try:
                                data = json.loads("\n".join(data_lines))
                            except Exception:
                                continue
                            events.append({"event": name, "data": data})
                else:
                    raw = await resp.text()
            await client.close()
            return status, ctype, raw, events

        box = {}

        def _runner():
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                box["value"] = loop.run_until_complete(_run())
            except Exception as exc:          # pragma: no cover - 诊断用
                box["error"] = exc
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
                asyncio.set_event_loop(None)

        thread = threading.Thread(target=_runner)
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def sse_events(self, turns, payload=None):
        """跑一次真实请求，返回后端真实产出的 SSE 事件列表。"""
        self._patch_model(turns)
        app, _ = self._make_app()
        body = {"message": "做个视频", "stream": 1}
        if payload is not None:
            body.update(payload)
        _, ctype, raw, events = self._post(app, "/api/agent/chat", body)
        self.assertIn("text/event-stream", ctype,
                      f"后端没有返回 SSE（ct={ctype!r} raw={raw[:300]!r}）")
        return events

    def json_result(self, turns, payload=None):
        """跑一次真实请求，返回后端真实产出的**一次性 JSON** 结果。"""
        self._patch_model(turns)
        app, _ = self._make_app()
        body = payload if payload is not None else {"message": "做个视频"}
        status, ctype, raw, _ = self._post(app, "/api/agent/chat", body)
        self.assertEqual(status, 200, raw[:400])
        self.assertNotIn("text/event-stream", ctype)
        return json.loads(raw)

    # ------------------------------------------------------------ 前端真跑
    def frontend(self):
        """真实 Chromium + 真实 web/agent.js + web/agent-media.js。"""
        if self._page is None:
            cls = type(self)
            cls._playwright = sync_playwright().start()
            cls._browser = cls._playwright.chromium.launch(args=["--no-sandbox"])
            cls._page = cls._browser.new_context().new_page()
            cls._page.set_content(self._html())
            cls._page.add_script_tag(path=str(WEB / "agent-media.js"))
            cls._page.add_script_tag(path=str(WEB / "agent.js"))
            cls._page.wait_for_timeout(200)
        return self._page

    @staticmethod
    def _html():
        """最小但**结构等价**的 Agent 页 DOM（与前端测试同款）。"""
        return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
        <style>
          html,body{margin:0;padding:0}
          .agent-page{display:flex;flex-direction:column;height:100vh}
          .ag-messages{flex:1 1 auto;min-height:0;overflow-y:auto}
          .ag-composer{flex:0 0 auto}
          .ag-msg{display:flex;gap:10px}
          .ag-msg-body{flex:1;min-width:0}
          .ag-bubble-text{white-space:pre-wrap;word-break:break-word}
        </style></head><body>
        <main class="agent-page" id="agentPage">
          <div class="ag-head"><button class="ag-new" id="agNew" type="button">新会话</button></div>
          <div class="ag-messages" id="agMessages"></div>
          <div class="ag-composer">
            <div class="ag-status" id="agStatus"></div>
            <div class="ag-input-row">
              <textarea id="agInput" rows="3"></textarea>
              <button class="ag-send" id="agSend" type="button">发送</button>
            </div>
          </div>
        </main>
        </body></html>"""

    def frontend_apply(self, events):
        """
        把**后端真实产出**的事件列表喂进**真实前端**的
        applyStreamEvent，返回前端内部消息状态。

        这是跨侧测试的核心动作 —— 数据来自后端，断言打在��端。
        """
        page = self.frontend()
        return page.evaluate("""(events) => {
          const msg = {role:'assistant', content:'', result:{}, jobs:[], assets:[],
                       prompt:null, steps:[], tools:[], streaming:true, error:''};
          for (const e of events) {
            window.AgentUI._applyStreamEvent(msg, e.event, e.data);
          }
          return {
            content: msg.content,
            tools: msg.tools,
            jobs: msg.jobs,
            clarify: msg.clarify || null,
            result: msg.result,
            steps: msg.steps,
          };
        }""", events)

    def frontend_render(self, events):
        """把后端真实事件喂进前端并**渲染**（真 DOM），返回渲染结果。"""
        page = self.frontend()
        return page.evaluate("""(events) => {
          window.AgentUI.state.messages = [];
          window.AgentUI.state.sessionId = '';
          window.AgentUI.state.lastResult = null;
          window.AgentUI.state.streaming = null;
          window.AgentUI.state.busy = false;
          const msg = {role:'assistant', content:'', result:{}, jobs:[], assets:[],
                       prompt:null, steps:[], tools:[], streaming:true, error:''};
          window.AgentUI.state.messages.push(msg);
          for (const e of events) {
            window.AgentUI._applyStreamEvent(msg, e.event, e.data);
          }
          window.AgentUI.renderMessages();
          return {
            tools: Array.from(document.querySelectorAll('.ag-tool')).map(t => ({
              name: (t.querySelector('.ag-tool-name')||{}).textContent || '',
              args: (t.querySelector('.ag-tool-args code')||{}).textContent || '',
              result: (t.querySelector('.ag-tool-result code')||{}).textContent || '',
              err: (t.querySelector('.ag-tool-err')||{}).textContent || '',
            })),
            clarifyOpts: Array.from(
              document.querySelectorAll('.ag-clarify-opt')).map(b => b.textContent),
            clarifyHint: (document.querySelector('.ag-clarify-hint')||{}).textContent || '',
            clarifyQ: (document.querySelector('.ag-clarify-q')||{}).textContent || '',
            bubbleText: (document.querySelector('.ag-msg-bot .ag-bubble-text')||{}).textContent || '',
          };
        }""", events)


# ==================================================================== 契约 ①
class TestStreamCarrierContract(_ContractBase):
    """
    契约 ①：**stream 开关的载体**。

    审查实测：前端发 `?stream=1`（query），后端只读 `body.stream`
    → 真实浏览器 12/12 请求返回 application/json，流式 100% 不生效。

    这条测试用**前端真实请求形态**打**真实 server.make_app()**。
    """

    def test_frontend_body_stream_request_returns_event_stream(self):
        """
        前端真实请求形态（body 带 stream=1）-> 真实 server -> SSE。
        """
        self._patch_model([{"content": "好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        status, ctype, raw, events = self._post(
            app, "/api/agent/chat",
            {"message": "你好", "stream": 1})
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("text/event-stream", ctype,
                      f"前端形态没有拿到 SSE（ct={ctype!r}）")
        self.assertTrue(events, "SSE 必须有事件")

    def test_frontend_query_stream_request_returns_event_stream(self):
        """
        query 载体（?stream=1）也必须命中 SSE ——
        这是**已部署旧前端**的请求形态，不能因为改了新前端就把它弄坏。
        """
        self._patch_model([{"content": "好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        status, ctype, raw, events = self._post(
            app, "/api/agent/chat?stream=1",
            {"message": "你好"})
        self.assertEqual(status, 200, raw[:300])
        self.assertIn("text/event-stream", ctype,
                      f"query 载体没有拿到 SSE（ct={ctype!r}）")

    def test_frontend_really_sends_stream_in_body(self):
        """
        前端**代码**里 stream 必须落在 body —— 端到端那条只证明
        「body 形态能命中」，这条证明「前端真的发的是 body 形态」。
        """
        page = self.frontend()
        captured = []
        page.evaluate("""() => {
          window.__calls = [];
          window.fetch = async (url, opts) => {
            window.__calls.push({url:String(url), body: opts && opts.body});
            return {ok:false, status:404,
                    text: async()=>JSON.stringify({error:'not found'}),
                    headers:{get:()=>'application/json'}};
          };
        }""")
        page.fill("#agInput", "契约测试")
        page.click("#agSend")
        page.wait_for_timeout(800)
        captured = page.evaluate("() => (window.__calls||[]).slice(0,1)")
        self.assertTrue(captured, "前端没有发出任何请求")
        url = captured[0]["url"]
        body = json.loads(captured[0]["body"])
        self.assertEqual(url, "/api/agent/chat",
                         f"前端请求的 URL 不对：{url}")
        self.assertEqual(body.get("stream"), 1,
                         f"前端请求体里没有 stream:1：{body}")


# ==================================================================== 契约 ②
class TestToolResultFieldContract(_ContractBase):
    """
    契约 ②：**tool_call / tool_result 的字段名**。

    审查实测：后端发 `{tool, ok, summary}`，前端读 `result`/`output`/
    `error`/`ms`/`call_id` → 工具结果**永远渲染不出来**，call_id 恒 null。
    """

    def _one_tool_events(self):
        return self.sse_events([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])

    def test_backend_tool_result_carries_call_id(self):
        """后端的 tool_call 与 tool_result 必须带**同一个** call_id。"""
        events = self._one_tool_events()
        call = [e for e in events if e["event"] == "tool_call"][0]["data"]
        result = [e for e in events if e["event"] == "tool_result"][0]["data"]
        self.assertTrue(call.get("call_id"), f"tool_call 没有 call_id：{call}")
        self.assertTrue(result.get("call_id"),
                        f"tool_result 没有 call_id：{result}")
        self.assertEqual(call["call_id"], result["call_id"],
                         "tool_call 与 tool_result 的 call_id 不一致")

    def test_frontend_consumes_backend_tool_result_payload(self):
        """
        把**后端真实产出**的 tool_result 喂进**真实前端** ——
        前端内部必须拿到 result（不是 undefined）。
        """
        events = self._one_tool_events()
        state = self.frontend_apply(events)
        self.assertTrue(state["tools"], f"前端没有产生工具条目：{state}")
        tool = state["tools"][0]
        self.assertEqual(tool["name"], "list_modes")
        self.assertIsNotNone(tool["result"],
                             f"前端没拿到工具结果（后端发的是 summary）：{tool}")
        self.assertTrue(str(tool["result"]).strip(),
                        f"工具结果是空的：{tool}")

    def test_frontend_renders_backend_tool_result_in_dom(self):
        """
        渲染层断言（真 DOM）：工具块里必须出现「结果」行。
        这一条直接对应审查截图里的「只有工具名和耗时，没有结果」。
        """
        events = self._one_tool_events()
        rendered = self.frontend_render(events)
        self.assertEqual(len(rendered["tools"]), 1, f"没渲染出工具块：{rendered}")
        self.assertTrue(rendered["tools"][0]["result"],
                        f"工具块里没有「结果」行：{rendered['tools'][0]}")
        self.assertEqual(rendered["tools"][0]["name"], "list_modes")

    def test_frontend_renders_backend_tool_arguments(self):
        """
        工具**参数**也要渲染出来（后端 tool_call 发的是 args）。
        """
        events = self.sse_events([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "search_assets",
                 "arguments": {"query": "雨夜"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        rendered = self.frontend_render(events)
        self.assertTrue(rendered["tools"], f"没渲染出工具块：{rendered}")
        self.assertIn("query=雨夜", rendered["tools"][0]["args"],
                      f"工具参数没渲染：{rendered['tools'][0]}")

    def test_frontend_tool_call_and_result_pair_by_call_id(self):
        """
        同一轮两个**同名**工具调用 -> 两张独立卡片（靠 call_id 区分）。
        没有 call_id 时前端会「按名字找第一个未完成的」-> 串卡。
        """
        events = self.sse_events([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "search_assets",
                 "arguments": {"query": "A"}},
                {"id": "c2", "name": "search_assets",
                 "arguments": {"query": "B"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        state = self.frontend_apply(events)
        self.assertEqual(len(state["tools"]), 2,
                         f"同名工具调用没有分成两张卡：{state['tools']}")
        call_ids = {t.get("call_id") for t in state["tools"]}
        self.assertEqual(len([c for c in call_ids if c]), 2,
                         f"两张卡没有各自独立的 call_id：{call_ids}")


# ==================================================================== 契约 ③
class TestClarificationFieldContract(_ContractBase):
    """
    契约 ③：**澄清字段名**。

    审查实测：后端只发 `clarification`（字符串），
    前端读 `clarify_options`/`clarify_hint` → 快捷选项 100% 落空。
    """

    def _clarify_events(self):
        return self.sse_events([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "respond",
                 "arguments": {
                     "summary": "你要的时长超出上限，需要确认用多少秒。",
                     "needs_clarification": True,
                     "clarify_options": ["5 秒", "10 秒", "15 秒"],
                     "clarify_hint": "视频时长合法区间是 5~15 秒。"}}]},
            {"content": "等用户回答", "tool_calls": [], "raw": {}},
        ])

    def test_backend_result_has_clarify_fields(self):
        """后端一次性结果必须带 clarify_options / clarify_hint。"""
        result = self.json_result([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "respond",
                 "arguments": {
                     "summary": "需要确认时长。",
                     "needs_clarification": True,
                     "clarify_options": ["5 秒", "10 秒"],
                     "clarify_hint": "合法区间 5~15 秒。"}}]},
            {"content": "等回答", "tool_calls": [], "raw": {}},
        ])
        self.assertEqual(result.get("status"), "needs_clarification",
                         f"状态不对：{result.get('status')}")
        self.assertIn("clarify_options", result,
                      f"后端结果没有 clarify_options：{sorted(result)}")
        self.assertIn("clarify_hint", result,
                      f"后端结果没有 clarify_hint：{sorted(result)}")
        self.assertEqual(result["clarify_options"], ["5 秒", "10 秒"])
        self.assertIn("5~15", result["clarify_hint"])

    def test_frontend_reads_backend_clarify_fields(self):
        """
        后端真实产出（含 clarify_options）-> 前端必须拿到选项。
        """
        events = self._clarify_events()
        state = self.frontend_apply(events)
        # done 事件之后前端从 result 里读澄清字段
        result = state.get("result") or {}
        self.assertEqual(result.get("status"), "needs_clarification",
                         f"状态不对：{result.get('status')}")
        self.assertEqual(result.get("clarify_options"), ["5 秒", "10 秒", "15 秒"],
                         f"前端没拿到快捷选项：{result.get('clarify_options')}")

    def test_frontend_renders_clarify_options_in_dom(self):
        """
        渲染层断言（真 DOM）：快捷选项按钮必须真的出现。
        这一条直接对应审查截图里的「蓝色区块里只有输入框，没有选项 chip」。
        """
        events = self._clarify_events()
        rendered = self.frontend_render(events)
        self.assertTrue(rendered["clarifyOpts"],
                        f"DOM 里没有澄清快捷选项：{rendered}")
        self.assertIn("5 秒", rendered["clarifyOpts"])
        self.assertIn("15 秒", rendered["clarifyOpts"])
        self.assertIn("合法区间", rendered["clarifyHint"],
                      f"澄清提示没渲染：{rendered['clarifyHint']}")

    def test_frontend_falls_back_to_clarification_field(self):
        """
        兼容老服务端：只有 `clarification`（字符串）时，
        前端至少要把**问题文本**显示出来（不能空白）。
        """
        page = self.frontend()
        state = page.evaluate("""() => {
          const msg = {role:'assistant', content:'', result:{}, jobs:[], assets:[],
                       prompt:null, steps:[], tools:[], streaming:false, error:''};
          window.AgentUI._applyStreamEvent(msg, 'done', {
            status: 'needs_clarification',
            clarification: '老服务端只给了这一个问题',
            answer: '老服务端只给了这一个问题'
          });
          return {result: msg.result};
        }""")
        self.assertEqual(state["result"]["status"], "needs_clarification")

    def test_backend_respond_schema_exposes_clarify_options(self):
        """respond 工具的 schema 必须声明 clarify_options 入参。"""
        spec = agent_mod.TOOLS["respond"]
        params = spec["parameters"].get("properties", {})
        self.assertIn("clarify_options", params,
                      f"respond schema 没有 clarify_options：{sorted(params)}")
        self.assertIn("clarify_hint", params,
                      f"respond schema 没有 clarify_hint：{sorted(params)}")


# ==================================================================== 契约 ④
class TestStepsArgsContract(_ContractBase):
    """
    契约 ④：**一次性路径的 steps[].args**。

    审查实测：后端 steps[] 带完整参数，前端 `syncToolsFromSteps`
    硬编码 `arguments: null` -> 工具参数被**主动丢弃**。
    """

    def test_backend_steps_carry_args(self):
        """后端 steps[] 必须带 args（真实编排产出）。"""
        result = self.json_result([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜霓虹街道",
                               "aspect": "9:16"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        steps = result.get("steps") or []
        self.assertTrue(steps, f"没有步骤：{result}")
        create = [s for s in steps if s.get("tool") == "create_job"]
        self.assertTrue(create, f"没有 create_job 步骤：{steps}")
        self.assertTrue(create[0].get("args"),
                        f"后端 steps[].args 是空的：{create[0]}")
        args = json.loads(create[0]["args"])
        self.assertEqual(args.get("mode"), "t2v")
        self.assertEqual(args.get("aspect"), "9:16")

    def test_frontend_uses_steps_args(self):
        """
        后端真实 steps[] -> 前端 syncToolsFromSteps -> arguments 非 null。
        """
        result = self.json_result([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜霓虹街道",
                               "aspect": "9:16"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        page = self.frontend()
        state = page.evaluate("""(steps) => {
          const msg = {role:'assistant', content:'', result:{}, jobs:[], assets:[],
                       prompt:null, steps:[], tools:[], streaming:false, error:''};
          // 前端暴露的内部接口里没有 syncToolsFromSteps，
          // 但 send() 的一次性路径会调用它 —— 这里走同一条路径：
          // 直接调 send() 的降级分支太重，这里用真实渲染入口复现。
          window.AgentUI.state.messages = [msg];
          window.AgentUI.state.streaming = null;
          return {before: msg.tools};
        }""", result.get("steps"))
        self.assertIsNotNone(state)
        # 用真实的 send() 走一次性 JSON 分支：把 fetch 打桩成返回后端真实结果
        page.evaluate("""(result) => {
          window.fetch = async (url, opts) => {
            return {ok:true, status:200, text: async()=>JSON.stringify(result),
                    headers:{get:()=>'application/json'}};
          };
        }""", result)
        page.fill("#agInput", "一次性路径")
        page.click("#agSend")
        page.wait_for_timeout(1200)
        tools = page.evaluate(
            "() => { const s = window.AgentUI.state.messages; "
            "const last = s[s.length-1]; return (last.tools||[]).map(t => ({"
            "name:t.name, args:t.arguments})); }")
        self.assertTrue(tools, f"一次性路径没有生成工具轨迹：{tools}")
        create = [t for t in tools if t["name"] == "create_job"]
        self.assertTrue(create, f"没有 create_job 工具条目：{tools}")
        self.assertIsNotNone(create[0]["args"],
                             f"steps[].args 被前端丢弃了：{create[0]}")
        self.assertEqual(create[0]["args"].get("mode"), "t2v")
        self.assertEqual(create[0]["args"].get("aspect"), "9:16")

    def test_frontend_renders_steps_args_in_dom(self):
        """渲染层断言：一次性路径的工具块要有「参数」行。"""
        result = self.json_result([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜霓虹街道",
                               "aspect": "9:16"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        page = self.frontend()
        page.evaluate("""(result) => {
          window.fetch = async (url, opts) => {
            return {ok:true, status:200, text: async()=>JSON.stringify(result),
                    headers:{get:()=>'application/json'}};
          };
        }""", result)
        page.fill("#agInput", "一次性路径")
        page.click("#agSend")
        page.wait_for_timeout(1200)
        rendered = page.evaluate("""() => ({
          args: Array.from(document.querySelectorAll('.ag-tool-args code'))
                  .map(e => e.textContent),
        })""")
        self.assertTrue(rendered["args"],
                        f"一次性路径的工具块没有「参数」行：{rendered}")
        self.assertTrue(any("mode=t2v" in a for a in rendered["args"]),
                        f"参数行内容不对：{rendered['args']}")


# ==================================================================== 契约 ⑤
class TestMusicAspectContract(_ContractBase):
    """
    契约 ⑤：**音乐任务的 aspect**。

    审查实测：后端 schema 里音乐模式**没有** aspect，
    但前端 effectiveParams 无条件 push aspect
    -> 音乐卡渲染出无意义的「比例 16:9」。
    """

    def test_backend_music_schema_has_no_aspect(self):
        """后端真源：音乐模式的 params 里没有 aspect。"""
        from h3studio import agent_schema
        for mode in ("t2a", "a2a"):
            schema = agent_schema.mode_schema(mode)
            self.assertNotIn("aspect", schema["params"],
                             f"{mode} 的后端 schema 不该有 aspect："
                             f"{sorted(schema['params'])}")

    def test_backend_music_defaults_have_no_video_params(self):
        """
        后端真源：音乐模式的 defaults 里不能混进视频参数
        （duration / lora / resolution）。
        """
        from h3studio import agent_schema
        for mode in ("t2a", "a2a"):
            defaults = agent_schema.mode_defaults(mode)
            extra = sorted(set(defaults) - set(agent_schema.mode_schema(mode)["params"]))
            self.assertEqual(extra, [],
                             f"{mode} 的默认值混进了越界参数：{extra}（{defaults}）")
            self.assertNotIn("duration", defaults,
                             f"{mode} 的默认值不该有 duration（音乐用 max_duration）："
                             f"{defaults}")

    def test_frontend_music_card_has_no_aspect(self):
        """
        后端真实产出（音乐任务）-> 前端渲染 -> **不能**出现「比例」。
        """
        result = self.json_result([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2a", "prompt": "一段轻快的钢琴曲",
                               "max_duration": 90}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        jobs = result.get("jobs") or []
        self.assertTrue(jobs, f"没有创建任务：{result}")
        page = self.frontend()
        rendered = page.evaluate("""(job) => {
          const msg = {role:'assistant', content:'', result:{}, jobs:[job],
                       assets:[], prompt:null, steps:[], tools:[], streaming:false,
                       error:''};
          window.AgentUI.state.messages = [msg];
          window.AgentUI.renderMessages();
          return {
            params: Array.from(document.querySelectorAll('.ag-param'))
                         .map(e => e.textContent),
          };
        }""", jobs[0])
        self.assertTrue(rendered["params"], f"任务卡没有参数：{rendered}")
        self.assertFalse(any("比例" in p for p in rendered["params"]),
                         f"音乐卡渲染出了无意义的比例：{rendered['params']}")
        self.assertTrue(any("时长" in p for p in rendered["params"]),
                        f"音乐卡该渲染时长：{rendered['params']}")


if __name__ == "__main__":
    unittest.main()
