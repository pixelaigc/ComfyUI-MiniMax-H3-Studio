# -*- coding: utf-8 -*-
"""
Agent「工具级误报 + 截断被显示成失败」的**回归测试**（2026-11-11）。

【本文件要锁住的缺陷】

用户新截图（/root/.hermes-web-ui/upload/coding/7d35e4e634a03368.png）：
  用户说「最后用 qwen-image21 试试」，界面显示
    * 工具调用（7）：✓ get_mode_schema / ✗ create_job ×5 / ✓ get_mode_schema
    * 消息徽章：黄色「已截断」
    * 底部条：**红色**「已达到步骤上限（8 步），返回已完成的部分结果」

【排查结论 —— 与用户预期不同，必须如实记录】

1. **那 5 个 ✗ 是如实的，不是误报。**
   生产会话库 `agent_sessions.db`（会话 06f7145d3dab4085）第 26~30 行，
   5 条 create_job 的工具结果全文都是「参数校验未通过：negative_prompt …」。
   权威核对 jobs 表（= server.DB_PATH，实测
   /root/projects/comfyui-minimax-h3-studio/data/jobs.db）：
   09:24 窗口内**零新增记录**（09:02:32 之后下一条是 11:41:00）。
   => 5 次 create_job 确实一个任务都没建成，标 ✗ 正确。

2. **真正的根因是「负面提示词不生效」被当成硬失败拦下任务。**
   `agent_schema.validate_params()` 把所有问题混在一个 issues 列表，
   上层只看到「非空」就 raise —— 于是**告知类**问题变成硬失败。
   而那段代码**自己的注释**写着「**不因为「该族不支持负面提示词」
   就拒绝任务**」，契约文档也写着「（**不拦任务**，只如实告知）」。
   实现与自己的注释、契约**相反**。
   后果链：任务被拒 -> 模型用同样参数重试 5 次 -> 撞 8 步上限 ->
   用户看到「5 个红色 ✗ + 红色条 + 已截断」。

3. **「步骤上限 / 截断」被渲染成红色失败条**（真缺陷）。
   后端把截断原因塞进 `error` 字段，前端 `renderError` 渲染成
   `.ag-error`（背景 #fdecec 纯红）—— 但语义是「返回已完成的部分结果」，
   是中性/警告，不是失败。徽章是黄色「已截断」，底部却是红条，自相矛盾。

【本文件覆盖】

  A. 工具级：真实建出任务的 create_job 必须显示 ✓（不被标 ✗）
  B. 工具级：工具说成功但库里查不到 -> 不得显示 ✓（假成功要挡住）
  C. 工具级：查库失败/无入口 -> 保持原判定（不能因查不到库翻转结论）
  D. 步骤上限截断 -> 不得显示成红色失败条，必须是中性提示条
  E. 真失败（status=failed）-> 仍然显示红色失败条（不得回退）
  F. 软问题（负面提示词默认不生效）-> **不拦任务**，照常建出并如实告知
  G. 硬问题（非法取值）-> 仍然拦下（拦截能力不得削弱）

运行：python3 -m pytest tests/test_agent_tool_status_fix.py -v
"""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from h3studio import agent as agent_mod          # noqa: E402
from h3studio import agent_sessions              # noqa: E402
from h3studio import agent_schema                # noqa: E402


WEB = REPO / "web"


def read_web(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


#: 生产实测：真实任务 id（jobs 表）
REAL_JOB_ID = "ac8778a06a04"
#: 生产实测：模型抄错的 id
TYPO_JOB_ID = "ac8778a6ea84"


# ------------------------------------------------------------------ 假服务端


class FakeServer:
    """
    Agent 依赖的服务端入口的最小替身。

    【关键点】它同时提供：
      * create_job_record —— 建任务时真的**落库**（jobs 表）
      * get_job_status_payload —— 权威查询（工具级复核用）
      * list_job_ids_since —— 权威来源（编造判定用）

    db_rows 是「真的落库的任务 id 集合」——
    工具级 ✓/✗ 判定必须以它为准（这就是本次修复的核心契约）。
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.db_path = tmp / "jobs.db"
        self.created = []
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
        import sqlite3
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
        """**权威来源**：jobs 表查询。"""
        return list(self.db_rows)

    def get_job_status_payload(self, job_id):
        """**权威来源**：单个任务查询（工具级复核用）。"""
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
        self.tmp = Path(tempfile.mkdtemp(prefix="h3agent-toolstatus-"))
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

    def run_agent_with_turns(self, turns, message="最后用 qwen-image21 试试",
                             max_steps=None):
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
                session_store=agent_sessions.get_store(),
                max_steps=max_steps or agent_mod.MAX_STEPS))
        finally:
            agent_mod.llm_client.call_provider_with_tools = original


# ============================================================ A. 工具级真成功


class TestToolLevelRealSuccessNotMarkedFailed(_Base):
    """
    场景 A：**真实建出任务**的 create_job 必须显示 ✓，不能被标 ✗。

    【为什么要测】用户要求核实「工具级 create_job 实际成功却被标 ✗」。
    实测结论是那 5 个 ✗ 属实，但判定链路有**结构性**隐患：
    工具的 ok 只来自「函数有没有抛异常」，任务建成了却在返回前抛异常
    就会被标 ✗。本类锁住「以 jobs 表为准」的复核逻辑。
    """

    def test_reconcile_uses_db_as_truth(self):
        """任务在库里 -> 即使工具声称失败，也判成功。"""
        ctx = self.ctx()
        # 模拟：任务真的建出来了（库里有一条），但工具随后抛了异常
        self.server.create_job_record("t2i", {"prompt": "x"})
        self.assertTrue(self.server.db_rows)
        ok = agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job",
            {"job_id": REAL_JOB_ID, "mode": "t2i", "status": "queued"},
            False)
        self.assertTrue(ok, "任务真在库里却被判失败 —— 这就是用户报的误报")

    def test_reconcile_keeps_original_when_job_not_in_db(self):
        """
        任务**查不到** -> 保持原判定，**不**翻成失败。

        【为什么锁这条 —— 2026-11-11 实测回归】
        先实现了「工具说成功但库里查不到 -> 判失败」，
        结果 tests/test_agent_oneshot_e2e.py 真回归：
        该测试的服务端替身 create_job_record 只往内存 append、**不写库**
        （这是合法的服务端实现形态），于是「查不到」被翻成失败 ->
        真实成功的工具块显示 ✗ —— 制造出的正是要消灭的那类误报。

        「库里查不到」无法区分「假成功」与「读不到」，所以不能翻转。
        """
        ctx = self.ctx()
        ok = agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job",
            {"job_id": "deadbeefcafe", "mode": "t2i", "status": "queued"},
            True)
        self.assertTrue(ok,
                        "查不到任务时不能把成功翻成失败（那正是要修的误报）")

    def test_reconcile_keeps_original_when_lookup_fails(self):
        """查库失败（抛异常）-> 保持原判定，不翻转结论。"""
        ctx = self.ctx()

        def boom(job_id):
            raise RuntimeError("DB 抖动")

        self.server.get_job_status_payload = boom
        self.assertTrue(agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job", {"job_id": REAL_JOB_ID}, True))
        self.assertFalse(agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job", {"job_id": REAL_JOB_ID}, False))

    def test_reconcile_keeps_original_when_server_has_no_lookup(self):
        """服务端没有查询入口 -> 保持原判定。"""
        ctx = self.ctx()
        self.server.get_job_status_payload = None
        self.assertTrue(agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job", {"job_id": REAL_JOB_ID}, True))
        self.assertFalse(agent_mod._reconcile_tool_ok_with_db(
            ctx, "create_job", {"job_id": REAL_JOB_ID}, False))

    def test_reconcile_ignores_non_job_tools(self):
        """检索类工具的成败与任务表无关 -> 不复核。"""
        ctx = self.ctx()
        self.assertFalse(agent_mod._reconcile_tool_ok_with_db(
            ctx, "search_assets", {"total": 0}, False))
        self.assertTrue(agent_mod._reconcile_tool_ok_with_db(
            ctx, "search_assets", {"total": 0}, True))

    def test_reconcile_handles_batch_create(self):
        """batch_create_jobs：created[] 里任一条在库里 -> 判成功。"""
        ctx = self.ctx()
        self.server.create_job_record("t2i", {"prompt": "x"})
        ok = agent_mod._reconcile_tool_ok_with_db(
            ctx, "batch_create_jobs",
            {"created": [{"job_id": REAL_JOB_ID}], "created_count": 1},
            False)
        self.assertTrue(ok)

    def test_orchestration_marks_real_job_as_success(self):
        """端到端：create_job 真的建出任务 -> steps[].ok 必须为 True。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2i", "prompt": "雨夜霓虹"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ]
        result = self.run_agent_with_turns(turns)
        create_steps = [s for s in result["steps"] if s.get("tool") == "create_job"]
        self.assertTrue(create_steps, f"没有 create_job 步骤：{result['steps']}")
        self.assertTrue(create_steps[0]["ok"],
                        f"真实建出任务却被标失败：{create_steps[0]}")
        self.assertTrue(self.server.db_rows, "任务没落库")


# ============================================================ B. 截断 != 失败


class TestTruncatedIsNotRenderedAsFailure(_Base):
    """
    场景 B：步骤上限截断**不得**显示成红色失败条。

    【实测】生产会话 06f7145d3dab4085（09:24:11）：
    徽章「已截断」（黄）+ 底部红条「已达到步骤上限（8 步），返回已完成的部分结果」。
    """

    def test_truncated_status_carries_neutral_notice(self):
        """后端：truncated 必须产出 notice（中性说明）。"""
        # 用**不同**参数调同一个工具 —— 否则会先撞上「重复调用打转」
        # （signature 相同 -> repeat_count -> REPEAT_LIMIT），
        # 那样截断原因就变成「重复调用」而不是「步骤上限」。
        #
        # 【2026-11-13 审查目标 4 语义变化】步数上限从 8 提到 50 之后，
        # 「跑满默认步数」需要 50 轮 —— 这里**显式**把上限调回 8，
        # 让本用例继续测「撞步数上限 -> truncated + notice」这条路径。
        # （默认上限另有专门用例覆盖，见 tests/test_agent_power.py。）
        modes = ["t2i", "t2i_qwen", "t2i_moss", "t2v", "flf", "r2v",
                 "t2a", "a2a", "i2i_qwen"]
        turns = [
            {"content": None, "tool_calls": [
                {"id": f"c{i}", "name": "get_mode_schema",
                 "arguments": {"mode": m}}]}
            for i, m in enumerate(modes)
        ]
        result = self.run_agent_with_turns(turns, max_steps=8)
        self.assertEqual(result["status"], "truncated",
                         f"预期截断，实际 {result['status']} / {result.get('error')}")
        self.assertTrue(result.get("notice"),
                        "截断必须产出 notice（中性说明），否则前端只能显示红条")
        self.assertIn("步骤上限", result["notice"])

    def test_failed_status_has_no_neutral_notice(self):
        """真正的失败仍然走 error，notice 为空（不得回退）。"""
        # 用真实的「未来时叙述」措辞（_NARRATION_MARKERS 里的词），
        # 让「模型只做口头承诺」这条拦截路径真正被触发。
        turns = [
            {"content": "我先增强提示词，然后我下发任务",
             "tool_calls": [], "raw": {}},
            {"content": "我先增强提示词，然后我下发任务",
             "tool_calls": [], "raw": {}},
            {"content": "我先增强提示词，然后我下发任务",
             "tool_calls": [], "raw": {}},
        ]
        result = self.run_agent_with_turns(turns)
        self.assertEqual(result["status"], "failed", result.get("error"))
        self.assertFalse(result.get("notice"),
                         "真失败不该有中性提示条")

    def test_frontend_routes_truncated_to_neutral_bar(self):
        """前端契约：truncated 走 .ag-notice，不进 .ag-error。"""
        src = read_web("agent.js")
        self.assertIn("ag-notice", src, "缺少中性提示条渲染")
        self.assertIn("renderStatusNote", src, "缺少按 status 分流的逻辑")
        # renderError 里必须显式排除 truncated
        block = src.split("function renderError(msg)")[1].split("\n  }")[0]
        self.assertIn('"truncated"', block,
                      "renderError 没排除 truncated —— 截断仍会显示红条")

    def test_frontend_has_notice_css(self):
        """样式契约：中性条用黄色系，不复用红色的 .ag-error。"""
        css = read_web("style.css")
        self.assertIn(".ag-notice", css, "缺少 .ag-notice 样式")
        self.assertIn(".ag-notice-text", css, "缺少 .ag-notice-text 样式")

    def test_frontend_notice_is_not_red(self):
        """中性条的配色不能是失败红。"""
        css = read_web("style.css").replace("\r\n", "\n")
        block = css.split(".ag-notice {")[1].split("}")[0]
        self.assertNotIn("#fdecec", block, "中性条用了失败红的底色")
        self.assertNotIn("#b3272b", block, "中性条用了失败红的字色")


# ============================================================ C. 软问题不拦


class TestSoftIssuesDoNotBlockTask(_Base):
    """
    场景 C：软问题（负面提示词默认不生效）**不拦任务**，照常建出来并如实告知。

    【实测】生产会话 06f7145d3dab4085，09:24:11 —— 用户说
    「最后用 qwen-image21 试试」，模型带了 negative_prompt，
    被 `negative_needs_cfg` 拦下 -> 任务一个都没建 ->
    模型重试 5 次 -> 撞 8 步上限。

    【契约依据】agent_schema.validate_params 的注释：
    「**不因为「该族不支持负面提示词」就拒绝任务**」；
    契约文档：「`negative_not_supported` | 400 | …（**不拦任务**，只如实告知）」。
    """

    def test_split_issues_classifies_correctly(self):
        issues = agent_schema.validate_params(
            "t2i_qwen", {"negative_prompt": "nsfw"})
        hard, soft = agent_schema.split_issues(issues)
        self.assertEqual([i.code for i in hard], [], "软问题被算成硬问题")
        self.assertEqual([i.code for i in soft], ["negative_needs_cfg"])

    def test_hard_issues_still_classified_as_hard(self):
        """非法取值必须仍是硬问题（拦截能力不得削弱）。"""
        issues = agent_schema.validate_params(
            "t2v", {"aspect": "21:10", "duration": 30})
        hard, soft = agent_schema.split_issues(issues)
        self.assertTrue(hard, "非法取值必须拦")
        self.assertEqual(soft, [])

    def test_negative_prompt_not_supported_is_soft(self):
        issues = agent_schema.validate_params(
            "t2v", {"negative_prompt": "nsfw"})
        hard, soft = agent_schema.split_issues(issues)
        self.assertEqual([i.code for i in hard], [])
        self.assertEqual([i.code for i in soft], ["negative_not_supported"])

    def test_resolution_upscaled_is_soft(self):
        """t2i 的 4k 会自动走官方 2K 放大路径 -> 允许（契约标「—（允许）」）。"""
        issues = agent_schema.validate_params("t2i", {"resolution": "4k"})
        hard, soft = agent_schema.split_issues(issues)
        self.assertEqual([i.code for i in hard], [])
        self.assertEqual([i.code for i in soft], ["resolution_upscaled"])

    def test_precheck_does_not_block_on_soft_issue(self):
        """编排层预校验放行软问题。"""
        ctx = self.ctx()
        issue = agent_mod._precheck_tool_args(
            "create_job",
            {"mode": "t2i_qwen", "prompt": "x", "negative_prompt": "nsfw"},
            ctx)
        self.assertIsNone(issue, f"软问题不该被预校验拦下：{issue}")

    def test_precheck_still_blocks_hard_issue(self):
        """编排层预校验照旧拦硬问题。"""
        ctx = self.ctx()
        issue = agent_mod._precheck_tool_args(
            "create_job",
            {"mode": "t2v", "prompt": "x", "aspect": "21:10"},
            ctx)
        self.assertIsNotNone(issue, "非法取值必须被拦")
        self.assertEqual(issue.get("code"), "invalid_params")

    def test_create_job_builds_task_despite_soft_issue(self):
        """端到端：负面提示词不生效 -> 任务**照常建出来** + 如实告知。"""
        ctx = self.ctx()
        result = self.call("create_job", {
            "mode": "t2i_qwen", "prompt": "海边日落",
            "negative_prompt": "nsfw",
        }, ctx=ctx)
        self.assertTrue(result.get("job_id"), f"任务没建出来：{result}")
        self.assertTrue(self.server.db_rows, "任务没落库")
        # 如实告知
        self.assertTrue(result.get("notice"), f"缺少如实告知：{result}")
        self.assertIn("negative_prompt", result["notice"]["message"])

    def test_orchestration_create_job_step_is_ok_with_soft_issue(self):
        """端到端：软问题不再让 create_job 显示 ✗。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2i_qwen", "prompt": "海边日落",
                               "negative_prompt": "nsfw"}}]},
            {"content": "好了", "tool_calls": [], "raw": {}},
        ]
        result = self.run_agent_with_turns(turns)
        create_steps = [s for s in result["steps"] if s.get("tool") == "create_job"]
        self.assertTrue(create_steps, f"没有 create_job 步骤：{result['steps']}")
        self.assertTrue(create_steps[0]["ok"],
                        f"软问题导致工具被标失败：{create_steps[0]}")
        self.assertTrue(self.server.db_rows, "任务没落库")

    def test_hard_issue_still_blocks_and_reports_error(self):
        """硬问题仍然拦下，且错误文案保留「参数校验未通过」。"""
        turns = [
            {"content": None, "tool_calls": [
                {"id": "c1", "name": "create_job",
                 "arguments": {"mode": "t2v", "prompt": "x",
                               "aspect": "21:10"}}]},
            {"content": None, "tool_calls": [
                {"id": "c2", "name": "respond",
                 "arguments": {"summary": "好"}}]},
        ]
        result = self.run_agent_with_turns(turns)
        step = [s for s in result["steps"] if s.get("tool") == "create_job"][0]
        self.assertFalse(step["ok"], "非法取值不该算成功")
        self.assertIn("参数校验未通过", step["error"])
        self.assertEqual(self.server.created, [], "非法取值不该建出任务")


# ============================================================ D. 编造拦截不削弱


class TestFabricatedBlockingStillWorks(_Base):
    """
    场景 D：本轮修复**不得**削弱「真编造 job id」的拦截能力。

    （上一轮修复引入的权威来源 + 容错比对，本次保持不变。）
    """

    def test_genuine_fabricated_id_is_still_blocked(self):
        text = "已下发文生视频任务（job t2v_019d874a）"
        self.assertTrue(agent_mod._looks_like_fabricated_job_claim(
            text, [{"job_id": REAL_JOB_ID}]))

    def test_real_typo_id_is_still_allowed(self):
        text = f"已下发 MOSS-Image 文生图任务（job {TYPO_JOB_ID}）"
        self.assertFalse(agent_mod._looks_like_fabricated_job_claim(
            text, [{"job_id": REAL_JOB_ID}]))

    def test_orchestration_blocks_genuine_fabrication(self):
        """端到端：模型声称下了任务但没建 -> 必须拦截（failed）。"""
        turns = [
            {"content": "已下发文生视频任务（job t2v_019d874a）",
             "tool_calls": [], "raw": {}},
            {"content": "已下发文生视频任务（job t2v_019d874a）",
             "tool_calls": [], "raw": {}},
            {"content": "已下发文生视频任务（job t2v_019d874a）",
             "tool_calls": [], "raw": {}},
        ]
        result = self.run_agent_with_turns(turns)
        self.assertEqual(result["status"], "failed",
                         f"真编造必须被拦：{result}")
        self.assertEqual(result.get("code"), "fabricated_job_id")


if __name__ == "__main__":
    unittest.main()
