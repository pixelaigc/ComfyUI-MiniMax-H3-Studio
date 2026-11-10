# -*- coding: utf-8 -*-
"""
A1 遗留项的专项测试。

覆盖 5 条：
  1. 真正的 token 级增量流式（不是整段全量重渲染）
  2. 任务卡轮询有终止条件 + SSE 有 AbortController
  3. recall(limit<0) 语义统一（<=0 -> 返回全部）
  4. steps[] 两种形态在 schema 层强制统一
  5. 交付脚本断言前端资源全部加载完
"""
import inspect
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h3studio import agent, agent_memory  # noqa: E402


# ------------------------------------------------------------------ 1. token 级流式


class TestTokenLevelStreaming(unittest.TestCase):
    """真实 token 级增量（不是整段全量重渲染）。"""

    def test_llm_exposes_streaming_entry_point(self):
        """llm 层必须有流式入口，且带 on_token 回调。"""
        from h3studio import llm
        self.assertTrue(hasattr(llm, "call_provider_with_tools_stream"),
                        "llm 层缺 call_provider_with_tools_stream")
        sig = inspect.signature(llm.call_provider_with_tools_stream)
        self.assertIn("on_token", sig.parameters,
                      "流式入口必须有 on_token 回调参数")

    def test_agent_wires_on_token_to_token_event(self):
        """Agent 必须把 on_token 接到 `_event("token", ...)` 上。"""
        src = inspect.getsource(agent.run_agent)
        self.assertIn("call_provider_with_tools_stream", src,
                      "run_agent 走的是非流式入口 -> 拿不到 token 增量")
        self.assertIn("on_token", src,
                      "run_agent 没有把 on_token 接进事件回调")
        # 增量事件必须带 delta 标记（前端据此走追加而不是替换）
        self.assertRegex(src, r'_event\("token",\s*\{"text":\s*piece,\s*"delta":\s*True\}\)',
                         "token 增量事件必须带 delta=True 标记")

    def test_streamed_content_is_not_duplicated(self):
        """流式推过增量后，不能再补一次整段（否则前端重复渲染）。"""
        src = inspect.getsource(agent.run_agent)
        self.assertIn('_streamed', src,
                      "run_agent 没有记录「是否已流式推过」")
        self.assertRegex(src, r'if not response\.get\("_streamed"\)',
                         "整段补发必须被 _streamed 标志挡住")

    def test_stream_replay_is_opt_in_and_has_fallback(self):
        """流式必须可关（Provider 不支持时能退回非流式）。"""
        self.assertTrue(hasattr(agent, "STREAM_REPLAY_ENABLED"),
                        "agent 缺 STREAM_REPLAY_ENABLED 开关")
        src = inspect.getsource(agent.run_agent)
        self.assertIn("stream_replay", src,
                      "run_agent 没有把 stream_replay 传给 llm 层")

    def test_llm_stream_falls_back_when_replay_unavailable(self):
        """流式重放失败**不能**让整轮失败（有完整 content 兜底）。"""
        from h3studio import llm
        src = inspect.getsource(llm.call_provider_with_tools_stream)
        self.assertIn("except Exception", src,
                      "流式重放必须被 try/except 包住")
        self.assertIn("不影响", src + "  " or src,
                      "流式重放失败要吞掉（注释要写明不影响主流程）")


# ------------------------------------------------------------------ 2. 轮询终止 + SSE AbortController


class TestFrontendTermination(unittest.TestCase):
    """前端：轮询终止条件 + SSE AbortController。"""

    def _js(self):
        return (ROOT / "web" / "agent.js").read_text(encoding="utf-8")

    def test_sse_uses_abort_controller(self):
        src = self._js()
        self.assertIn("new AbortController()", src,
                      "SSE 请求没有 AbortController -> 无法取消/超时")
        self.assertIn("signal: controller.signal", src,
                      "fetch 没有接上 AbortController 的 signal")

    def test_sse_has_hard_timeout(self):
        src = self._js()
        self.assertIn("SSE_MAX_MS", src, "SSE 没有硬超时")
        # setTimeout 的回调里调 controller.abort()（跨行，所以用 DOTALL）
        self.assertRegex(src, r"setTimeout\([\s\S]{0,200}controller\.abort\(\)",
                         "SSE 超时没有真的 abort 掉请求")

    def test_cancel_function_is_exposed(self):
        src = self._js()
        self.assertIn("function cancelAgentSSE", src,
                      "缺 cancelAgentSSE（外部无法取消 SSE）")
        self.assertIn("_cancelAgentSSE: cancelAgentSSE", src,
                      "cancelAgentSSE 没导出（测试与「停止」按钮用不了）")

    def test_page_unload_disconnects_sse(self):
        src = self._js()
        self.assertIn("beforeunload", src,
                      "页面卸载时没断开 SSE -> 服务端 Agent 白跑")
        self.assertRegex(src, r"beforeunload[\s\S]{0,400}cancelAgentSSE",
                         "beforeunload 没调用 cancelAgentSSE")

    def test_job_polling_has_termination_conditions(self):
        src = self._js()
        self.assertIn("JOB_POLL_MAX_MS", src,
                      "任务卡轮询没有超时上限 -> 永远轮询下去")
        self.assertIn("JOB_POLL_MAX_ERRORS", src,
                      "任务卡轮询没有连续失败上限")
        self.assertIn("markPollingTimedOut", src,
                      "轮询超时后没有把任务标成「状态未知」")

    def test_polling_timeout_marks_state_unknown(self):
        src = self._js()
        self.assertRegex(src, r'markPollingTimedOut[\s\S]{0,600}entry\.status\s*=\s*"unknown"',
                         "轮询超时没有把状态标成 unknown（会假装还在跑）")


# ------------------------------------------------------------------ 3. recall 语义


class TestRecallLimitSemantics(unittest.TestCase):
    """recall(limit<0) 的语义必须统一（<=0 -> 返回全部）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.store = agent_memory.MemoryStore(Path(self.tmp.name) / "m.db")
        for i in range(5):
            self.store.remember(f"pref_{i}", f"v{i}")

    def tearDown(self):
        self.tmp.cleanup()

    def test_normalize_function_exists(self):
        self.assertTrue(hasattr(agent_memory, "_normalize_read_limit"),
                        "缺统一的 limit 归一函数")

    def test_zero_and_negative_mean_all(self):
        self.assertEqual(agent_memory._normalize_read_limit(0), 0)
        self.assertEqual(agent_memory._normalize_read_limit(-1), 0)
        self.assertEqual(agent_memory._normalize_read_limit(-99), 0)

    def test_positive_is_clamped(self):
        self.assertEqual(agent_memory._normalize_read_limit(3), 3)
        self.assertEqual(
            agent_memory._normalize_read_limit(agent_memory.MAX_READ_LIMIT + 50),
            agent_memory.MAX_READ_LIMIT)

    def test_recall_with_limit_zero_returns_all(self):
        self.assertEqual(len(self.store.recall("", limit=0)), 5)

    def test_recall_with_negative_limit_returns_all(self):
        """旧实现：limit=-1 被夹成 **1**（不是「全部」）。"""
        self.assertEqual(len(self.store.recall("", limit=-1)), 5)

    def test_recall_with_positive_limit_respects_it(self):
        self.assertEqual(len(self.store.recall("", limit=2)), 2)

    def test_tool_layer_uses_same_normalizer(self):
        """工具层与存储层必须用**同一个**归一函数。"""
        from h3studio import agent_tools
        src = inspect.getsource(agent_tools)
        self.assertIn("agent_memory._normalize_read_limit", src,
                      "工具层没走统一的归一函数 -> 两层语义会漂移")


# ------------------------------------------------------------------ 4. steps[] schema 统一


class TestStepsSchemaUnified(unittest.TestCase):
    """steps[] 必须只有**一种**形态。"""

    def test_step_fields_declared(self):
        self.assertTrue(hasattr(agent, "STEP_FIELDS"),
                        "缺 STEP_FIELDS 契约声明")
        self.assertEqual(len(set(agent.STEP_FIELDS)), len(agent.STEP_FIELDS),
                         "STEP_FIELDS 有重复字段")
        for field in ("step", "tool", "ok", "ms", "args", "summary",
                      "call_id", "note", "error"):
            self.assertIn(field, agent.STEP_FIELDS,
                          f"STEP_FIELDS 缺 {field}")

    def test_step_entry_produces_exact_field_set(self):
        entry = agent._step_entry(1, tool="search_assets", ok=True, ms=12,
                                  args="{}", summary="ok", call_id="c1")
        self.assertEqual(set(entry), set(agent.STEP_FIELDS),
                         "_step_entry 产出的字段与契约不符")

    def test_non_tool_step_has_same_fields(self):
        """非工具步也必须带全套字段（tool=None 而不是缺字段）。"""
        entry = agent._step_entry(2, tool=None, ok=False, ms=3,
                                  note="模型给出结论")
        self.assertEqual(set(entry), set(agent.STEP_FIELDS))
        self.assertIsNone(entry["tool"])
        self.assertEqual(entry["args"], "")
        self.assertEqual(entry["summary"], "")

    def test_all_steps_append_go_through_builder(self):
        """run_agent 里**所有** steps.append 必须走 _step_entry。"""
        src = inspect.getsource(agent.run_agent)
        raw = re.findall(r"steps\.append\(\s*\{", src)
        self.assertEqual(raw, [],
                         f"run_agent 里有 {len(raw)} 处裸 steps.append({{...}})"
                         " —— 必须走 _step_entry 才能保证形态统一")

    def test_ctx_record_normalizes_fields(self):
        """ctx.record 也必须补齐缺省字段（不能变成第二种形态）。"""
        ctx = agent.AgentContext()
        ctx.record({"step": 1, "tool": "x"})
        entry = ctx.steps[0]
        self.assertEqual(set(entry), set(agent.STEP_FIELDS),
                         "ctx.record 没补齐字段 -> 又多出一种形态")


# ------------------------------------------------------------------ 5. 交付脚本资源断言


class TestDeliveryScriptAssertsResources(unittest.TestCase):
    """交付脚本必须断言前端资源全部加载完。"""

    def _script(self):
        return (ROOT / "scripts" / "verify_agent_contract_fix.py").read_text(
            encoding="utf-8")

    def test_script_asserts_static_resources(self):
        src = self._script()
        self.assertIn("script[src]", src,
                      "交付脚本没收集 <script src> 列表")
        self.assertIn("link[rel=stylesheet]", src,
                      "交付脚本没收集 <link rel=stylesheet> 列表")
        self.assertIn("failed_static", src,
                      "交付脚本没断言静态资源全部 200")

    def test_script_fails_loudly_on_missing_resource(self):
        src = self._script()
        self.assertRegex(src, r"if failed_static:[\s\S]{0,200}raise AssertionError",
                         "资源缺失时没有 raise（脚本会假装通过）")

    def test_script_checks_every_resource_status(self):
        src = self._script()
        self.assertRegex(src, r"for url in all_static:[\s\S]{0,400}status\s*!=\s*200",
                         "资源断言没有逐个查状态码")


if __name__ == "__main__":
    unittest.main()
