# -*- coding: utf-8 -*-
"""内置 Agent 的提示词历史钩子（合并必补项 —— 见《交付报告-提示词历史.md》§9）。

【为什么需要这个测试】
提示词历史的落点原本只挂在 **server.api_create**（面板点「生成」那条路）。
内置 Agent 走的是**自己的工具链**（enhance_prompt / create_job），
**不经过** api_create —— 所以 Agent 产生的提示词在面板的「历史」浮层里
根本查不到，用户没法把 Agent 的成果一键填回提示词框。

合并时补的钩子：h3studio/agent.py 的 `_record_agent_prompt_history`
  * _tool_enhance_prompt 返回前 -> source = enhance
  * _tool_create_job 建任务后     -> source = generate

【本测试守住什么】
1. Agent 增强 -> 历史表里多一行，原始/增强两份都在，source = enhance
2. Agent 建任务 -> 历史表里多一行，source = generate
3. 两条链路**互不冲突**：
   - 面板侧记的行不会被 Agent 侧覆盖（面板行原样还在）
   - Agent 行不会污染面板行
   - 同一面板同一原文 -> 合并成一行（use_count 累加），不重复建行
4. 钩子失败**不阻断** Agent 主流程（历史是旁路功能）
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod  # noqa: E402
from h3studio import prompt_history as ph  # noqa: E402
from h3studio import prompt_history_hooks as ph_hooks  # noqa: E402


class _FakeServer:
    """最小服务端替身：只提供 Agent 钩子需要的 db() 与 create_job_record()。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self.created: list[tuple[str, dict]] = []

    def db(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def create_job_record(self, mode: str, params: dict) -> str:
        self.created.append((mode, dict(params)))
        return "jobagent01"

    def prepare_job_params(self, mode: str, params: dict, asset_refs=None,
                           saved_files=None):
        """Agent 的 create_job 工具会调它做服务端级校验。

        这里做**最小**校验：提示词非空 + 模式白名单 ——
        与 server.prepare_job_params 的核心判据一致，
        但不引入素材库/参考图那一整套（那不是本测试要验证的东西）。
        """
        prompt = str((params or {}).get("prompt") or "").strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if mode not in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2v",
                        "t2a", "a2a"):
            raise ValueError(f"未知模式 {mode}")
        return asset_refs or []


class TestAgentPromptHistoryHook(unittest.TestCase):
    """Agent 侧钩子的落库行为。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "jobs.db"
        self.server = _FakeServer(self.db_path)
        self.ctx = agent_mod.AgentContext(server_module=self.server)
        with self.server.db() as conn:
            ph.ensure_table(conn)
            conn.commit()

    def tearDown(self):
        self._tmp.cleanup()

    # ---------------------------------------------------------------- 工具

    def _rows(self, panel_key: str = ""):
        with self.server.db() as conn:
            return ph.list_history(conn, panel_key=panel_key, limit=100)

    def _count(self, panel_key: str = "") -> int:
        return len(self._rows(panel_key))

    # ---------------------------------------------------------------- 用例

    def test_enhance_hook_records_enhance_source(self):
        """Agent 增强 -> 历史表多一行，source=enhance，原始/增强两份都在。"""
        agent_mod._record_agent_prompt_history(
            self.ctx, "t2i",
            {"original_prompt": "一只橘猫", "prompt": "一只橘猫，暖调胶片"},
            agent_mod._ph_source_enhance())

        items = self._rows("t2i")
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["original_prompt"], "一只橘猫")
        self.assertEqual(row["enhanced_prompt"], "一只橘猫，暖调胶片")
        self.assertEqual(row["source"], ph.SOURCE_ENHANCE)
        self.assertTrue(row["has_enhanced"])

    def test_create_job_hook_records_generate_source(self):
        """Agent 建任务 -> 历史表多一行，source=generate。"""
        agent_mod._record_agent_prompt_history(
            self.ctx, "t2i_qwen",
            {"original_prompt": "赛博朋克东京", "prompt": "赛博朋克东京",
             "model_key": "qwen_image_21"},
            agent_mod._ph_source_generate())

        items = self._rows("t2i_qwen")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["source"], ph.SOURCE_GENERATE)
        self.assertEqual(items[0]["model_key"], "qwen_image_21")
        # 未增强直出 -> has_enhanced 为 False
        self.assertFalse(items[0]["has_enhanced"])

    def test_panel_and_agent_lines_do_not_conflict(self):
        """
        两条链路互不冲突（合并要求的核心）：
        面板侧记的行与 Agent 侧记的行互不覆盖、互不污染。
        """
        # 面板侧（api_create 的钩子）：模拟面板点「生成」
        with self.server.db() as conn:
            ph_hooks.record_prompt_history(
                conn, "t2i",
                {"original_prompt": "面板原文", "prompt": "面板原文"},
                source=ph.SOURCE_GENERATE)
            conn.commit()

        # Agent 侧（本次合并补的钩子）
        agent_mod._record_agent_prompt_history(
            self.ctx, "t2i",
            {"original_prompt": "Agent原文", "prompt": "Agent增强"},
            agent_mod._ph_source_enhance())

        items = self._rows("t2i")
        self.assertEqual(len(items), 2, "面板行与 Agent 行必须并存")
        originals = {it["original_prompt"] for it in items}
        self.assertEqual(originals, {"面板原文", "Agent原文"})
        # 面板行没有被 Agent 侧改写
        panel_row = next(it for it in items if it["original_prompt"] == "面板原文")
        self.assertEqual(panel_row["source"], ph.SOURCE_GENERATE)
        # 未增强直出：enhanced_prompt 存空串（has_enhanced=False）
        self.assertFalse(panel_row["has_enhanced"])
        self.assertEqual(panel_row["enhanced_prompt"], "")
        # Agent 行也没被污染
        agent_row = next(it for it in items if it["original_prompt"] == "Agent原文")
        self.assertEqual(agent_row["source"], ph.SOURCE_ENHANCE)

    def test_same_prompt_merges_into_one_row(self):
        """同一面板同一原文 -> 合并成一行，不重复建行（与面板侧口径一致）。"""
        for _ in range(3):
            agent_mod._record_agent_prompt_history(
                self.ctx, "t2i",
                {"original_prompt": "重复的提示词", "prompt": "重复的提示词"},
                agent_mod._ph_source_generate())
        items = self._rows("t2i")
        self.assertEqual(len(items), 1, "同原文必须合并成一行")
        self.assertEqual(items[0]["use_count"], 3)

    def test_agent_row_is_isolated_from_other_panels(self):
        """Agent 记的行也遵守跨面板隔离。"""
        agent_mod._record_agent_prompt_history(
            self.ctx, "t2i_moss",
            {"original_prompt": "MOSS 专属", "prompt": "MOSS 专属"},
            agent_mod._ph_source_generate())
        self.assertEqual(self._count("t2i_moss"), 1)
        self.assertEqual(self._count("t2i"), 0)

    def test_hook_failure_does_not_break_agent_flow(self):
        """钩子失败**不阻断** Agent 主流程（历史是旁路功能）。"""
        # 故意给一个没有 db() 的 server -> 钩子应静默跳过而不是抛
        ctx = agent_mod.AgentContext(server_module=object())
        agent_mod._record_agent_prompt_history(
            ctx, "t2i", {"original_prompt": "x", "prompt": "y"},
            agent_mod._ph_source_enhance())
        # 不断言异常 —— 断言「没有抛」本身就是断言

    def test_hook_with_no_server_is_noop(self):
        """ctx.server 为 None -> 静默跳过。"""
        ctx = agent_mod.AgentContext(server_module=None)
        agent_mod._record_agent_prompt_history(
            ctx, "t2i", {"original_prompt": "x", "prompt": "y"},
            agent_mod._ph_source_enhance())

    def test_empty_prompt_is_not_recorded(self):
        """空提示词不写行（与面板侧口径一致）。"""
        agent_mod._record_agent_prompt_history(
            self.ctx, "t2i", {"original_prompt": "", "prompt": ""},
            agent_mod._ph_source_generate())
        self.assertEqual(self._count("t2i"), 0)


class TestAgentEnhanceToolRecordsHistory(unittest.TestCase):
    """_tool_enhance_prompt 真实调用后，历史表里能看到记录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "jobs.db"
        self.server = _FakeServer(self.db_path)
        self.ctx = agent_mod.AgentContext(server_module=self.server)
        with self.server.db() as conn:
            ph.ensure_table(conn)
            conn.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def test_enhance_tool_writes_history(self):
        """走真实的 enhance_prompt 工具 -> 历史表里出现 enhance 行。"""
        calls: list[str] = []

        class _FakeLLM:
            @staticmethod
            async def call_provider(provider, messages, options=None):
                calls.append("called")
                return "画面中心是一只橘猫，暖调胶片质感，浅景深"

        original_llm = agent_mod.llm_client
        agent_mod.llm_client = _FakeLLM
        try:
            ctx = agent_mod.AgentContext(
                server_module=self.server,
                provider={"name": "fake", "id": "fake"},
            )
            result = asyncio.run(agent_mod._tool_enhance_prompt(
                ctx, {"prompt": "一只橘猫", "mode": "t2i"}))
        finally:
            agent_mod.llm_client = original_llm

        self.assertTrue(calls, "应调用过 provider")
        self.assertEqual(result["original"], "一只橘猫")

        with self.server.db() as conn:
            items = ph.list_history(conn, panel_key="t2i", limit=10)
        self.assertEqual(len(items), 1, "enhance_prompt 必须写进历史表")
        self.assertEqual(items[0]["source"], ph.SOURCE_ENHANCE)
        self.assertEqual(items[0]["original_prompt"], "一只橘猫")
        self.assertNotEqual(items[0]["enhanced_prompt"], "")

    def test_create_job_tool_writes_history(self):
        """走真实的 create_job 工具 -> 历史表里出现 generate 行。

        这条用例守的是**工具真实路径**上的钩子 ——
        去掉钩子调用（而不是去掉钩子函数）时，本用例必须红。
        """
        ctx = agent_mod.AgentContext(server_module=self.server)
        result = asyncio.run(agent_mod._tool_create_job(
            ctx, {"mode": "t2i", "prompt": "Agent 建任务的提示词"}))

        self.assertEqual(result["job_id"], "jobagent01")
        self.assertTrue(self.server.created, "应真的建了任务")

        with self.server.db() as conn:
            items = ph.list_history(conn, panel_key="t2i", limit=10)
        self.assertEqual(len(items), 1,
                         "create_job 必须写进历史表（去掉钩子则本用例红）")
        self.assertEqual(items[0]["source"], ph.SOURCE_GENERATE)
        self.assertEqual(items[0]["original_prompt"], "Agent 建任务的提示词")

    def test_create_job_and_enhance_share_one_table_without_conflict(self):
        """两条工具链路先后跑 -> 同一张表里并存，互不覆盖。"""
        ctx = agent_mod.AgentContext(
            server_module=self.server,
            provider={"name": "fake", "id": "fake"},
        )

        class _FakeLLM:
            @staticmethod
            async def call_provider(provider, messages, options=None):
                return "增强后的版本"

        original_llm = agent_mod.llm_client
        agent_mod.llm_client = _FakeLLM
        try:
            asyncio.run(agent_mod._tool_enhance_prompt(
                ctx, {"prompt": "同一面板的原文", "mode": "t2i"}))
        finally:
            agent_mod.llm_client = original_llm

        asyncio.run(agent_mod._tool_create_job(
            ctx, {"mode": "t2i", "prompt": "同一面板的原文"}))

        with self.server.db() as conn:
            items = ph.list_history(conn, panel_key="t2i", limit=10)
        # 同一面板 + 同一原文 -> 合并成一行，不重复建行
        self.assertEqual(len(items), 1)
        # use_count 只统计「提交生成」的次数（增强不算）——
        # 与面板侧口径一致，见 h3studio/prompt_history.record 的注释。
        # 这里 enhance 先写（计数 0），create_job 后写（计数 1）。
        self.assertEqual(items[0]["use_count"], 1)
        # 增强那次留下的增强值不被「未增强直出」抹掉
        self.assertTrue(items[0]["has_enhanced"])
        self.assertNotEqual(items[0]["enhanced_prompt"], "")


if __name__ == "__main__":
    unittest.main()
