# -*- coding: utf-8 -*-
"""
Agent「误报编造 job id」的**回归测试**（2026-11-11）。

【本文件要锁住的缺陷】

用户在 8190 页面用内置 Agent 说「换成moss模型看看」，界面顶部给消息打了
红色「失败」标签，底部红条提示「模型声称已下发任务但实际没有创建任务记录
（报告的 job id 是编造的），已拦截。」

但线上真实数据证明这是**误报**：
  * jobs 表里有**真实**任务 ac8778a06a04（t2i_moss，done，
    产物 images/i-zimage-000134.png 正常渲染）；
  * 工具调用区 4 个工具（get_mode_schema ×2 / create_job / respond）
    全部 ✓ 成功；
  * 会话库里该轮**只有 assistant 消息、没有 tool 消息** ——
    这正是 server.py 里 `failed_code` 早退分支的特征。

【根因（三条，缺一不可）】

1. **判定只看 ctx.jobs（内存），不看权威来源（jobs 表）**
   ctx.jobs 只是「本次运行工具链 append 的条目」，不是「系统里真实存在的
   任务」。面板建的任务 / 上一轮建的任务 / 异步落库延迟的任务都查不到。

2. **job id 用精确字符串比对**
   模型把真实 id **抄错**了：ac8778a06a04 -> ac8778a6ea84（12 位错 3 位）。
   精确相等 -> 判「编造」。

3. **fabricated 标志在 nudge 之后「粘住」不放**
   第 N 步正文判定（误报）-> fabricated=True -> 推它一轮；
   第 N+1 步模型**真的建了任务并 respond 成功** -> 任务真的建成了，
   但 fabricated 还是 True -> 整轮硬失败。

【本文件覆盖三类场景】

  A. 真实 12 位短 id（模型抄错几个字符）  -> 必须**放行**（成功）
  B. 异步落库延迟（ctx.jobs 空，库里已有） -> 必须**放行**（成功）
  C. 真编造不存在的 job id                -> 必须**拦截**（失败）

外加边界：大小写 / 分隔符 / 前缀 / 截断前缀 / 任务被删 / 并发多条。

运行：python3 -m pytest tests/test_agent_fabricated_job_fix.py -v
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_sessions              # noqa: E402


#: 线上真实误报里服务端铸的 id（jobs 表实测）
REAL_JOB_ID = "ac8778a06a04"
#: 模型抄错后的 id（用户截图里看到的）
TYPO_JOB_ID = "ac8778a6ea84"


# ------------------------------------------------------------------ 假服务端


class FakeServer:
    """
    Agent 真正依赖的服务端入口的最小替身。

    【关键点】它模拟了**权威来源**（jobs 表）：create_job_record 落库，
    list_job_ids_since 按时间窗查库。这样才能测出「ctx.jobs 空但库里
    有任务」这种真实存在的漏判场景。
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db_path = tmp / "jobs.db"
        self.created = []
        #: jobs 表里的真实记录（权威来源）
        self.db_rows = []
        self._server_mod = None
        self._seed_providers()

    def _seed_providers(self):
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

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def prepare_job_params(self, mode, params, asset_refs=None, saved_files=None):
        return self._server_mod.prepare_job_params(mode, params, asset_refs,
                                                   saved_files)

    def create_job_record(self, mode, params):
        job_id = REAL_JOB_ID if not self.created else f"other{len(self.created):08d}"
        self.created.append((job_id, mode, dict(params)))
        self.db_rows.append(job_id)
        return job_id

    def get_job_params(self, job_id):
        for jid, mode, params in self.created:
            if jid == job_id:
                return {"mode": mode, "params": dict(params)}
        return None

    def list_job_records(self, status=None, mode=None, since_hours=None, limit=10):
        return [{"job_id": j, "mode": m, "status": "queued", "params": p}
                for j, m, p in self.created[:limit]]

    def list_job_ids_since(self, since_ts):
        """**权威来源**：jobs 表查询（与 server.list_job_ids_since 同语义）。"""
        return list(self.db_rows)

    def get_job_status_payload(self, job_id):
        for jid, m, params in self.created:
            if jid == job_id:
                return {"job_id": jid, "mode": m, "status": "queued",
                        "params": params}
        return None

    async def cancel_job_record(self, job_id):
        return {"job_id": job_id, "status": "canceled", "ok": True}


def load_real_server_module():
    import importlib
    sys.modules.pop("server", None)
    return importlib.import_module("server")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-fabricated-"))
        self.server_mod = load_real_server_module()
        self.server = FakeServer(self.tmp)
        self.server._server_mod = self.server_mod
        agent_sessions.reset_store(self.tmp / "agent_sessions.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self):
        c = agent_mod.AgentContext(server_module=self.server,
                                  provider={"name": "测试", "model": "m",
                                            "type": "openai"})
        c.session_store = agent_sessions.get_store()
        return c

    def call(self, tool_name, args, ctx=None):
        spec = agent_mod.TOOLS[tool_name]
        return asyncio.run(spec["handler"](ctx or self.ctx(), args))

    def call_expect_error(self, tool_name, args, ctx=None):
        # 注意：不要把 assertRaises 的 as 目标也叫 ctx —— 它会遮蔽参数。
        with self.assertRaises(agent_mod.AgentError) as raised:
            asyncio.run(agent_mod.TOOLS[tool_name]["handler"](
                ctx or self.ctx(), args))
        return raised.exception

    def run_agent_with_turns(self, turns, message="换成moss模型看看"):
        """按预设的模型轮次跑一次完整 Agent 编排。"""
        original = agent_mod.llm_client.call_provider_with_tools
        calls = {"n": 0}

        async def fake(provider, messages, tools, options=None, images=None):
            index = calls["n"]
            calls["n"] += 1
            if index >= len(turns):
                return {"content": "结束", "tool_calls": [], "raw": {}}
            return turns[index]

        agent_mod.llm_client.call_provider_with_tools = fake
        try:
            return asyncio.run(agent_mod.run_agent(
                self.server, message,
                session_store=agent_sessions.get_store()))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original


# ------------------------------------------------------------------ A. 真实短 id


class TestRealShortJobIdNotFabricated(_Base):
    """
    场景 A：**真实**任务 id 被模型抄错几个字符 -> 不能判「编造」。

    【实测事实 2026-11-11】线上真实误报：服务端铸 ac8778a06a04，
    模型写成 ac8778a6ea84（12 位里错 3 位）。
    """

    def test_typo_of_real_id_is_not_fabricated(self):
        text = f"已下发 MOSS-Image 文生图任务（job {TYPO_JOB_ID}）"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(
                text, [{"job_id": REAL_JOB_ID}]))

    def test_typo_distance_is_exactly_three(self):
        """锁住实测事实：真实误报差 3 位（回归时阈值不能悄悄改小）。"""
        distance = agent_mod._job_id_typo_distance(REAL_JOB_ID, TYPO_JOB_ID)
        self.assertEqual(distance, 3)
        self.assertLessEqual(distance, agent_mod.JOB_ID_TYPO_TOLERANCE)

    def test_exact_id_is_not_fabricated(self):
        text = f"已下发任务（job {REAL_JOB_ID}）"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(
                text, [{"job_id": REAL_JOB_ID}]))

    def test_typo_accepted_when_ids_given_as_plain_list(self):
        """判定函数要兼容「纯 id 列表」与「条目字典列表」两种入参。"""
        text = f"已下发任务（job {TYPO_JOB_ID}）"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(
                text, [REAL_JOB_ID]))

    def test_case_and_separator_variants_are_not_fabricated(self):
        """格式差异（大小写 / 分隔符 / 前缀）不算编造。"""
        jobs = [{"job_id": REAL_JOB_ID}]
        for variant in (REAL_JOB_ID.upper(),
                        "ac8778a0-6a04",
                        "ac8778a0_6a04",
                        f"job_{REAL_JOB_ID}"):
            text = f"已下发任务（job {variant}）"
            self.assertFalse(
                agent_mod._looks_like_fabricated_job_claim(text, jobs),
                f"格式变体 {variant!r} 被误判成编造")

    def test_truncated_prefix_is_not_fabricated(self):
        """模型只抄了前 8 位 —— 也是抄自真实 id，不是编造。"""
        text = "已下发任务（job ac8778a0）"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(
                text, [{"job_id": REAL_JOB_ID}]))

    def test_respond_accepts_miscopied_id_of_real_job(self):
        """respond 报告抄错的 id -> 接受（不能抛 fabricated_job_id）。"""
        shared = self.ctx()
        result = self.call("create_job",
                            {"mode": "t2i_moss", "prompt": "港风夜景"},
                            ctx=shared)
        self.assertEqual(result["job_id"], REAL_JOB_ID)
        out = self.call("respond", {
            "summary": f"已下发 MOSS 任务（job {TYPO_JOB_ID}）",
            "job_ids": [TYPO_JOB_ID]}, ctx=shared)
        self.assertTrue(out["ok"])
        self.assertEqual(shared.respond_payload["fabricated_job_ids"], [])


# ------------------------------------------------------------------ B. 落库延迟


class TestAuthoritativeSourceCoversCommitDelay(_Base):
    """
    场景 B：**异步落库延迟 / 非本次工具链创建**的任务。

    ctx.jobs 是空的，但权威来源（jobs 表）里有 —— 不能判「编造」。
    """

    def test_detector_uses_authoritative_ids_when_ctx_jobs_empty(self):
        text = f"已下发 MOSS-Image 文生图任务（job {REAL_JOB_ID}）"
        # ctx.jobs 空，但权威来源里有
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(text, [REAL_JOB_ID]))

    def test_known_job_ids_unions_memory_and_authoritative(self):
        ctx = self.ctx()
        ctx.authoritative_job_ids = {"auth00000001"}
        ctx.jobs.append({"job_id": "memory000001"})
        ids = agent_mod._known_job_ids(ctx)
        self.assertIn("auth00000001", ids)
        self.assertIn("memory000001", ids)

    def test_refresh_authoritative_reads_server_jobs_table(self):
        ctx = self.ctx()
        ctx.started_at = 1000.0
        self.server.db_rows.append("dbtable000001")
        ids = agent_mod._refresh_authoritative_job_ids(ctx)
        self.assertIn("dbtable000001", ids)

    def test_refresh_authoritative_survives_server_error(self):
        """查库失败不能把主流程带崩（退回到只看 ctx.jobs）。"""
        ctx = self.ctx()
        ctx.started_at = 1000.0

        def boom(_since):
            raise RuntimeError("db down")

        self.server.list_job_ids_since = boom
        self.assertEqual(agent_mod._refresh_authoritative_job_ids(ctx), set())

    def test_refresh_authoritative_tolerates_missing_server_api(self):
        ctx = self.ctx()
        ctx.started_at = 1000.0
        # 服务端没有这个 API（老服务端 / 单测替身）-> 退回只看 ctx.jobs
        self.server.list_job_ids_since = None
        self.assertEqual(agent_mod._refresh_authoritative_job_ids(ctx), set())

    def test_respond_accepts_job_only_present_in_authoritative_source(self):
        """任务在库里但不在 ctx.jobs（面板建 / 异步落库）-> 接受。"""
        ctx = self.ctx()
        ctx.started_at = 1000.0
        self.server.db_rows.append("dbtable000001")
        out = self.call("respond", {
            "summary": "已下发任务（job dbtable000001）",
            "job_ids": ["dbtable000001"]}, ctx=ctx)
        self.assertTrue(out["ok"])
        self.assertEqual(ctx.respond_payload["fabricated_job_ids"], [])


# ------------------------------------------------------------------ C. 真编造


class TestGenuineFabricationStillBlocked(_Base):
    """
    场景 C：模型**真编造** job id -> 必须仍然拦截（能力不得回退）。
    """

    def test_prefixed_fake_id_is_fabricated(self):
        text = "已下发文生视频任务（job t2v_019d874a）：一只猫在雨夜奔跑。"
        self.assertTrue(agent_mod._looks_like_fabricated_job_claim(text, []))

    def test_hex_fake_id_is_fabricated(self):
        text = "已下发文生视频任务（job 3f2a9c1b7d04）"
        jobs = [{"job_id": REAL_JOB_ID}]
        self.assertTrue(
            agent_mod._looks_like_fabricated_job_claim(text, jobs))

    def test_fake_id_far_from_real_is_fabricated(self):
        """差很多位的 id 仍然是编造（容错不能放宽成「差不多就算」）。"""
        fake = "000000000000"
        text = f"已下发任务（job {fake}）"
        self.assertTrue(
            agent_mod._looks_like_fabricated_job_claim(
                text, [{"job_id": REAL_JOB_ID}]))

    def test_fake_id_of_different_length_is_fabricated(self):
        fake = "ac8778a06a04ff"
        text = f"已下发任务（job {fake}）"
        self.assertTrue(
            agent_mod._looks_like_fabricated_job_claim(
                text, [{"job_id": REAL_JOB_ID}]))

    def test_respond_rejects_fake_job_id(self):
        exc = self.call_expect_error("respond", {
            "summary": "已下发文生视频任务（job t2v_019d874a）",
            "job_ids": ["t2v_019d874a"]})
        self.assertEqual(exc.code, "fabricated_job_id")
        self.assertIn("不存在", exc.message)

    def test_respond_rejects_fake_id_even_when_other_real_job_exists(self):
        """有真任务不代表**任何** id 都算数。"""
        shared = self.ctx()
        self.call("create_job", {"mode": "t2i_moss", "prompt": "夜景"},
                  ctx=shared)
        exc = self.call_expect_error("respond", {
            "summary": "已下发任务（job 000000000000）",
            "job_ids": ["000000000000"]}, ctx=shared)
        self.assertEqual(exc.code, "fabricated_job_id")

    def test_full_run_with_fabricated_id_is_blocked(self):
        """整条链路：编造 -> 强制重试 -> 仍编 -> 结构化失败。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
            {"content": None, "tool_calls": [
                {"id": "c3", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
        ]
        result = self.run_agent_with_turns(turns)
        self.assertEqual(result.get("jobs"), [])
        self.assertEqual(result.get("status"), "failed")
        self.assertEqual(result.get("code"), "fabricated_job_id")
        self.assertIn("编造", result.get("error") or "")

    def test_run_that_never_creates_job_and_claims_one_is_blocked(self):
        """一次任务都没建，却声称已下发 -> 仍然拦（不能被容错放宽）。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "list_modes", "arguments": {}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
            {"content": None, "tool_calls": [
                {"id": "c3", "name": "respond",
                 "arguments": {"summary": "已下发文生视频任务（job t2v_019d874a）",
                               "job_ids": ["t2v_019d874a"]}}]},
        ]
        result = self.run_agent_with_turns(turns, message="做个视频")
        self.assertEqual(result.get("status"), "failed")
        self.assertIn(result.get("code"),
                      ("fabricated_job_id", "no_tool_call"))


# ------------------------------------------------------------------ D. 端到端误报


class TestReportedFalsePositiveEndToEnd(_Base):
    """
    端到端复现用户报的误报，并确认**真成功**的链路不再被判失败。

    【真实时序（线上会话 06f7145d3dab4085 实测）】
      step1: create_job -> 真实任务建好
      step2: 模型输出正文（正文里 job id 抄错了）-> 旧逻辑判编造 -> 推一轮
      step3: 模型 respond（成功）
    旧逻辑：status=failed（误报）
    新逻辑：status=completed + jobs=[真实任务]
    """

    MISCOPIED = (f"已用 Moss 模型重新下发同一张港风夜景图：t2i_moss"
                  f"（MOSS-Image 文生图）｜比例 21:9 超宽画幅｜分辨率 720p。"
                  f"（job {TYPO_JOB_ID}）")

    def test_miscopied_id_after_real_create_job_completes(self):
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2i_moss", "prompt": "港风夜景"}}]},
            {"content": self.MISCOPIED, "tool_calls": []},
            {"content": None, "tool_calls": [
                {"id": "c3", "name": "respond",
                 "arguments": {"summary": self.MISCOPIED,
                               "job_ids": [TYPO_JOB_ID]}}]},
        ]
        result = self.run_agent_with_turns(turns)
        self.assertEqual(
            result.get("status"), "completed",
            f"真实成功的任务被判失败了: {result.get('error')!r}")
        self.assertNotEqual(result.get("code"), "fabricated_job_id")
        self.assertEqual([j.get("job_id") for j in result.get("jobs") or []],
                         [REAL_JOB_ID])

    def test_sticky_flag_does_not_fail_a_later_successful_run(self):
        """
        锁住「fabricated 标志粘住」这个根因。

        场景：第 1 步正文里的 id 被**误判**成编造（推它一轮），
        第 2 步模型**真的建了任务并 respond 成功** -> 必须正常收尾。
        """
        turns = [
            # 第 1 步：正文里有一个**不存在**的 id -> 判编造 -> 推一轮
            {"content": "已下发任务（job 000000000000）", "tool_calls": []},
            # 第 2 步：真的建任务 + respond 成功
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "create_job",
                 "arguments": {"mode": "t2i_moss", "prompt": "港风夜景"}}]},
            {"content": None, "tool_calls": [
                {"id": "c3", "name": "respond",
                 "arguments": {"summary": "已下发 MOSS 任务（job "
                                           + REAL_JOB_ID + "）",
                               "job_ids": [REAL_JOB_ID]}}]},
        ]
        result = self.run_agent_with_turns(turns)
        self.assertEqual(
            result.get("status"), "completed",
            "后面真的成功了，前面的误判却把整轮判成失败: "
            f"{result.get('error')!r}")
        self.assertNotEqual(result.get("code"), "fabricated_job_id")
        self.assertEqual([j.get("job_id") for j in result.get("jobs") or []],
                         [REAL_JOB_ID])


# ------------------------------------------------------------------ E. 边界


class TestBoundaryCases(_Base):
    """边界情形：并发多条任务 / 任务被删 / 无 id 的条目。"""

    def test_concurrent_jobs_any_of_them_makes_claim_valid(self):
        """并发建了多条任务 —— 报告其中**任意一条**都算真。"""
        ctx = self.ctx()
        ctx.jobs.append({"job_id": "aaaa00000001"})
        ctx.jobs.append({"job_id": "bbbb00000002"})
        text = "已下发任务（job bbbb00000002）"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(text, ctx.jobs))

    def test_deleted_job_is_still_fabricated(self):
        """任务被删了（库里查不到）-> 报告它仍算编造。"""
        ctx = self.ctx()
        ctx.started_at = 1000.0
        # 库里没有这个 id
        text = "已下发任务（job deadbeef1234）"
        self.assertTrue(
            agent_mod._looks_like_fabricated_job_claim(
                text, agent_mod._known_job_ids(ctx)))

    def test_entries_without_job_id_are_ignored(self):
        ctx = self.ctx()
        ctx.jobs.append({"mode": "t2i_moss"})       # 没有 job_id
        ctx.jobs.append({"job_id": ""})
        self.assertEqual(agent_mod._known_job_ids(ctx), [])

    def test_empty_real_list_keeps_old_behaviour(self):
        """真实任务为空时，行为与修复前一致（声称已下发 = 编造）。"""
        text = "已下发文生视频任务（job t2v_019d874a）"
        self.assertTrue(agent_mod._looks_like_fabricated_job_claim(text, []))

    def test_non_claim_text_is_never_fabricated(self):
        """没有「已下发」这类完成时声明的正文，不该被判编造。"""
        text = "已创建 1 个文生视频任务，job_id=abc123"
        self.assertFalse(
            agent_mod._looks_like_fabricated_job_claim(text, []))

    def test_normalize_strips_prefix_and_separators(self):
        self.assertEqual(
            agent_mod._normalize_job_id("job_ac8778a0-6a04"),
            "ac8778a06a04")
        self.assertEqual(
            agent_mod._normalize_job_id("AC8778A06A04"),
            "ac8778a06a04")


if __name__ == "__main__":
    unittest.main()
