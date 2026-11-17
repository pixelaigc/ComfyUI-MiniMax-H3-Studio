# -*- coding: utf-8 -*-
"""接续**成片时长**的回归测试（2026-11-17 用户更正口径）。

【用户口径 —— 这是权威口径】
    潜空间接续存在 1 秒重叠：每接续一段，实际新增时长 =
    该段请求时长 - 1 秒；重叠的 1 秒来自潜空间续接的**前段尾部帧**，
    与前段尾部重合，不重复计入。

    成片总时长 = Σ(各段请求时长) - (段数-1) × 1 秒

      第1段 5 秒              -> 成片 5 秒
      接续第2段（请求 5 秒）  -> 成片 = 5 + (5-1) = 9 秒
      再接续第3段（请求 8 秒）-> 成片 = 9 + (8-1) = 16 秒

【这个测试在防什么】
两类失败：
  1. **把「请求时长」当「新增时长」直接累加** —— 成片会多出每段 1 秒
     （5+5+8 = 18 秒，而正确是 16 秒）。
  2. **把重叠 1 秒当成整段截断/丢失** —— 用户实测过「5 秒接续 5 秒
     只得到 4 秒」。那不是「请求 5 秒只交付 4 秒」，而是**成片根本没
     拼出来**（服务重启打断 -> 自愈路径不跑接续收尾）。

【为什么实测值与用户口径有小幅差 —— 必须锁住】
H3 有两条**硬网格约束**（不是我们的选择）：
  * 段长只能落在 17k+5 帧网格：请求 5 秒 = 120 帧，模型只能出
    124 帧（= 5.1667 秒）—— 链首段因此比请求多 4 帧；
  * 潜空间拷贝桥只接受 5+17k 帧网格的钉住窗口
    （5/22/39/56/73/90/107/124）。**24 帧（= 整 1 秒）不在网格上**，
    Relay-Kit `relay_core.py:411` 会直接 raise。
    最接近 1 秒的合法窗口是 **22 帧 = 0.9167 秒**（nonebot 咏唱 h3
    的 seg1/seg2 同款）。

所以：
    实测成片 = Σ align(请求帧数) - (续接段数) × 22 帧
    与用户口径的差 = 网格余量 + (1 - 22/24) × (段数-1)

【真实产物对照（ffprobe 实测）】
    5s -> 5s       : 124 + 102 = 226 帧 = 9.4167s（口径 9.0000s）
    5s -> 5s -> 8s : 124 + 102 + 170 = 396 帧 = 16.5000s（口径 16.0000s）
    5s × 4 段      : 124 + 102×3 = 430 帧 = 17.9167s（线上真实产物）

运行：python3 -m pytest tests/test_cont_duration_semantics.py -v
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import server as S                                          # noqa: E402
from h3studio import continuation as cont                   # noqa: E402

FPS = 24


# ==================================================================
# 一、链级时长账（纯函数）
# ==================================================================
class TestChainDurationAccount(unittest.TestCase):
    """`cont.chain_duration_account` —— 成片时长账的唯一真源。"""

    def test_single_segment_delivers_full_request(self):
        """第 1 段没有上游 -> 交付整段（不扣重叠）。"""
        a = cont.chain_duration_account([5.0])
        self.assertEqual(len(a["segments"]), 1)
        seg = a["segments"][0]
        self.assertTrue(seg["is_head"])
        self.assertEqual(seg["overlap_frames"], 0,
                         "链首段不该有重叠")
        self.assertEqual(seg["delivered_frames"], 124,
                         "链首段应交付整段 124 帧")

    def test_two_segments_match_real_artifact(self):
        """5 秒 -> 接续 5 秒：成片 226 帧（真实产物 ffprobe 实测值）。

        【为什么锁 226 而不是 216】
        216 帧 = 9.000 秒是**用户口径**的值；226 帧是**实际产物**的值。
        两者差 10 帧 = 4 帧网格余量 + 2 帧（22 vs 24 帧重叠）。
        这里锁的是「实际产物」—— 因为成片文件是这么长的。
        """
        a = cont.chain_duration_account([5.0, 5.0])
        self.assertEqual(a["total_frames"], 226,
                         "5s+5s 的成片帧数不对（真实产物 = 226 帧）")
        self.assertAlmostEqual(a["total_seconds"], 226 / 24, places=6)

    def test_three_segments_match_real_artifact(self):
        """5 秒 -> 5 秒 -> 8 秒：成片 396 帧。"""
        a = cont.chain_duration_account([5.0, 5.0, 8.0])
        self.assertEqual(a["total_frames"], 396,
                         "5s+5s+8s 的成片帧数不对")
        self.assertAlmostEqual(a["total_seconds"], 396 / 24, places=6)

    def test_four_segments_match_online_real_chain(self):
        """5 秒 × 4 段：430 帧 —— 对齐线上真实 4 段链产物。

        【证据来源】线上 DB 里那条 4 段链（chain fec6e25740b9）
        的成片 `v-h3-studio-000035.mp4`，ffprobe 实测 430 帧。
        """
        a = cont.chain_duration_account([5.0, 5.0, 5.0, 5.0])
        self.assertEqual(a["total_frames"], 430,
                         "4 段链的成片帧数与线上真实产物（430）不符")

    def test_formula_follows_user_spec(self):
        """「按 1 秒重叠口径」的成片时长必须等于用户公式。"""
        # 第1段 5 秒 -> 成片 5 秒
        self.assertEqual(
            cont.chain_duration_account([5.0])["formula_seconds"], 5.0)
        # 接续第2段（请求 5 秒）-> 成片 = 5 + (5-1) = 9 秒
        self.assertEqual(
            cont.chain_duration_account([5.0, 5.0])["formula_seconds"], 9.0)
        # 再接续第3段（请求 8 秒）-> 成片 = 9 + (8-1) = 16 秒
        self.assertEqual(
            cont.chain_duration_account([5.0, 5.0, 8.0])["formula_seconds"],
            16.0)

    def test_overlap_is_22_frames_not_24(self):
        """重叠必须是 **22 帧**（0.9167 秒），不能是 24 帧。

        【为什么这条断言重要】24 帧（= 整 1 秒）**不是**合法的钉住
        窗口 —— Relay-Kit `relay_core.py:411` 会直接 raise：
        「24 帧不是 H3 的整步续接窗口」。若为了凑用户公式把
        context_frames 改成 24，整条接续链会在 ComfyUI 侧直接炸。
        """
        self.assertFalse(cont.valid_context_frames(24),
                         "24 帧居然是合法窗口了 —— Relay-Kit 网格变了？")
        self.assertTrue(cont.valid_context_frames(22))
        self.assertEqual(cont.steps_for_frames(22), 7)
        a = cont.chain_duration_account([5.0, 5.0])
        self.assertEqual(a["overlap_frames"], 22)
        self.assertAlmostEqual(a["overlap_seconds"], 22 / 24, places=6)

    def test_delta_explains_grid_constraints(self):
        """实测与用户口径的差必须被算出来（且 notes 说得清原因）。"""
        a = cont.chain_duration_account([5.0, 5.0])
        # 差 = 网格余量 8 帧 + (1 - 22/24) × 1 段 = 8 + 2 = 10 帧
        self.assertEqual(a["grid_slack_frames"], 8,
                         "网格余量算错：请求 5s 实际 124 帧，两段各多 4 帧")
        self.assertAlmostEqual(a["delta_seconds"], 10 / 24, places=6)
        self.assertTrue(a["notes"], "没有说明差值原因")

    def test_notes_mention_both_constraints(self):
        """notes 必须同时说清「帧网格」与「重叠窗口」两条原因。"""
        a = cont.chain_duration_account([5.0, 5.0])
        joined = " ".join(a["notes"])
        self.assertIn("17k+5", joined,
                      "notes 没提段长的 17k+5 帧网格约束")
        self.assertIn("重叠", joined,
                      "notes 没提段间重叠窗口的约束")

    def test_empty_chain_is_safe(self):
        """空链不能崩（返回 0 帧）。"""
        a = cont.chain_duration_account([])
        self.assertEqual(a["total_frames"], 0)
        self.assertEqual(a["formula_seconds"], 0.0)

    def test_grid_slack_zero_when_on_grid(self):
        """请求时长恰好落在网格上时没有余量（8 秒 -> 192 帧）。"""
        a = cont.chain_duration_account([8.0, 8.0])
        self.assertEqual(a["segments"][0]["frame_count"], 192)
        self.assertEqual(a["grid_slack_frames"], 0,
                         "8 秒 = 192 帧恰在网格上，不该有余量")


# ==================================================================
# 二、段级帧数账（continuation_frames 的 is_head 语义）
# ==================================================================
class TestSegmentFrameLedger(unittest.TestCase):
    """段级账：链首段不裁、续接段裁掉重叠。"""

    def test_continuation_segment_trims_overlap(self):
        """续接段：交付 = 总帧数 - 22。"""
        info = cont.continuation_frames(124, 22, is_head=False)
        self.assertEqual(info["delivered_frames"], 102)
        self.assertEqual(info["trim_frames"], 22)

    def test_head_segment_delivers_all(self):
        """链首段：交付 = 总帧数（不裁）。"""
        info = cont.continuation_frames(124, 22, is_head=True)
        self.assertEqual(info["delivered_frames"], 124)
        self.assertEqual(info["trim_frames"], 22,
                         "trim_frames 是「要裁多少」的协议值，与交付无关")

    def test_default_is_continuation_semantics(self):
        """默认必须是**续接段**语义（历史契约，不能改）。

        【为什么】这个函数历史上就是「算一段续接」的账。默认改成
        链首段语义会让所有既有调用方凭空多 22 帧。
        """
        info = cont.continuation_frames(124, 22)
        self.assertEqual(info["delivered_frames"], 102)


# ==================================================================
# 三、血缘时长收集（服务端）
# ==================================================================
class TestLineageDurations(unittest.TestCase):
    """`_lineage_durations` —— 按血缘收集各段请求时长。"""

    def _make_db(self, rows):
        tmpdir = tempfile.mkdtemp(prefix="h3dur_")
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
        original_db, original_path = S.db, S.DB_PATH

        def fake_db():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

        S.db = fake_db
        S.DB_PATH = path
        try:
            return fn()
        finally:
            S.db, S.DB_PATH = original_db, original_path

    def test_durations_follow_source_job_chain(self):
        """按 source_job 回溯，按段序返回。"""
        rows = [
            ("s0", {"duration": 5.0, "continuation": {
                "chain_id": "c", "stage_index": 0, "source_job": ""}}),
            ("s1", {"duration": 5.0, "continuation": {
                "chain_id": "c", "stage_index": 1, "source_job": "s0"}}),
            ("s2", {"duration": 8.0, "continuation": {
                "chain_id": "c", "stage_index": 2, "source_job": "s1"}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_durations("s2"))
        self.assertEqual(out, [5.0, 5.0, 8.0],
                         "血缘时长收集不对：%r" % (out,))

    def test_durations_exclude_sibling_branch(self):
        """兄弟分支不能混进时长列表。"""
        rows = [
            ("A", {"duration": 5.0, "continuation": {
                "chain_id": "A", "stage_index": 0, "source_job": ""}}),
            ("B1", {"duration": 5.0, "continuation": {
                "chain_id": "A", "stage_index": 1, "source_job": "A"}}),
            ("B2", {"duration": 7.0, "continuation": {
                "chain_id": "A", "stage_index": 1, "source_job": "A"}}),
            ("C1", {"duration": 8.0, "continuation": {
                "chain_id": "A", "stage_index": 2, "source_job": "B1"}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_durations("C1"))
        self.assertEqual(out, [5.0, 5.0, 8.0],
                         "兄弟分支 B2 混进了时长列表：%r" % (out,))

    def test_duration_falls_back_to_segment_frames(self):
        """duration 缺失时按 segment_frames 折算。"""
        rows = [
            ("s0", {"continuation": {
                "chain_id": "c", "stage_index": 0, "source_job": "",
                "segment_frames": 124}}),
        ]
        path = self._make_db(rows)
        out = self._with_db(path, lambda: S._lineage_durations("s0"))
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0], 124 / 24, places=6)


# ==================================================================
# 四、链级时长计划（服务端下发）
# ==================================================================
class TestChainDurationPlan(unittest.TestCase):
    """`_chain_duration_plan` —— 服务端下发的成片时长账。"""

    def _make_db(self, rows):
        tmpdir = tempfile.mkdtemp(prefix="h3plan_")
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
        original_db, original_path = S.db, S.DB_PATH

        def fake_db():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

        S.db = fake_db
        S.DB_PATH = path
        try:
            return fn()
        finally:
            S.db, S.DB_PATH = original_db, original_path

    def test_plan_includes_extra_next_segment(self):
        """extra_seconds 把「下一段」算进去。

        场景：链上已有 1 段 5 秒，用户点「接续」且面板时长仍是 5 秒
        -> 预期成片 = 9 秒（按口径）/ 226 帧（实际）。
        """
        rows = [("s0", {"duration": 5.0, "continuation": {
            "chain_id": "c", "stage_index": 0, "source_job": "",
            "segment_frames": 124, "context_frames": 22}})]
        path = self._make_db(rows)
        plan = self._with_db(
            path, lambda: S._chain_duration_plan("s0", extra_seconds=5.0))
        self.assertEqual(len(plan["segments"]), 2,
                         "下一段没被算进时长账")
        self.assertEqual(plan["total_frames"], 226)
        self.assertEqual(plan["formula_seconds"], 9.0,
                         "按 1 秒重叠口径的成片时长应为 9 秒")

    def test_plan_without_extra_covers_current_chain_only(self):
        """不给 extra 时只算到当前段。"""
        rows = [("s0", {"duration": 5.0, "continuation": {
            "chain_id": "c", "stage_index": 0, "source_job": "",
            "segment_frames": 124, "context_frames": 22}})]
        path = self._make_db(rows)
        plan = self._with_db(path, lambda: S._chain_duration_plan("s0"))
        self.assertEqual(len(plan["segments"]), 1)
        self.assertEqual(plan["total_frames"], 124)
        self.assertEqual(plan["formula_seconds"], 5.0)


# ==================================================================
# 五、前端展示
# ==================================================================
class TestFrontendDurationDisplay(unittest.TestCase):
    """前端要显示「按口径的成片时长」与「实际帧数」的差。"""

    def _render_panel_source(self):
        """只取 `renderPanel` 函数的源码（不含整个文件）。

        【为什么不能 grep 整个文件】`renderDurationRows` / `formula_seconds`
        这些标识符在**函数定义**里也有 —— grep 整个文件时，
        把调用删掉也照样能搜到 -> 断言变成装饰品。
        所以这里按函数体切片，只查「真正用到它的地方」。
        """
        js = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        start = js.index("function renderPanel(")
        # 到下一个顶层 function 声明为止
        end = js.index("\n  function ", start + 10)
        return js[start:end]

    def test_continuation_js_renders_duration_plan(self):
        """renderPanel 里必须真的调用 renderDurationRows。"""
        panel = self._render_panel_source()
        self.assertIn(
            "renderDurationRows(current.duration_plan)", panel,
            "renderPanel 没有调用 renderDurationRows —— "
            "成片时长账不会显示给用户")

    def test_continuation_js_shows_formula_and_actual(self):
        """两个数都要显示 —— 只显示一个，用户无从判断。

        【为什么 grep 函数体】`formula_seconds` / `total_seconds` 在
        `renderDurationRows` 的**定义**里也出现 —— 只查整个文件的话，
        把展示逻辑删了断言仍然会过。所以这里查 `renderDurationRows`
        的函数体。
        """
        js = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        start = js.index("function renderDurationRows(")
        end = js.index("\n  function ", start + 10)
        body = js[start:end]
        self.assertIn("formula_seconds", body,
                      "前端没显示「按 1 秒重叠口径」的成片时长")
        self.assertIn("total_seconds", body,
                      "前端没显示「实际帧数」的成片时长")
        self.assertIn("delta_seconds", body,
                      "前端没显示「两者相差多少」")

    def test_fetch_passes_next_duration(self):
        """取上下文时要把下一段的请求时长传给服务端。"""
        js = (REPO / "web" / "continuation.js").read_text(encoding="utf-8")
        start = js.index("async function fetchContext(")
        end = js.index("\n  function ", start + 10)
        body = js[start:end]
        self.assertIn("?duration=", body,
                      "取接续上下文时没带下一段的时长 —— "
                      "服务端算不出「接完之后成片多长」")

    def test_server_context_endpoint_exposes_duration_plan(self):
        """接续上下文必须下发 duration_plan。"""
        src = __import__("inspect").getsource(S.api_continuation_context)
        self.assertIn("duration_plan", src,
                      "/api/jobs/{id}/continuation 没有下发 duration_plan")
        self.assertIn("_chain_duration_plan", src,
                      "接续上下文没有调用成片时长账")


if __name__ == "__main__":
    unittest.main()
