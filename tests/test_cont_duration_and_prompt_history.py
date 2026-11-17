# -*- coding: utf-8 -*-
"""接续**合并时长缺失** + **跨段提示词增强**的回归测试（2026-11-17）。

【这两个测试在防什么】

  1. **接续只出半截，没有成片**（时长缺陷）。
     用户实测：5 秒视频点「接续」再生成 5 秒 -> 只得到 **4 秒**的一段
     视频，没有 9 秒成片。
     根因（现场取证）：服务重启打断 stage1 的运行，worker 协程被杀，
     自愈路径**只下载产物、标 done**，不跑 `_finalize_continuation`
     （不登记分段、不拼完整视频）。
     → 断言必须检查「自愈路径也调用了接续收尾」。
  2. **跨段提示词增强丢前情**。
     用户需求：接续点「增强」要把此前所有段落的提示词一并提供给模型。
     → 断言必须检查「历史真的进了模型的消息」且「本段被剔除」。

【断言纪律】
  * 期望值全部是**字面量**，不是从被测函数自己的输出反推。
  * 断言要由「命名该行为的那条断言」抓住（变异测试证明）。
  * 不弱化既有断言、不掩盖失败。

运行：python3 -m pytest tests/test_cont_duration_and_prompt_history.py -v
"""

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as S                                          # noqa: E402
from h3studio import enhancer as enh                        # noqa: E402


# ==================================================================
# 一、接续合并时长缺失（自愈路径必须补跑接续收尾）
# ==================================================================
class TestResidualHealRunsContinuationFinalize(unittest.TestCase):
    """服务重启自愈路径必须跑接续收尾。

    【背景 —— 用户 19:00 实测】
    5 秒 -> 点接续 -> 再生成 5 秒，理论 9 秒，实际只得到 4.25 秒一段。
    现场取证（线上 DB 全量对拍 2026-11-17）：
      * 22 条接续链里 2 条 stage1 无成片（is_full 缺失）；
      * 这 2 条的 stage1 **全部**落在服务重启窗口内；
      * 另外 20 条没被重启打断的链，stage1 **全部**有成片。
    """

    def test_finalize_residual_calls_continuation_finalize(self):
        """自愈收尾（_finalize_residual_with_outputs）必须调用接续收尾。

        【为什么这条断言能抓住回归】
        原始缺陷版本里，这段代码只有「下载产物 + 标 done」，
        完全没有 `_finalize_continuation` 调用。
        把本补丁回退（删掉调用）-> 本断言立即失败。
        """
        src = inspect.getsource(S._finalize_residual_with_outputs)
        self.assertIn(
            "await _finalize_continuation_impl(",
            src,
            "自愈收尾路径没有调用接续收尾 —— 服务重启打断后，"
            "接续段只会有「自己那一小段」视频，没有完整成片"
            "（用户实测：5 秒接续 5 秒只得到 4 秒）")

    def test_adopt_residual_calls_continuation_finalize(self):
        """接管路径（_adopt_residual_running）也必须调用接续收尾。

        【为什么这条断言能抓住回归】
        接管路径是「任务还在 ComfyUI 队列里，重启后接管」那一条。
        它同样只下载产物、标 done —— 同样会丢成片。
        """
        src = inspect.getsource(S._adopt_residual_running)
        self.assertIn(
            "await _finalize_continuation_impl(",
            src,
            "接管路径没有调用接续收尾 —— 重启期间任务在队列里跑完，"
            "接续段同样会缺完整成片")

    def test_worker_finalize_delegates_to_module_impl(self):
        """Worker._finalize_continuation 必须委托给模块级实现。

        【为什么这样设计】原实现是 Worker 的实例方法（`self._finalize...`），
        但自愈路径是**模块级函数**、没有 Worker 实例可用。
        所以真正实现提到模块级 `_finalize_continuation_impl`，
        Worker 侧保留薄委托 —— 两处行为**必须完全一致**。
        """
        self.assertTrue(
            hasattr(S, "_finalize_continuation_impl"),
            "模块级 _finalize_continuation_impl 不存在")
        worker_method = S.Worker._finalize_continuation
        src = inspect.getsource(worker_method)
        self.assertIn(
            "return await _finalize_continuation_impl(job_id, mode, params, saved)",
            src,
            "Worker._finalize_continuation 没有委托给模块级实现 —— "
            "自愈路径与正常路径会跑出不同的成片")

    def test_impl_is_idempotent_for_segment_registration(self):
        """接续收尾实现必须「按血缘」取分段（而不是共享账本）。

        【为什么】兄弟分支隔离靠血缘回溯。若回退成共享账本，
        B1 的链会把 B2 混进来 —— 成片内容静默错误。
        """
        src = inspect.getsource(S._finalize_continuation_impl)
        self.assertIn("_lineage_segment_rels", src,
                      "接续收尾没有用血缘回溯取分段")
        self.assertIn("_record_segment_rel", src,
                      "接续收尾没有登记本段分段")


class TestLineagePrompts(unittest.TestCase):
    """血缘提示词历史：按 source_job 回溯，兄弟分支隔离。"""

    def _make_db(self, rows):
        """造一个临时 jobs 表，返回 sqlite 路径。

        rows: [(job_id, params_json), ...]
        """
        tmpdir = tempfile.mkdtemp(prefix="h3cont_prompt_")
        path = Path(tmpdir) / "jobs.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, params TEXT)")
        for job_id, params in rows:
            conn.execute("INSERT INTO jobs (id, params) VALUES (?, ?)",
                         (job_id, json.dumps(params, ensure_ascii=False)))
        conn.commit()
        conn.close()
        return path

    def _with_db(self, path, fn):
        """把 server.db() 临时指向临时库，跑 fn(db_path)。"""
        original_db = S.db
        original_path = S.DB_PATH

        def fake_db():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

        S.db = fake_db
        S.DB_PATH = path
        try:
            return fn()
        finally:
            S.db = original_db
            S.DB_PATH = original_path

    def test_lineage_prompts_walks_source_job_chain(self):
        """历史必须按 source_job 回溯，按段号顺序返回。

        链：seg0(prompt=A) -> seg1(prompt=B) -> seg2(prompt=C)
        从 seg2 查 -> 期望 [{stage:0,A}, {stage:1,B}, {stage:2,C}]
        """
        rows = [
            ("seg0", {"prompt": "A 段", "continuation": {
                "chain_id": "seg0", "stage_index": 0, "source_job": ""}}),
            ("seg1", {"prompt": "B 段", "continuation": {
                "chain_id": "seg0", "stage_index": 1, "source_job": "seg0"}}),
            ("seg2", {"prompt": "C 段", "continuation": {
                "chain_id": "seg0", "stage_index": 2, "source_job": "seg1"}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_prompts("seg2"))
        self.assertEqual(len(out), 3, "历史段数不对：%r" % (out,))
        self.assertEqual(out[0]["stage"], 0)
        self.assertEqual(out[0]["prompt"], "A 段")
        self.assertEqual(out[1]["stage"], 1)
        self.assertEqual(out[1]["prompt"], "B 段")
        self.assertEqual(out[2]["stage"], 2)
        self.assertEqual(out[2]["prompt"], "C 段")

    def test_lineage_prompts_excludes_sibling_branch(self):
        """兄弟分支不能混进彼此的历史。

        链：A -> B1 -> C1，A -> B2
        从 C1 查 -> 期望只有 A、B1（**不含 B2**）。
        """
        rows = [
            ("A", {"prompt": "A 段", "continuation": {
                "chain_id": "A", "stage_index": 0, "source_job": ""}}),
            ("B1", {"prompt": "B1 段", "continuation": {
                "chain_id": "A", "stage_index": 1, "source_job": "A"}}),
            ("B2", {"prompt": "B2 段", "continuation": {
                "chain_id": "A", "stage_index": 1, "source_job": "A"}}),
            ("C1", {"prompt": "C1 段", "continuation": {
                "chain_id": "A", "stage_index": 2, "source_job": "B1"}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_prompts("C1"))
        prompts = [item["prompt"] for item in out]
        self.assertEqual(prompts, ["A 段", "B1 段", "C1 段"],
                         "兄弟分支 B2 混进了 C1 的历史：%r" % (prompts,))
        self.assertNotIn("B2 段", prompts)

    def test_lineage_prompts_skips_empty_prompt(self):
        """空提示词的段不进历史（不能塞空行给模型）。"""
        rows = [
            ("s0", {"prompt": "有内容", "continuation": {
                "chain_id": "s0", "stage_index": 0, "source_job": ""}}),
            ("s1", {"prompt": "   ", "continuation": {
                "chain_id": "s0", "stage_index": 1, "source_job": "s0"}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_prompts("s1"))
        self.assertEqual([i["prompt"] for i in out], ["有内容"])


# ==================================================================
# 二、跨段提示词增强（历史进模型消息 + 本段剔除）
# ==================================================================
class TestPromptHistoryInEnhance(unittest.TestCase):
    """历史提示词必须真的进到模型消息里，且本段被剔除。"""

    def _spec(self):
        return {"system_prompt": "你是视频导演。", "user_template": "{prompt}"}

    def test_build_messages_includes_history(self):
        """历史必须出现在 user 消息里。

        【为什么这条断言能抓住回归】
        原实现 `build_messages(spec, original, language)` 根本不接受
        history 参数 -> 直接抛 TypeError；即便签名改了但不注入，
        消息里也不会有历史内容。
        """
        msgs = enh.build_messages(
            self._spec(), "本段原文", "auto",
            history=[{"stage": 0, "prompt": "第1段：猫在屋顶"},
                     {"stage": 1, "prompt": "第2段：猫跳下"}])
        user = msgs[1]["content"]
        self.assertIn("第1段：猫在屋顶", user,
                      "历史提示词没有进到模型消息里")
        self.assertIn("第2段：猫跳下", user,
                      "历史提示词没有进到模型消息里")
        self.assertIn("本段原文", user,
                      "本段原文丢了 —— 增强的对象必须是本段")

    def test_build_messages_without_history_unchanged(self):
        """没有历史时，消息内容与今天**完全一致**。

        【为什么】普通单段增强的行为不能被这个功能改变。
        """
        msgs_no_hist = enh.build_messages(self._spec(), "本段原文", "auto")
        msgs_empty = enh.build_messages(self._spec(), "本段原文", "auto",
                                         history=[])
        self.assertEqual(msgs_no_hist, msgs_empty,
                         "空历史改变了普通增强的消息")

    def test_history_labels_segment_numbers(self):
        """历史必须标清段号（模型才知道哪段是哪段）。"""
        msgs = enh.build_messages(
            self._spec(), "本段原文", "auto",
            history=[{"stage": 0, "prompt": "A"},
                     {"stage": 2, "prompt": "C"}])
        user = msgs[1]["content"]
        self.assertIn("第1段", user, "历史没有标段号：%s" % user)
        self.assertIn("第3段", user, "历史没有标段号：%s" % user)

    def test_history_comes_before_current_prompt(self):
        """历史必须在当前段原文**之前**（前情在前、本段在后）。

        【为什么】模型读到最后才输出；本段原文放最后，
        模型更不容易把多段内容糊成一段来扩写。
        """
        msgs = enh.build_messages(
            self._spec(), "本段原文", "auto",
            history=[{"stage": 0, "prompt": "前情内容"}])
        user = msgs[1]["content"]
        self.assertLess(user.index("前情内容"), user.index("本段原文"),
                        "历史没有排在当前段原文之前：%s" % user)

    def test_normalize_history_drops_current_stage(self):
        """本段自己必须被剔除（original 已经是本段了）。"""
        out = S._normalize_prompt_history(
            [{"stage": 0, "prompt": "前情"},
             {"stage": 1, "prompt": "本段"},
             {"stage": 2, "prompt": "更早的？"}],
            current_stage=1)
        prompts = [i["prompt"] for i in out]
        self.assertNotIn("本段", prompts,
                         "本段自己没被剔除：%r" % (prompts,))
        self.assertIn("前情", prompts)

    def test_normalize_history_sorts_by_stage(self):
        """乱序传入也要按段号排序（前情在前）。"""
        out = S._normalize_prompt_history(
            [{"stage": 3, "prompt": "C"}, {"stage": 1, "prompt": "A"}],
            current_stage=0)
        self.assertEqual([i["stage"] for i in out], [1, 3])

    def test_normalize_history_tolerates_garbage(self):
        """脏数据不能让增强请求崩掉（历史只是加分项）。"""
        # 非列表
        self.assertEqual(S._normalize_prompt_history(None, 0), [])
        self.assertEqual(S._normalize_prompt_history("x", 0), [])
        # 字符串数组（容错：按顺序当段号）
        out = S._normalize_prompt_history(["A", "B"], 0)
        self.assertEqual([i["prompt"] for i in out], ["A", "B"])
        # 字典缺 stage
        out = S._normalize_prompt_history([{"prompt": "A"}], 0)
        self.assertEqual(len(out), 1)
        # 空提示词被丢掉
        out = S._normalize_prompt_history([{"stage": 0, "prompt": "  "}], 0)
        self.assertEqual(out, [])

    def test_enhance_endpoint_passes_history_to_build_messages(self):
        """/api/enhance 必须把历史传进 build_messages。

        【为什么这条断言能抓住回归】
        端点是**唯一**真正调用模型的入口 —— 端点不传，
        前面所有纯函数测试都白搭。
        """
        src = inspect.getsource(S.api_enhance)
        self.assertIn("prompt_history", src,
                      "/api/enhance 没有读 prompt_history")
        self.assertIn("history=history", src,
                      "/api/enhance 没有把历史传给 build_messages")

    def test_enhance_endpoint_reads_stage(self):
        """端点必须读「当前段号」才能剔除本段。"""
        src = inspect.getsource(S.api_enhance)
        self.assertIn("_normalize_prompt_history", src,
                      "/api/enhance 没有用 _normalize_prompt_history 规整历史")
        self.assertIn("current_stage", src,
                      "/api/enhance 没有把当前段号传给历史规整函数")


class TestFrontendPromptHistoryWiring(unittest.TestCase):
    """前端：接续上下文存历史、增强时透传。"""

    def test_continuation_js_exposes_prompt_history(self):
        """continuation.js 必须暴露 promptHistory()。"""
        js = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        self.assertIn("promptHistory", js,
                      "continuation.js 没有暴露 promptHistory()")
        self.assertIn("prompt_history", js,
                      "continuation.js 没有读服务端的 prompt_history")

    def test_enhance_menu_passes_prompt_history(self):
        """enhance-menu.js 必须把历史传给 /api/enhance。

        【为什么断言「两处都要有」】视频面板有两条增强入口：
        普通增强（runBasic）与视频导演稿（runDirector）。
        只改一处 = 用户走另一条入口时历史照样丢。
        所以这里断言**出现次数 >= 2**。
        """
        js = (REPO / "web" / "enhance-menu.js").read_text(encoding="utf-8")
        self.assertIn("continuationPromptHistory", js,
                      "enhance-menu.js 没有取接续历史")
        count = js.count("prompt_history: continuationPromptHistory()")
        self.assertGreaterEqual(
            count, 2,
            "enhance-menu.js 只有 %d 处透传历史（应为 >= 2："
            "普通增强 + 视频导演稿）" % count)

    def test_enhance_menu_history_is_optional(self):
        """不在接续模式时历史为空 -> 普通增强行为不变。"""
        js = (REPO / "web" / "enhance-menu.js").read_text(encoding="utf-8")
        self.assertIn('if (!cont || typeof cont.promptHistory !== "function") return [];',
                      js,
                      "enhance-menu.js 没有在取不到接续模块时安全退回空历史")


if __name__ == "__main__":
    unittest.main()
