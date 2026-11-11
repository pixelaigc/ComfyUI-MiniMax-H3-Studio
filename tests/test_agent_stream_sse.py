# -*- coding: utf-8 -*-
"""
Agent 流式接口（SSE）的**端到端**测试。

为什么单独一个文件：
  tests/test_agent_backend_enh.py 测的是 **Agent 层**的事件回调
  （on_event 收到什么）。本文件测的是 **HTTP 层**：
  POST /api/agent/chat?stream=1 真的返回 text/event-stream，
  事件名 / 顺序 / data 形状都对。

这两层必须分开测 —— Agent 层对了不代表 SSE 写出去对了
（事件名映射、JSON 序列化、连接关闭都是 HTTP 层的责任）。

运行：python3 -m pytest tests/test_agent_stream_sse.py -v
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

from h3studio import agent as agent_mod  # noqa: E402
from h3studio import agent_sessions       # noqa: E402


#: 真的 server.prepare_job_params（在下面替换 server 模块的属性之前抓住它）。
#:
#: 【为什么要这样】本文件把 srv.prepare_job_params 换成假服务端的版本，
#: 而假服务端又需要调真实现 —— 如果假实现回头调 srv.prepare_job_params，
#: 就是无限递归（实测 maximum recursion depth exceeded）。
#: 所以在替换**之前**把真函数存成模块级变量。
_REAL_PREPARE_JOB_PARAMS = None


def _fake_server(tmp: Path):
    """一个最小服务端替身（与 test_agent_backend_enh.py 同款，但独立避免耦合）。"""
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
            # 注意：这里**不能**回调 srv.prepare_job_params ——
            # 本文件会把 srv.prepare_job_params 替换成本方法，
            # 回调就是无限递归（实测报 maximum recursion depth exceeded）。
            # 所以在 import server 之前先把真函数存下来。
            return _REAL_PREPARE_JOB_PARAMS(mode, params, asset_refs,
                                             saved_files)

        def create_job_record(self, mode, params):
            job_id = f"job{len(self.created):04d}"
            self.created.append((job_id, mode, dict(params)))
            return job_id

    return FakeServer()


class TestAgentStreamSSE(unittest.TestCase):
    """SSE 端到端：真的起 aiohttp app，真的 POST，真的读事件流。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-sse-"))
        agent_sessions.reset_store(self.tmp / "agent_sessions.db")
        self.server = _fake_server(self.tmp)
        self._patched = False

    def tearDown(self):
        if self._patched:
            original = agent_mod.llm_client.call_provider_with_tools
            agent_mod.llm_client.call_provider_with_tools = original
        # 还原被替换的 server 模块属性（见 _make_app 的说明）——
        # 不还原会污染后面所有 `import server` 的用例。
        srv = getattr(self, "_patched_module", None)
        if srv is not None:
            for name, real in getattr(self, "_orig_attrs", {}).items():
                setattr(srv, name, real)
            self._patched_module = None
            self._orig_attrs = {}
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_model(self, turns):
        """把模型调用替换成预置脚本。"""
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

    def _collect_sse(self, app, payload: dict, path: str = "/api/agent/chat"):
        """真的起服务 + POST + 读事件流。返回 (events, raw_text, status)。"""
        from aiohttp.test_utils import TestClient, TestServer

        async def _run():
            # TestServer 直接吃 Application（不需要自己建 AppRunner）
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            events = []
            raw = ""
            status = 0
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
            return events, raw, status

        return asyncio.run(_run())

    def _make_app(self):
        """用真的 server 模块建 app（路由是生产代码）。"""
        global _REAL_PREPARE_JOB_PARAMS
        import importlib
        sys.modules.pop("server", None)
        srv = importlib.import_module("server")
        # 抓住真实现（必须在替换之前）
        if _REAL_PREPARE_JOB_PARAMS is None:
            _REAL_PREPARE_JOB_PARAMS = srv.prepare_job_params
        # 把 server 模块的 db/建任务函数指向测试用的假服务端
        #
        # 【必须还原 —— 2026-11-11 修】
        # 这三个属性是**替换**共享模块 server 的属性，不还原的话
        # 假服务端的 db 会一直留在 server 模块里 → 后面任何
        # `import server` 拿到的都是被污染的模块。
        # 实测后果（合并时踩到）：
        #   tests/test_prompt_history.py::TestPromptHistoryApi.setUpClass
        #   调 server_mod.init_db() → 走到 FakeServer.db → 连的是
        #   FakeServer 自己的临时目录 → sqlite3.OperationalError:
        #   unable to open database file → 该文件 10 个用例全 ERROR。
        #
        # 真实原因不是产品缺陷，是**测试间的全局状态污染**。
        # 修法：tearDownClass 里把三个属性还原成模块原函数。
        self._patched_module = srv
        self._orig_attrs = {
            "db": srv.db,
            "prepare_job_params": srv.prepare_job_params,
            "create_job_record": srv.create_job_record,
        }
        srv.db = self.server.db
        srv.prepare_job_params = self.server.prepare_job_params
        srv.create_job_record = self.server.create_job_record
        srv.agent_sessions.reset_store(self.tmp / "agent_sessions.db")
        return srv.make_app(), srv

    # ------------------------------------------------------------------

    def test_stream_returns_sse_content_type(self):
        self._patch_model([{"content": "你好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, raw, status = self._collect_sse(
            app, {"message": "你好", "stream": 1})
        self.assertEqual(status, 200)
        self.assertIn("event:", raw)
        self.assertTrue(events, "SSE 必须有事件")

    def test_event_names_and_order(self):
        """token / tool_call / tool_result / job / done 的名字与顺序。"""
        self._patch_model([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜"}}]},
            {"content": "已创建任务。", "tool_calls": [], "raw": {}},
        ])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "做个视频", "stream": 1})
        names = [e["event"] for e in events]
        self.assertIn("tool_call", names)
        self.assertIn("tool_result", names)
        self.assertIn("job", names)
        self.assertIn("token", names)
        self.assertIn("done", names)
        # tool_call 早于 tool_result
        self.assertLess(names.index("tool_call"), names.index("tool_result"))
        # job 在 create_job 之后（tool_call 之后）
        self.assertGreater(names.index("job"), names.index("tool_call"))
        # token 早于 done
        self.assertLess(names.index("token"), names.index("done"))
        # 收尾：done 之后是 result（向后兼容基线前端）
        self.assertEqual(names[-1], "result")

    def test_event_data_has_type_field(self):
        """data 里必须带 type 字段（前端可统一 onmessage 分派）。"""
        self._patch_model([{"content": "好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "你好", "stream": 1})
        for item in events:
            self.assertIn("type", item["data"], item["event"])
            self.assertEqual(item["data"]["type"], item["event"])

    def test_tool_call_event_carries_name_and_args(self):
        self._patch_model([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "search_assets",
                 "arguments": {"query": "雷神"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "找雷神", "stream": 1})
        call = [e for e in events if e["event"] == "tool_call"][0]
        self.assertEqual(call["data"]["tool"], "search_assets")
        self.assertEqual(call["data"]["args"]["query"], "雷神")

    def test_tool_result_event_carries_ok_and_summary(self):
        self._patch_model([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "列模式", "stream": 1})
        result = [e for e in events if e["event"] == "tool_result"][0]
        self.assertIn("ok", result["data"])
        self.assertTrue(result["data"]["ok"])
        self.assertTrue(result["data"]["summary"])

    def test_job_event_has_pollable_card_data(self):
        """job 事件必须带前端能拿去轮询的字段。"""
        self._patch_model([
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜",
                               "aspect": "9:16", "duration": 8}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "做个视频", "stream": 1})
        job = [e for e in events if e["event"] == "job"][0]["data"]
        for key in ("job_id", "mode", "status", "params"):
            self.assertIn(key, job)
        self.assertEqual(job["mode"], "t2v")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["params"]["aspect"], "9:16")
        # job_id 必须与服务端真实创建的 id 一致
        self.assertEqual(job["job_id"], self.server.created[0][0])

    def test_done_event_carries_answer_and_status(self):
        self._patch_model([{"content": "已完成。", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "你好", "stream": 1})
        done = [e for e in events if e["event"] == "done"][0]["data"]
        self.assertIn("已完成", done["answer"])
        self.assertEqual(done["status"], "completed")

    def test_result_event_matches_non_stream_shape(self):
        """result 事件的结构必须与一次性接口一致（向后兼容）。"""
        self._patch_model([{"content": "已完成。", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "你好", "stream": 1})
        result = [e for e in events if e["event"] == "result"][0]["data"]
        for key in ("session_id", "status", "answer", "jobs", "assets",
                    "prompt", "steps", "usage", "provider"):
            self.assertIn(key, result, key)

    def test_no_tool_call_error_event(self):
        """模型只说不做 -> 流里必须有 error 事件（不能静默结束）。"""
        self._patch_model([
            {"content": "我先增强提示词，再下发任务。",
             "tool_calls": [], "raw": {}},
            {"content": "我先增强提示词，再下发任务。",
             "tool_calls": [], "raw": {}},
        ])
        app, _ = self._make_app()
        events, _, status = self._collect_sse(
            app, {"message": "做个视频", "stream": 1})
        self.assertEqual(status, 200)
        errors = [e for e in events if e["event"] == "error"]
        self.assertTrue(errors, "必须发 error 事件")
        self.assertEqual(errors[0]["data"]["code"], "no_tool_call")
        # 没有 result（失败链路不发 result）
        self.assertNotIn("result", [e["event"] for e in events])

    def test_dedicated_stream_endpoint_same_contract(self):
        """/api/agent/chat/stream 与 stream=1 必须同契约。"""
        self._patch_model([{"content": "好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, _, _ = self._collect_sse(
            app, {"message": "你好"}, path="/api/agent/chat/stream")
        names = [e["event"] for e in events]
        self.assertIn("token", names)
        self.assertIn("done", names)
        self.assertEqual(names[-1], "result")

    def test_non_stream_endpoint_returns_json(self):
        """不加 stream=1 时必须返回普通 JSON（不能变成 SSE）。"""
        self._patch_model([{"content": "好", "tool_calls": [], "raw": {}}])
        app, _ = self._make_app()
        events, raw, status = self._collect_sse(
            app, {"message": "你好"})
        self.assertEqual(status, 200)
        self.assertNotIn("event:", raw)
        body = json.loads(raw)
        self.assertEqual(body["status"], "completed")


if __name__ == "__main__":
    unittest.main()
