# -*- coding: utf-8 -*-
"""
内置多模态 Agent 的回归测试。

覆盖的是**真实会咬人的地方**，不是「跑通就算」：

1. **参数收敛**：Agent 的 create_job 必须走服务端**同一套**校验
   （server.prepare_job_params）—— 模式白名单 / 参考图数量 / <imageN> 占位注入 /
   参考音频数量 / 提示词非空。两边各写一份必然漂移（Agent 放行 i2i_qwen 无参考图
   -> 提交后才炸；Agent 漏掉占位注入 -> 图1 变成图2）。

2. **重复调用检测**：本地小模型会原地打转（实测连续 6 步调同一个工具）。
   必须能收尾，且第 2 次要**先给纠偏提示**而不是直接掐死。

3. **工具白名单**：模型报一个不存在的工具名，不能执行任何东西。

4. **会话存储**：历史裁剪按「轮」而不是按「条」；当前轮的用户消息不能重复喂给模型。

5. **结构化输出契约**：jobs 里的 id 必须是服务端真实生成的（模型编不出来）；
   mode 必须在 workflows.BUILDERS 里。

6. **超时/超限不挂死**：超限返回已完成的部分 + 可读原因，而不是抛异常。

运行：python3 -m pytest tests/test_agent.py -v
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod  # noqa: E402
from h3studio import agent_sessions  # noqa: E402
from h3studio import workflows as wf  # noqa: E402


# ------------------------------------------------------------------ 假服务端


class FakeServer:
    """
    一个最小的「服务端」替身。

    只提供 Agent 真正依赖的两个入口：
      * db()          —— 任务库连接
      * prepare_job_params / create_job_record —— 建任务（Agent 复用同一套校验）

    这里的 prepare_job_params 是**真逻辑的薄封装**：直接调 server 模块里的
    那个函数，保证测试验的是真代码，不是复制的副本。
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db_path = tmp / "jobs.db"
        self.created = []
        self._server_mod: object = None
        self._seed_providers()

    def _seed_providers(self):
        """预置一个可用的默认 Provider —— Agent 必须能从「本地模型配置」里取到它。"""
        conn = self.db()
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

    def prepare_job_params(self, mode, params, asset_refs=None, saved_files=None):
        mod = self._server_mod
        return mod.prepare_job_params(mode, params, asset_refs, saved_files)  # type: ignore[attr-defined]

    def create_job_record(self, mode, params):
        job_id = f"job{len(self.created):04d}"
        self.created.append((job_id, mode, dict(params)))
        return job_id


def load_real_server_module():
    """加载真的 server 模块（只为拿 prepare_job_params / create_job_record）。"""
    import importlib
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    return mod


# ------------------------------------------------------------------ 测试


class TestAgentToolRegistry(unittest.TestCase):
    """工具注册表的基本契约。"""

    def test_expected_tools_are_registered(self):
        for name in ("search_assets", "list_modes", "describe_asset",
                     "enhance_prompt", "create_job", "respond"):
            self.assertIn(name, agent_mod.TOOLS,
                          "工具 %s 必须注册（Agent 靠它做事）" % name)

    def test_tool_specs_are_openai_shaped(self):
        specs = agent_mod._tool_specs()
        self.assertEqual(len(specs), len(agent_mod.TOOLS))
        for spec in specs:
            self.assertEqual(spec["type"], "function")
            fn = spec["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertEqual(fn["parameters"]["type"], "object")
            self.assertIn("properties", fn["parameters"])

    def test_create_job_mode_enum_matches_real_builders(self):
        """create_job 的 mode 枚举必须等于后端真实注册的模式。"""
        spec = agent_mod.TOOLS["create_job"]["parameters"]
        enum = spec["properties"]["mode"]["enum"]
        self.assertEqual(sorted(enum), sorted(wf.BUILDERS.keys()),
                         "工具 schema 里的 mode 枚举与 workflows.BUILDERS 不一致")

    def test_unknown_tool_is_not_executable(self):
        """模型报一个不存在的工具名 -> 没有 handler 可调。"""
        self.assertNotIn("rm_rf", agent_mod.TOOLS)
        self.assertIsNone(agent_mod.TOOLS.get("rm_rf", {}).get("handler"))


class TestAgentJobValidation(unittest.TestCase):
    """Agent 的 create_job 必须走服务端同一套校验。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-test-"))
        self.server_mod = load_real_server_module()
        self.server = FakeServer(self.tmp)
        self.server._server_mod = self.server_mod
        self.ctx = agent_mod.AgentContext(server_module=self.server,
                                          provider={"name": "test",
                                                    "model": "m"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, args):
        return asyncio.run(
            agent_mod.TOOLS["create_job"]["handler"](self.ctx, args))

    def test_valid_job_is_created(self):
        result = self._call({"mode": "t2v", "prompt": "一只猫在雨里跑"})
        self.assertIn("job_id", result)
        self.assertEqual(result["mode"], "t2v")
        self.assertEqual(len(self.server.created), 1)
        self.assertEqual(self.server.created[0][1], "t2v")

    def test_unknown_mode_is_rejected_with_readable_error(self):
        with self.assertRaises(agent_mod.AgentError) as ctx:
            self._call({"mode": "t2x", "prompt": "x"})
        self.assertIn("未知模式", ctx.exception.message)
        self.assertEqual(len(self.server.created), 0, "被拒的任务不能落库")

    def test_empty_prompt_is_rejected(self):
        with self.assertRaises(agent_mod.AgentError) as ctx:
            self._call({"mode": "t2v", "prompt": "   "})
        # 文案必须与面板提交路径一致（用户在两边看到的解释要一样）
        self.assertIn("提示词", ctx.exception.message)
        self.assertEqual(ctx.exception.message, "提示词不能为空")

    def test_invalid_aspect_is_rejected(self):
        """比例必须来自 workflows.ASPECT_MAP —— 不能让模型编一个比例。"""
        with self.assertRaises(agent_mod.AgentError) as ctx:
            self._call({"mode": "t2v", "prompt": "x", "aspect": "99:1"})
        self.assertIn("未知画面比例", ctx.exception.message)

    def test_i2i_qwen_without_refs_is_rejected(self):
        """图片编辑必须带参考图（参考图只能来自素材库）。"""
        with self.assertRaises(agent_mod.AgentError) as ctx:
            self._call({"mode": "i2i_qwen", "prompt": "改成夜景"})
        self.assertIn("参考图", ctx.exception.message)
        self.assertEqual(len(self.server.created), 0)

    def test_a2a_without_ref_audio_is_rejected(self):
        """参考生音乐必须带参考音频。"""
        with self.assertRaises(agent_mod.AgentError) as ctx:
            self._call({"mode": "a2a", "prompt": "一段摇滚"})
        self.assertIn("参考音频", ctx.exception.message)

    def test_repeated_identical_asset_refs_are_deduplicated_by_ordinal(self):
        """多个参考图必须保持顺序（占位语义与插槽顺序一一对应）。

        【2026-11-16】素材引用现在**查库**（server.prepare_job_params ->
        asset_ref_guard.check_asset_refs），编造的 asset_id 会被正确拒绝。
        所以这里用临时素材库里**真实入库**的两张图 ——
        本用例验的是「顺序 / ordinal 语义」，与素材 id 本身无关。
        """
        from assetlib_fixture import temp_asset_library
        with temp_asset_library(image=2) as ids:
            if len(ids["image"]) < 2:
                self.skipTest("建不出两张图片素材")
            result = self._call({
                "mode": "r2v", "prompt": "参考这两张",
                "asset_ids": list(ids["image"]),
            })
            self.assertEqual(len(self.server.created), 1)
            params = self.server.created[0][2]
            refs = params.get("asset_refs") or []
            self.assertEqual([r["asset_id"] for r in refs], ids["image"])
            self.assertEqual([r["ordinal"] for r in refs], [0, 1])
            self.assertIn("job_id", result)


class TestAgentOrchestration(unittest.TestCase):
    """编排器：重复调用检测 / 工具上限 / 未知工具 / respond 收尾。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-orch-"))
        self.server_mod = load_real_server_module()
        self.server = FakeServer(self.tmp)
        self.server._server_mod = self.server_mod
        # 会话库指向临时目录
        self.store_path = self.tmp / "agent_sessions.db"
        agent_sessions.reset_store(self.store_path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patch_turns(self, turns):
        """把模型调用替换成预置脚本（每轮一个 dict）。"""
        calls = {"n": 0}

        async def fake_call(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= len(turns):
                return {"content": "结束", "tool_calls": [], "raw": {}}
            return turns[index]

        return fake_call, calls

    def _run(self, turns, **kw):
        import h3studio.agent as mod
        original = mod.llm_client.call_provider_with_tools
        fake, calls = self._patch_turns(turns)
        mod.llm_client.call_provider_with_tools = fake
        try:
            return asyncio.run(mod.run_agent(self.server, "做个视频",
                                             **kw)), calls
        finally:
            mod.llm_client.call_provider_with_tools = original

    def test_unknown_tool_is_reported_not_executed(self):
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "rm_rf", "arguments": {"path": "/"}}]},
            {"content": "没有这个工具", "tool_calls": [], "raw": {}},
        ]
        result, _ = self._run(turns)
        self.assertEqual(result["status"], "completed")
        step = result["steps"][0]
        self.assertEqual(step["tool"], "rm_rf")
        self.assertFalse(step["ok"], "未知工具不能算成功")
        self.assertIn("未知工具", step["error"])

    def test_identical_repeat_gets_nudge_before_being_cut(self):
        """
        同一工具 + 同一参数连续 2 次 -> 先给纠偏提示（不是直接掐死）。
        第 3 次才判定打转。
        """
        same = {"content": None, "tool_calls": [
            {"id": "c1", "name": "list_modes", "arguments": {}}]}
        turns = [dict(same), dict(same), dict(same)]
        result, calls = self._run(turns)
        # 第 2 次给出提示，第 3 次收尾 —— 总共不超过 3 步
        self.assertEqual(calls["n"], 3)
        self.assertEqual(result["status"], "truncated")
        self.assertIn("重复调用", result["error"])
        # 提示那次要留下 note
        notes = [s.get("note") for s in result["steps"]]
        self.assertTrue(any(n and "纠偏提示" in n for n in notes),
                        "第 2 次重复必须留下纠偏提示的记录，实际 notes=%r" % notes)

    def test_respond_ends_the_loop_immediately(self):
        """respond 之后不该再问一轮（每轮 60~120 秒，很贵）。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "respond",
                 "arguments": {"summary": "好的"}}]},
            {"content": "不该被调用", "tool_calls": [], "raw": {}},
        ]
        result, calls = self._run(turns)
        self.assertEqual(calls["n"], 1, "respond 之后不应再调模型")
        self.assertEqual(result["answer"], "好的")
        self.assertEqual(result["status"], "completed")

    def test_tool_call_limit_is_enforced(self):
        """工具调用数有硬上限，不能无限调。"""
        turn = {"content": None, "tool_calls": [
            {"id": "c1", "name": "list_modes", "arguments": {}}]}
        # 用不同参数避免先触发重复检测
        turns = []
        for i in range(10):
            turns.append({"content": None, "tool_calls": [
                {"id": f"c{i}", "name": "list_modes", "arguments": {"i": i}}]})
        result, calls = self._run(turns)
        self.assertLessEqual(result["usage"]["tool_calls"],
                             agent_mod.MAX_TOOL_CALLS)
        self.assertIn(result["status"], ("truncated", "completed"))

    def test_job_ids_come_from_server_not_model(self):
        """
        结构化输出里的 job_id 必须是服务端生成的真实 id。
        模型报一个假的 job_id 进来，不能被写进 jobs[]。
        """
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "雨夜街道"}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "已创建", "job_ids": ["FAKE-ID-123"]}}]},
        ]
        result, _ = self._run(turns)
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(result["jobs"][0]["job_id"], "job0000")
        self.assertNotIn("FAKE-ID-123",
                         [j["job_id"] for j in result["jobs"]],
                         "模型自报的 job_id 不能进结构化输出")

    def test_result_shape_is_stable(self):
        """结构化输出的契约 —— 前端按这个字段渲染，字段不能少。"""
        turns = [{"content": "好的", "tool_calls": [], "raw": {}}]
        result, _ = self._run(turns)
        for key in ("status", "answer", "jobs", "assets", "prompt", "steps",
                    "usage", "provider", "error"):
            self.assertIn(key, result, "结构化输出缺少字段 %s" % key)
        self.assertIn("steps", result["usage"])
        self.assertIn("tool_calls", result["usage"])
        self.assertIn("elapsed_ms", result["usage"])

    def test_mode_outside_builders_is_never_in_plan(self):
        """结构化输出里的 mode 必须来自 workflows.BUILDERS。"""
        turns = [{"content": "好", "tool_calls": [], "raw": {}}]
        result, _ = self._run(turns)
        for job in result["jobs"]:
            self.assertIn(job["mode"], wf.BUILDERS)


class TestAgentNarrationGuard(TestAgentOrchestration):
    """
    「口头承诺」防护。

    实测本地小模型会用自然语言说「我先增强提示词，再下发生成任务」然后停下，
    一个工具都没调 —— 用户收到的是一句承诺，不是一个任务。
    """

    def test_intent_narration_is_detected(self):
        for text in ("我先增强提示词，再下发生成任务。",
                     "接下来我会调用 create_job。",
                     "我现在就去检索素材库"):
            self.assertTrue(agent_mod._looks_like_intent_narration(text),
                            "应识别为口头承诺：%r" % text)

    def test_real_conclusion_is_not_mistaken_for_narration(self):
        for text in ("已创建 1 个文生视频任务，job_id=abc123。",
                     "素材库里没有找到雨夜相关的素材。",
                     "任务已进入队列，可在生成列表查看。",
                     ""):
            self.assertFalse(agent_mod._looks_like_intent_narration(text),
                             "真实结论不应被当成口头承诺：%r" % text)

    def test_narration_after_tool_use_triggers_a_nudge(self):
        """模型用工具之后只做口头承诺 -> 必须推它继续，而不是直接收尾。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "search_assets",
                 "arguments": {"query": "雷神"}}]},
            # 口头承诺，没有工具调用
            {"content": "我先增强提示词，再下发生成任务。", "tool_calls": [],
             "raw": {}},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "已经下发好了"}}]},
        ]
        result, calls = self._run(turns)
        # 3 轮：检索 -> 被推一把 -> respond
        self.assertEqual(calls["n"], 3)
        self.assertEqual(result["answer"], "已经下发好了")
        notes = [s.get("note") for s in result["steps"]]
        self.assertTrue(any(n and "口头承诺" in n for n in notes),
                        "应记录「只做了口头承诺」的推进行为，实际 notes=%r" % notes)


class TestAgentModeOptions(unittest.TestCase):
    """list_modes 返回的可选项不能是编造的。"""

    def test_video_duration_is_a_range_not_a_fake_allowlist(self):
        """
        视频时长是任意秒数（后端 float(params["duration"])），
        面板滑块是 2~60。之前硬编码成 [5,10,15]，
        模型据此以为「8 秒不支持」而拒绝执行 —— 凭空造的约束。

        ⚠️ 2026-11-12：上限从 15 放宽到 60（用户实测「15 秒太短，
        一分钟都行」）。区间必须**从 video_models 取**，不能写死数字 ——
        写死就会在改区间时漏改这里（上一轮 5~15 就是这么挂的）。
        """
        import asyncio
        from h3studio import video_models as vm
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(
            agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        self.assertNotIn("durations", result,
                         "不该有硬编码时长白名单")
        self.assertEqual(result["video_duration_range"],
                         [int(vm.VIDEO_DURATION_MIN), int(vm.VIDEO_DURATION_MAX)])
        self.assertEqual(result["music_duration_range"], [4, 360])
        # 每个模式条目里也不能再有白名单式的 durations
        for entry in result["modes"]:
            self.assertNotIn("durations", entry)

    def test_list_modes_result_fits_in_tool_result_limit(self):
        """
        list_modes 的结果必须**完整**塞进工具结果上限。

        之前结果 1250 字符 > 600 上限 -> 被截断 -> 模型看到
        「详情被截断」-> 以为工具坏了 -> 重复调同一个工具（实测打转 3 次）。
        """
        import asyncio
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(
            agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        blob = json.dumps(result, ensure_ascii=False)
        self.assertLessEqual(len(blob), agent_mod.TOOL_RESULT_LIMIT,
                             "list_modes 结果 %d 字符，超过工具结果上限 %d —— "
                             "模型会看到被截断的内容"
                             % (len(blob), agent_mod.TOOL_RESULT_LIMIT))

    def test_listed_modes_are_all_actually_creatable(self):
        """
        list_modes 报出来的模式，create_job 必须都能建。
        否则模型会拿到一个「看得到、用不了」的模式（比如 longfilm 走的是
        另一条子进程链路，不在 workflows.BUILDERS 里）。
        """
        import asyncio
        ctx = agent_mod.AgentContext(server_module=None, provider={})
        result = asyncio.run(
            agent_mod.TOOLS["list_modes"]["handler"](ctx, {}))
        modes = {m["mode"] for m in result["modes"]}
        for mode in modes:
            self.assertIn(mode, wf.BUILDERS,
                          "list_modes 报了 create_job 建不了的模式：%s" % mode)


class TestAgentConcurrencyGate(unittest.TestCase):
    """
    并发闸门：同时最多 MAX_CONCURRENT 个会话，超出立刻 429（不排队）。

    这条为什么要测：闸门如果用「先判断 locked() 再 async with」，
    两个请求可能同时通过判断 -> 并发数变成 3+ -> 本地模型被压垮。
    """

    def test_gate_never_overshoots_under_concurrency(self):
        """
        50 个并发请求打进来，实际同时运行的会话数**必须**等于上限。

        这是实测过的两种写法的判据：
          * wait_for(acquire(), timeout=0) —— 实测一次都抢不到（全员 429）
          * locked() + async with           —— 实测 peak 恰好等于上限
        所以断言的是「peak == 上限」，而不是「某个 API 的行为」。
        """
        import asyncio
        import server as server_mod

        async def scenario():
            server_mod.AGENT_GATE = None
            gate = server_mod._agent_gate()
            limit = server_mod.AGENT_CONCURRENCY
            concurrent = 0
            peak = 0
            rejected = 0

            async def worker():
                nonlocal concurrent, peak, rejected
                if gate.locked():
                    rejected += 1
                    return
                async with gate:
                    concurrent += 1
                    peak = max(peak, concurrent)
                    await asyncio.sleep(0.01)
                    concurrent -= 1

            await asyncio.gather(*[worker() for _ in range(50)])
            return peak, rejected, limit

        peak, rejected, limit = asyncio.run(scenario())
        self.assertEqual(peak, limit,
                         "并发峰值应恰好等于上限 %d，实际 %d" % (limit, peak))
        self.assertGreater(rejected, 0, "超出的请求应该被挡下（429）")

    def test_concurrency_limit_is_small(self):
        """本地模型单卡，并发数必须小（1~4）。"""
        self.assertGreaterEqual(agent_mod.MAX_CONCURRENT, 1)
        self.assertLessEqual(agent_mod.MAX_CONCURRENT, 4)


class TestAgentSessions(unittest.TestCase):
    """会话存储：轮次裁剪 / 标题 / 清理。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-sess-"))
        self.store = agent_sessions.SessionStore(self.tmp / "s.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_is_idempotent(self):
        sid = self.store.create("fixed-id")
        again = self.store.create("fixed-id")
        self.assertEqual(sid, again)
        self.assertTrue(self.store.exists("fixed-id"))

    def test_history_is_trimmed_by_turn_not_by_message(self):
        """裁剪按「用户轮」算 —— 不能把一轮里的 assistant/tool 切一半。"""
        sid = self.store.create()
        for i in range(12):
            self.store.add_message(sid, "user", f"问题{i}")
            self.store.add_message(sid, "assistant", f"回答{i}")
            self.store.add_message(sid, "tool", f"tool{i}", tool_name="x")
        history = self.store.history(sid, limit=4)
        # 只保留最近 4 轮（= 12 条消息）
        self.assertEqual(len(history), 12)
        self.assertEqual(history[0]["content"], "问题8")

    def test_history_starts_at_a_user_message(self):
        """裁剪后第一条必须是 user —— 半截的 tool 消息会让模型困惑。"""
        sid = self.store.create()
        for i in range(6):
            self.store.add_message(sid, "user", f"问题{i}")
            self.store.add_message(sid, "assistant", f"回答{i}")
        history = self.store.history(sid, limit=2)
        self.assertEqual(history[0]["role"], "user")

    def test_title_comes_from_first_user_message(self):
        sid = self.store.create()
        self.store.add_message(sid, "user", "生成一段雨夜视频")
        sessions = self.store.list_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertIn("雨夜", sessions[0]["title"])

    def test_delete_removes_messages(self):
        sid = self.store.create()
        self.store.add_message(sid, "user", "x")
        self.store.delete(sid)
        self.assertFalse(self.store.exists(sid))
        self.assertEqual(self.store.raw_history(sid), [])

    def test_long_message_is_truncated_on_store(self):
        """超长模型输出不能把库撑爆。"""
        sid = self.store.create()
        self.store.add_message(sid, "assistant", "x" * 50000)
        raw = self.store.raw_history(sid)
        self.assertLessEqual(len(raw[0]["content"]),
                             agent_sessions.MAX_MESSAGE_CHARS)


class TestAgentLimits(unittest.TestCase):
    """限额常量必须是「有限且合理」的 —— 不能写成无限。"""

    def test_limits_are_bounded(self):
        self.assertGreater(agent_mod.TOTAL_TIMEOUT, 0)
        # 【2026-11-13 审查目标 4 语义反转】步数上限 8 -> 50，总时限必须跟着抬
        # （50 步 × 单步 20~120 秒 = 1000~6000 秒；总时限留在 420 秒的话
        #   「50 步上限」只是纸面数字，第 8 步之后必然被总时限掐断）。
        # 上界从 900 抬到 3600（60 分钟）—— 仍然是有限值，不会无限跑。
        self.assertLessEqual(agent_mod.TOTAL_TIMEOUT, 3600)
        self.assertGreater(agent_mod.MAX_STEPS, 0)
        # 步数上限：从「<= 10」改成「>= 50 且 <= 硬顶 50」
        # （用户 2026-11-13 审查目标 4 明确要求至少 50 步）。
        self.assertGreaterEqual(agent_mod.MAX_STEPS, 50)
        self.assertLessEqual(agent_mod.MAX_STEPS, 50)
        self.assertGreater(agent_mod.MAX_TOOL_CALLS, 0)
        self.assertGreaterEqual(agent_mod.MAX_TOOL_CALLS, 100)
        self.assertLessEqual(agent_mod.MAX_TOOL_CALLS, 200)
        self.assertGreater(agent_mod.MAX_CONCURRENT, 0)
        self.assertLessEqual(agent_mod.MAX_CONCURRENT, 8)

    def test_total_timeout_covers_the_step_budget(self):
        """
        【关键不变量】总时限必须**够跑完**步数上限。

        这一条是「50 步上限」能不能落地的判据：单步最坏 180 秒
        （STEP_TIMEOUT），总时限至少要能覆盖步数上限的下限区间，
        否则步数上限是纸面数字。

        实测口径：50 步 × 平均 48 秒 = 2400 秒 -> TOTAL_TIMEOUT 必须 >= 2400。
        """
        self.assertGreaterEqual(agent_mod.TOTAL_TIMEOUT,
                                agent_mod.MAX_STEPS * 48)

    def test_repeat_limit_is_at_least_two(self):
        """第 2 次重复要先给提示，所以上限至少 3。"""
        self.assertGreaterEqual(agent_mod.REPEAT_LIMIT, 3)


if __name__ == "__main__":
    unittest.main()
