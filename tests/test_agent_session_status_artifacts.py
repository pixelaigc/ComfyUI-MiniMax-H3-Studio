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
