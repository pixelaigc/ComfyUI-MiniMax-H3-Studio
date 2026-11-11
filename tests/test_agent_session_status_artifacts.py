# -*- coding: utf-8 -*-
"""
Agent 会话管理 —— 第二轮补齐（会话状态 / 创建时间 / 产物分辨率 / 大图预览 / 定位真生效）。

【这一轮补的是什么 —— 2026-11-12】
上一轮把「会话持久化 + 列表 + 新建 + 删除 + 产物观测」做完了，
但逐条核对需求原文时发现还有 3 处没到位：

  1. 需求 2 要求列表展示「创建/最后更新时间 … 状态」——
     **创建时间**后端一直有（created_at）但前端丢弃了；
     **状态**后端压根没有这个字段。
  2. 需求 5 要求产物面板能「点击产物可跳转到生成库对应条目或打开大图/预览」——
     「在生成库中定位」按钮把 id 写进 location.hash，但**全仓 0 处消费者**，
     按钮实际只是切了页面，**没有定位**（假功能）；
     缩略图也**不可点**，没有大图预览。
  3. 需求 5 要求产物显示「生成参数」——
     面板没有分辨率信息（档位字符串与实际像素对不上）。

【本文件的分层】
  A. 后端：会话状态语义（_session_status）
  B. 前端契约：列表字段 / 产物分辨率 / 大图预览 / 定位走真实通道
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import agent_sessions  # noqa: E402


# ==================================================================
# A. 后端：会话状态语义
# ==================================================================

class TestSessionStatus(unittest.TestCase):
    """会话状态字段的判定规则（2026-11-12 新增）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = agent_sessions.SessionStore(self.tmp / "agent_sessions.db")

    def tearDown(self):
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def _one(self, sid):
        items = self.store.list_sessions()
        hit = [s for s in items if s["id"] == sid]
        self.assertEqual(len(hit), 1, "会话没出现在列表里")
        return hit[0]

    # ---------------------------------------------------------- 空会话

    def test_empty_session_status(self):
        """刚建、没发过消息 -> 空会话。"""
        sid = self.store.create()
        item = self._one(sid)
        self.assertEqual(item["status"], "empty")
        self.assertEqual(item["status_label"], "空会话")

    # ---------------------------------------------------------- 正常完成

    def test_completed_status(self):
        """正常完成的一轮 -> 完成。"""
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "好的", meta={"status": "completed"})
        item = self._one(sid)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["status_label"], "完成")

    def test_status_ignores_trailing_tool_messages(self):
        """
        状态不能被「最后一条是 tool 消息」带偏。

        【为什么这条重要 —— 2026-11-12 实测踩到】
        一轮交互的落库顺序是 user -> assistant -> tool(*)。
        如果按「最后一条任意角色的消息」取状态，
        正常完成的一轮最后一条是 tool（不带 meta.status），
        状态会全部算成「待补充」—— 而每一轮其实都正常完成了。
        """
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "好的", meta={"status": "completed"})
        self.store.add_message(sid, "tool", "create_job: ok", tool_name="create_job")
        self.store.add_message(sid, "tool", "list_modes: ok", tool_name="list_modes")
        item = self._one(sid)
        self.assertEqual(item["status"], "completed",
                         "tool 消息把状态带偏了")
        self.assertEqual(item["status_label"], "完成")

    # ---------------------------------------------------------- 异常态

    def test_truncated_status_is_not_failed(self):
        """
        截断是**部分成功**，不能标成失败。

        截断 = 达到步骤上限但**返回了已完成的部分结果**。
        标红会让用户以为白干了（本项目修过这个语义错位）。
        """
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "部分结果",
                               meta={"status": "truncated"})
        item = self._one(sid)
        self.assertEqual(item["status"], "truncated")
        self.assertEqual(item["status_label"], "已截断")
        self.assertNotEqual(item["status"], "failed")

    def test_failed_status(self):
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "出错了",
                               meta={"status": "failed"})
        self.assertEqual(self._one(sid)["status"], "failed")

    def test_needs_clarification_status(self):
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "你要什么比例？",
                               meta={"status": "needs_clarification"})
        item = self._one(sid)
        self.assertEqual(item["status"], "needs_clarification")
        self.assertEqual(item["status_label"], "待补充")

    # ---------------------------------------------------------- 老会话

    def test_legacy_session_without_meta_is_completed(self):
        """
        迁移前建的老会话（meta 为空）-> 归为「完成」。

        老会话只存了纯文本，拿不到状态**不代表出问题**。
        标成「待补充」会让用户以为要补什么。
        """
        sid = self.store.create()
        self.store.add_message(sid, "user", "老消息")
        # 没写 meta（模拟迁移前的老数据）
        item = self._one(sid)
        self.assertEqual(item["status"], "completed")
        self.assertEqual(item["status_label"], "完成")

    def test_bad_meta_json_does_not_break_status(self):
        """坏 meta（半截 JSON）不能让整个会话列表打不开。"""
        sid = self.store.create()
        self.store.add_message(sid, "user", "x")
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages (session_id, role, content, tool_name, meta, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (sid, "assistant", "坏了", "", "{半截JSON", 1.0))
        item = self._one(sid)
        self.assertEqual(item["status"], "completed")

    # ---------------------------------------------------------- 失败链路

    def test_failed_request_is_not_reported_as_completed(self):
        """
        失败的请求必须显示成「失败」，不能是「完成」。

        【回归点 —— 2026-11-12 实测发现】
        一次性接口（POST /api/agent/chat）的异常分支原来这样落库：
            store.add_message(session_id, "assistant", f"[请求失败] {exc.message}")
        —— **不带 meta**。于是这条消息的 meta 是空的，
        会话状态判定拿不到 status -> 回落到「完成」。
        结果：**Provider 超时的会话，列表里显示绿色「完成」**。

        修法在源头：失败落库时带 meta={"status": "failed", ...}。
        这里锁住「失败 -> failed」这条映射。
        """
        sid = self.store.create()
        self.store.add_message(sid, "user", "做段视频")
        self.store.add_message(sid, "assistant", "[请求失败] Provider 超时",
                               meta={"status": "failed", "error": "Provider 超时"})
        item = self._one(sid)
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["status_label"], "失败")

    def test_server_failure_path_stores_failed_status(self):
        """
        服务端的失败分支必须真的写 meta.status=failed（静态契约层）。

        【为什么要静态查源码 —— 2026-11-12】
        上面那条测的是「映射对不对」；这条测的是
        「服务端那条失败分支有没有真的带 meta」。
        真跑一次异常路径要 mock Provider，成本高；
        而这次的 bug 恰恰是「忘了传 meta」——
        这种漏参，静态查调用点比跑异常路径更直接。
        """
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        # 两处失败落库（AgentError 分支 + 通用 Exception 分支）
        # 都必须带 meta，且 meta 里必须有 status=failed。
        self.assertIn('meta={"status": "failed"', server)
        self.assertGreaterEqual(
            server.count('meta={"status": "failed"'), 2,
            "一次性接口的两个失败分支都应带上 status=failed 的 meta")

    def test_sse_failure_result_already_carries_failed_status(self):
        """
        SSE 失败链路的 result 本身带 status=failed（无需额外处理）。

        守住这个契约：将来有人改了 agent.py 的失败返回，
        会话列表的「失败」状态会静默退化成「完成」。
        """
        agent = (ROOT / "h3studio" / "agent.py").read_text(encoding="utf-8")
        self.assertIn('"status": "failed"', agent)

    # ---------------------------------------------------------- 列表字段完整性

    def test_list_includes_created_at_and_status_fields(self):
        """需求 2 要求的时间与状态字段必须在列表里。"""
        sid = self.store.create()
        self.store.add_message(sid, "user", "x")
        item = self._one(sid)
        for key in ("id", "title", "created_at", "updated_at",
                    "message_count", "artifact_count", "status", "status_label"):
            self.assertIn(key, item, f"列表项缺少 {key}")
        self.assertGreater(item["created_at"], 0)

    def test_status_labels_are_unique_and_cover_all_statuses(self):
        """每个状态都有唯一的中文标签（前端直接显示，不自己翻译）。"""
        labels = agent_sessions.SESSION_STATUS_LABELS
        self.assertEqual(len(labels), len(set(labels.values())),
                         "有两个状态用了同一个中文标签")
        for key in ("empty", "completed", "truncated", "failed",
                    "needs_clarification"):
            self.assertIn(key, labels)
            self.assertTrue(labels[key].strip(), f"{key} 没有中文标签")

    def test_search_also_returns_status(self):
        """搜索命中的会话也要带状态（前端列表共用一套渲染）。"""
        sid = self.store.create()
        self.store.add_message(sid, "user", "赛博朋克雨夜")
        self.store.add_message(sid, "assistant", "好", meta={"status": "failed"})
        items = self.store.list_sessions(query="赛博朋克")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "failed")


# ==================================================================
# A2. 后端：未完成任务数（pending_count）
# ==================================================================

class TestPendingCount(unittest.TestCase):
    """
    会话列表的「N 个生成中」——统计的是**任务**，不是会话的运行状态。

    【为什么需要它】
    会话的「状态」字段只描述这一轮对话的收尾方式，不含「进行中」——
    服务端**没有** per-session 运行注册表（AGENT_GATE 是全局信号量，
    不记录是哪个会话在跑），跨会话无从判断。
    但「这个会话还有任务在生成」是**任务**层面的客观事实，可以单独显示。
    """

    @classmethod
    def setUpClass(cls):
        cls.server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        cls.js = (ROOT / "web" / "agent_sessions.js").read_text(encoding="utf-8")

    def test_session_job_stats_exists_and_is_additive(self):
        """
        必须**新增** session_job_stats，不改 session_job_counts。

        session_job_counts 返回 {sid: int}，被既有测试按整数用；
        改成 dict-of-dict 是 breaking change。
        """
        self.assertIn("def session_job_counts(", self.server_src)
        self.assertIn("def session_job_stats(", self.server_src)
        # 老函数的返回类型必须还是 dict[str, int]
        idx = self.server_src.index("def session_job_counts(")
        block = self.server_src[idx:idx + 2000]
        self.assertIn("-> dict[str, int]", block)

    def test_pending_counts_queued_and_running_only(self):
        """pending 只算 queued + running（done/failed/canceled 不算）。"""
        idx = self.server_src.index("def session_job_stats(")
        block = self.server_src[idx:idx + 1800]
        self.assertIn("status IN ('queued','running')", block)

    def test_sessions_api_returns_pending_count(self):
        """列表接口要返回 pending_count。"""
        idx = self.server_src.index("async def api_agent_sessions(")
        block = self.server_src[idx:idx + 2000]
        self.assertIn("pending_count", block)
        self.assertIn("session_job_stats", block)

    def test_frontend_renders_pending_count(self):
        """前端要渲染「N 个生成中」。"""
        self.assertIn("pending_count", self.js)
        self.assertIn("ag-session-item-pending", self.js)
        self.assertIn("生成中", self.js)

    def test_pending_wording_refers_to_tasks_not_session_running(self):
        """
        文案必须指向**任务**，不能说「会话进行中」。

        「这个会话进行中」是**假的** —— 服务端没有 per-session 运行态。
        说「N 个生成中」（指任务）才是事实。
        """
        idx = self.js.index("ag-session-item-pending")
        snippet = self.js[max(0, idx - 400):idx + 200]
        self.assertIn("生成中", snippet)
        self.assertNotIn("会话进行中", snippet)
        self.assertNotIn("会话正在", snippet)

    def test_artifact_count_wording_kept_for_requirement_compat(self):
        """
        「产物数」这个措辞要保留（需求原文用词），但要有 tooltip 说明口径。

        口径真相：artifact_count 数的是**任务数**（每个任务产出一个作品）。
        直接改成「任务」会偏离需求用词，也会让既有验收断言失效；
        改成「产物」+ tooltip 说明，是两者兼顾。
        """
        self.assertIn("产物", self.js)
        self.assertIn("该会话创建的任务数", self.js)


# ==================================================================
# B. 前端契约
# ==================================================================

class TestFrontendSessionStatus(unittest.TestCase):
    """前端必须真的把「创建时间」和「状态」显示出来。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "web" / "agent_sessions.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_session_list_renders_created_at(self):
        """列表项必须渲染创建时间（后端一直有，前端之前丢弃了）。"""
        self.assertIn("created_at", self.js)
        self.assertIn("ag-session-item-created", self.js)
        self.assertIn("formatDate", self.js)

    def test_created_at_uses_absolute_date_not_relative(self):
        """
        创建时间必须是**绝对日期**，不能复用相对时间格式化。

        相对时间（"3-12"）过几个月还是 "3-12" —— 用户无法判断是哪一年。
        """
        self.assertIn("function formatDate", self.js)
        # formatDate 必须带年份（同年可省，但要有年份的分支）
        self.assertIn("getFullYear", self.js)

    def test_session_list_renders_status_badge(self):
        """列表项必须渲染状态徽章。"""
        self.assertIn("ag-session-status", self.js)
        self.assertIn("status_label", self.js)
        self.assertIn("statusClass", self.js)

    def test_status_class_maps_to_existing_badge_colors(self):
        """
        状态徽章必须复用既有的 .ag-badge-* 配色类。

        另起一套颜色 = 两处样式漂移（对话气泡里的徽章和列表里的徽章
        一个绿一个黄）。
        """
        for cls in ("ok", "warn", "bad"):
            self.assertIn(f"ag-badge-{cls}", self.css)

    def test_session_meta_row_is_flex_wrap(self):
        """
        列表 meta 行必须允许换行。

        一行里要放下「最后更新 / 创建时间 / 消息数 / 产物数 / 状态徽章」5 样，
        375px 窄屏下 nowrap 必然横向溢出。
        """
        idx = self.css.index(".ag-session-item-meta")
        block = self.css[idx:idx + 400]
        self.assertIn("flex-wrap", block,
                      "meta 行没有 flex-wrap，窄屏会横向溢出")


class TestFrontendArtifactImprovements(unittest.TestCase):
    """产物面板：分辨率 / 大图预览 / 定位真生效。"""

    @classmethod
    def setUpClass(cls):
        cls.js = (ROOT / "web" / "agent_sessions.js").read_text(encoding="utf-8")
        cls.app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_artifact_shows_resolution(self):
        """产物卡片必须显示分辨率/像素信息。"""
        self.assertIn("artifactResolution", self.js)
        self.assertIn("ag-artifact-meta", self.js)

    def test_resolution_prefers_actual_pixels_over_tier_string(self):
        """
        分辨率显示必须「实际像素优先」。

        params.resolution 是档位字符串（"544p"），
        文生视频面板实际跑的是 1344×768 —— 显示档位就是错的。
        """
        idx = self.js.index("function artifactResolution")
        block = self.js[idx:idx + 1400]
        self.assertIn("resolution_info", block,
                      "没有优先用服务端算好的实际像素标签")
        self.assertIn("width", block)
        self.assertIn("height", block)

    def test_artifact_thumbnail_opens_large_image(self):
        """图片产物必须能点开大图（需求 5：「打开大图/预览」）。"""
        self.assertIn("preview-artifact", self.js)
        self.assertIn("previewArtifact", self.js)

    def test_preview_reuses_existing_lightbox(self):
        """
        大图预览必须复用 app.js 已有的 lightbox，不能新造模态框。

        自己写一个功能更少的模态框 = 两份大图查看器必然漂移。
        """
        self.assertIn("lightboxShow", self.js)
        self.assertIn("lightboxShow", self.app)
        self.assertIn("window.H3Studio", self.app)

    def test_locate_job_uses_real_channel_not_dead_hash(self):
        """
        「在生成库中定位」必须走**真能生效**的通道。

        【回归点】原实现只写 location.hash，全仓 0 处消费者 ->
        按钮写着「定位」却只切了页面。这是假功能。
        """
        self.assertIn("locateJobInLibrary", self.js)
        self.assertIn("locateJobInLibrary", self.app)

    def test_locate_channel_exists_in_app_js(self):
        """app.js 必须真的实现定位通道（滚动 + 高亮）。"""
        self.assertIn("function locateJobInLibrary", self.app)
        self.assertIn("consumePendingLocate", self.app)
        self.assertIn("is-located", self.app)

    def test_locate_uses_existing_filter_truth_source(self):
        """
        定位必须复用既有的筛选真源（applyDoneFilters）。

        自己写一遍 state.doneFilters = {...} 就是第二份状态，必然漂移。
        """
        idx = self.app.index("function locateJobInLibrary")
        block = self.app[idx:idx + 1600]
        self.assertIn("applyDoneFilters", block,
                      "定位没有走既有的筛选真源")
        # 只看**代码行**（去掉注释）—— 注释里提到 state.doneFilters
        # 是为了解释「为什么不自己写」，不能被当成真的赋值。
        code = "\n".join(line for line in block.splitlines()
                         if not line.strip().startswith("//"))
        self.assertNotIn("state.doneFilters = {", code,
                         "定位自己写了一份 doneFilters 状态")

    def test_located_card_highlight_style_exists(self):
        """定位命中的卡片要有可见高亮样式。"""
        self.assertIn(".job-card.is-located", self.css)

    def test_audio_artifact_not_rendered_as_video(self):
        """
        音频产物不能渲染成 <video>。

        原实现把 audio 和 video 合并成 is-video，
        浏览器拿 mp3 当视频播 -> 空白黑框，用户以为坏了。
        """
        self.assertIn("is-audio", self.js)
        self.assertIn(".ag-artifact-thumb.is-audio", self.css)

    def test_artifact_actions_row_exists(self):
        """产物卡片要有操作行（查看大图 + 在生成库中定位）。"""
        self.assertIn("ag-artifact-actions", self.js)
        self.assertIn(".ag-artifact-actions", self.css)


if __name__ == "__main__":
    unittest.main()
