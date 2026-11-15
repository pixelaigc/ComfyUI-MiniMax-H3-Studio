# -*- coding: utf-8 -*-
"""c9 回归：Agent 对话链路的 Provider 超时预算必须够用。

【缺陷来源 —— 独立验收 c9 不通过，根因在工作台侧】

验收实测：`POST /api/agent/chat` 9 次抽样成功 0 次（429×4 并发闸门 + 110–150s 超时×5），
报错「调用 Provider『本机 Qwen 多模态』超时（1024 tokens 上限，模型可能未加载或过慢）」。
验收方给的定位：工作台阈值 `llm.py:51` = `120 + max_tokens*0.12`，
`agent.py:3709` `max_tokens=1024` ⇒ 243s，**小于上游所需**，必然超时。

【本轮实测把根因钉死到三件事（都是工作台侧可修的）】

1. **工具调用路径的 timeout 根本没用 compute_timeout()**
   `h3studio/llm.py`：
     * `call_provider()`（第 340 行）→ `compute_timeout(max_tokens)`  ✅
     * `call_provider_with_tools()`（第 513 行）→ `options.get("timeout", 180)`  ❌ 写死 180
     * `_replay_stream_text()`（第 694 行）→ `options.get("timeout", 180)`    ❌ 写死 180
   Agent 走的是**工具调用**路径，且 `agent.py` 传 `timeout=STEP_TIMEOUT(180)`。
   ⇒ 243s 的计算根本没生效，实际用的是 **180s**。

2. **单槽 Provider 的排队等待时间没有被算进超时预算**
   实测（本机 Strata，pid 15495，单槽串行）：
     * 我们自己的请求实际算力开销：`prompt_ms=434 + predicted_ms=67` ⇒ **0.5s**
       （`reasoning_content` 长度 = 0 ⇒ `enable_thinking=false` **是被尊重的**）
     * 但 HTTP 端到端耗时 **90.5s** ⇒ 差额全是**排队等别人的请求**
     * 上游日志同期显示别人在跑 54,178 / 97,429 / 65,473 tokens 的 prompt，
       思考预算 24,000，单请求能占槽几分钟。
   ⇒ 「超时」不是模型慢，是**共享单槽 Provider 的排队时间**，
      而工作台的超时预算里**没有这一项**。

3. **超时文案把原因说错了**
   `llm.py` 的 ProviderError 文案写「模型可能未加载或过慢」，
   实际是「排队等前面的请求」—— 文案误导排障。

【本批测试锁住什么】
  1. 工具调用路径的 timeout **必须**由 max_tokens 推导（不得写死 180）。
  2. Agent 传入的 timeout **必须** >= compute_timeout(max_tokens)；
     且预算里**必须**含排队余量（共享单槽 Provider）。
  3. 超时文案必须点明「可能是在排队等待」，不能只说「模型未加载或过慢」。
  4. 流式重放路径的 timeout 同样不得写死 180。

运行：python3 -m pytest tests/test_agent_provider_timeout_budget.py -v
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from h3studio import llm as llm_client  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LLM_PY = os.path.join(REPO, "h3studio", "llm.py")
AGENT_PY = os.path.join(REPO, "h3studio", "agent.py")


class TestToolPathTimeoutBudget(unittest.TestCase):
    """工具调用路径的 timeout 不得写死，必须按 max_tokens 推导。"""

    @classmethod
    def setUpClass(cls):
        with open(LLM_PY, encoding="utf-8") as fh:
            cls.llm_src = fh.read()

    def _body_of(self, func_name):
        m = re.search(rf"def {func_name}\(.*?\n(?=\S)", self.llm_src, re.S)
        self.assertIsNotNone(m, f"找不到 {func_name}")
        return m.group(0)

    def test_01_call_provider_with_tools_uses_compute_timeout(self):
        """call_provider_with_tools 必须用 compute_timeout(max_tokens)。"""
        body = self._body_of("call_provider_with_tools")
        self.assertIn("compute_timeout(max_tokens)", body)
        self.assertNotIn('options.get("timeout", 180)', body)

    def test_02_stream_replay_uses_compute_timeout(self):
        """_replay_stream_text 同样不得写死 180。"""
        body = self._body_of("_replay_stream_text")
        self.assertIn("compute_timeout(max_tokens)", body)
        self.assertNotIn('options.get("timeout", 180)', body)

    def test_03_no_hardcoded_180_fallback_left(self):
        """整个 llm.py 里不得再有写死的 180 兜底。"""
        self.assertNotIn('options.get("timeout", 180)', self.llm_src)

    def test_04_explicit_timeout_option_still_wins(self):
        """调用方显式传 timeout 时仍以调用方为准（不得覆盖）。"""
        body = self._body_of("call_provider_with_tools")
        self.assertIn('options.get("timeout")', body)

    def test_05_timeout_error_mentions_queueing(self):
        """**每一条**超时文案都必须点明「可能是在排队等待」。

        两条路径都要检查：
          * call_provider()               —— 一问一答路径
          * call_provider_with_tools()   —— Agent 工具调用路径（c9 的实际路径）
        """
        found = 0
        for m in re.finditer(r"调用 Provider「\{name\}」超时", self.llm_src):
            found += 1
            text = self.llm_src[m.start():m.start() + 500]
            self.assertTrue(
                ("排队" in text) or ("等待" in text),
                f"超时文案必须提到排队/等待，实际：{text[:200]}")
        self.assertGreaterEqual(found, 2,
                                "应至少找到两条超时文案（普通路径 + 工具路径）")


class TestAgentTimeoutBudget(unittest.TestCase):
    """Agent 传给 Provider 的 timeout 必须够用（含排队余量）。"""

    @classmethod
    def setUpClass(cls):
        with open(AGENT_PY, encoding="utf-8") as fh:
            cls.agent_src = fh.read()

    def test_10_agent_tool_call_timeout_is_not_below_computed(self):
        """Agent 传给 Provider 的 timeout 不得小于 compute_timeout(max_tokens)。"""
        # compute_timeout(1024) 已含排队余量：120 + 122.88 + 240 = 482.88
        need = llm_client.compute_timeout(1024)
        self.assertGreater(need, 180.0,
                           "compute_timeout(1024) 必须 > 180（旧写死值），"
                           "否则「写死 180」不会被测出来")
        # Agent 不应再把写死的 timeout 传给 Provider
        # （由 llm 层按 max_tokens 推导）
        self.assertNotIn('"timeout": STEP_TIMEOUT', self.agent_src)
        self.assertNotIn('"timeout": 180', self.agent_src)
        # 也不该再用 180 这个写死值当单步超时
        self.assertNotIn("STEP_TIMEOUT = 180.0", self.agent_src)

    def test_11_step_timeout_budget_covers_queue_wait(self):
        """Agent 的单步超时预算必须含排队余量（共享单槽 Provider）。"""
        self.assertTrue(hasattr(llm_client, "compute_timeout"))
        # 排队余量参数必须存在且为正
        self.assertTrue(
            hasattr(llm_client, "QUEUE_WAIT_ALLOWANCE"),
            "llm.py 必须定义 QUEUE_WAIT_ALLOWANCE（共享单槽 Provider 的排队余量）")
        self.assertGreater(llm_client.QUEUE_WAIT_ALLOWANCE, 0)

    def test_12_agent_timeout_covers_computed_budget(self):
        """Agent 的 STEP_TIMEOUT 必须 >= compute_timeout(max_tokens)。

        （排队余量已经**含在** compute_timeout 里，不重复加。）
        """
        need = (llm_client.compute_timeout(1024)
                + llm_client.QUEUE_WAIT_ALLOWANCE)
        from h3studio import agent as agent_mod
        self.assertGreaterEqual(
            getattr(agent_mod, "STEP_TIMEOUT", 0.0), need,
            "Agent 的 STEP_TIMEOUT 必须覆盖 compute_timeout + 排队余量")

    def test_13_queue_allowance_only_on_tools_path(self):
        """排队余量**只**加在工具调用路径，不得污染 compute_timeout。

        既有契约（tests/test_max_tokens.py::test_ceiling_does_not_inflate_timeout）
        要求「本次给 800 token -> 超时 < 300 秒」——
        那条路径是自己独占推理服务、不与别人抢槽，排队时间不适用。
        """
        # compute_timeout 保持纯「随预算缩放」语义
        base = llm_client.BASE_TIMEOUT + 1024 * llm_client.PER_TOKEN_TIMEOUT
        self.assertAlmostEqual(llm_client.compute_timeout(1024),
                               base, delta=0.01)
        self.assertLess(llm_client.compute_timeout(800), 300,
                        "compute_timeout(800) 必须 < 300（既有契约）")
        # 但工具调用路径必须有排队余量
        tools_timeout = (llm_client.compute_timeout(1024)
                         + llm_client.QUEUE_WAIT_ALLOWANCE)
        self.assertGreater(tools_timeout, 480.0,
                           "工具调用路径的超时必须覆盖实测的 178s 排队")
        self.assertGreater(tools_timeout, llm_client.compute_timeout(1024))


