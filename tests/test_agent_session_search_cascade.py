# -*- coding: utf-8 -*-
"""
会话管理 —— 新增需求（搜索/过滤、产物计数、级联清理、实时刷新）的回归测试。

覆盖：
  A. 会话列表：搜索/过滤（标题 + 消息正文）、产物计数
  B. 删除会话：级联清理产物**引用关系**，但**不删任务、不碰素材库**
  C. 前端契约：搜索框 / 定位按钮 / 轮询条件

【为什么级联清理只清引用、不删任务 —— 见 clear_session_job_refs 的说明】
任务同时是「生成库」里的一条记录；删掉它会让用户在生成列表里凭空少一条。
素材库实体文件在 /mnt/usb2t/asset_library，与会话完全无关。
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


class _ServerEnv:
    """隔离的 server 模块环境（独立 jobs.db + 独立会话库）。"""

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self._old = os.environ.get("H3STUDIO_DATA_DIR")
        os.environ["H3STUDIO_DATA_DIR"] = str(self.tmp / "studio")
        sys.modules.pop("server", None)
        self.server = importlib_import("server")
        self.server.init_db()
        # 会话库也隔离（默认在 data/agent_sessions.db，跟着 DATA_DIR 走）
        self.store = agent_sessions.SessionStore(
            self.tmp / "studio" / "agent_sessions.db")

    def close(self):
        if self._old is None:
            os.environ.pop("H3STUDIO_DATA_DIR", None)
        else:
            os.environ["H3STUDIO_DATA_DIR"] = self._old
        sys.modules.pop("server", None)


def importlib_import(name):
    import importlib
    return importlib.import_module(name)


# ---------------------------------------------------------------- A. 搜索/过滤

class TestSessionSearch(unittest.TestCase):
    """会话列表的搜索/过滤。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3sess-search-"))
        self.store = agent_sessions.SessionStore(self.tmp / "s.db")

    def test_empty_query_returns_all(self):
        """空查询 = 不过滤，返回全部会话。"""
        for i in range(3):
            sid = self.store.create(None)
            self.store.add_message(sid, "user", f"会话 {i}")
        self.assertEqual(len(self.store.list_sessions(query="")), 3)

    def test_search_by_title(self):
        """能按标题搜到。"""
        sid = self.store.create(None)
        self.store.add_message(sid, "user", "帮我做一段雨夜的赛博朋克视频")
        other = self.store.create(None)
        self.store.add_message(other, "user", "用素材库里的林黛玉做一段电影场景")

        hits = self.store.list_sessions(query="赛博朋克")
        self.assertEqual([h["id"] for h in hits], [sid])

    def test_search_by_message_body(self):
        """
        能按**消息正文**搜到（不只标题）。

        【为什么必须有这条】标题只是首条用户消息的前 40 字。
        用户后来聊的内容（「改成 16:9」「再生成一版」）根本不在标题里 ——
        只搜标题等于搜不到用户真正想找的东西。
        """
        sid = self.store.create(None)
        self.store.add_message(sid, "user", "做一个视频")
        self.store.add_message(sid, "assistant", "好的")
        self.store.add_message(sid, "user", "改成 16:9 的比例")

        hits = self.store.list_sessions(query="16:9")
        self.assertEqual([h["id"] for h in hits], [sid])

    def test_search_case_insensitive(self):
        """搜索不区分大小写（用户输入 "MOSS" 应能命中 "moss"）。"""
        sid = self.store.create(None)
        self.store.add_message(sid, "user", "换成 MOSS 模型看看")
        self.assertEqual(len(self.store.list_sessions(query="moss")), 1)
        self.assertEqual(len(self.store.list_sessions(query="MOSS")), 1)

    def test_search_no_match_returns_empty(self):
        """搜不到就是空列表（不是报错，也不是返回全部）。"""
        sid = self.store.create(None)
        self.store.add_message(sid, "user", "雨夜视频")
        self.assertEqual(self.store.list_sessions(query="不存在的关键词"), [])

    def test_search_escapes_like_wildcards(self):
        """
        搜 `%` 不应该匹配全部（SQL LIKE 通配符必须被转义）。

        【为什么】用户输入 `%` 时如果没转义，LIKE '%%%' 会匹配**所有**会话 ——
        搜索框看起来「什么都没输入却列出全部」，是典型的 LIKE 注入/误用。
        """
        for i in range(3):
            sid = self.store.create(None)
            self.store.add_message(sid, "user", f"会话 {i}")
        hits = self.store.list_sessions(query="%")
        self.assertEqual(hits, [], "% 不应匹配任何会话")


class TestArtifactCount(unittest.TestCase):
    """会话列表带关联产物数。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3sess-count-"))
        self.env = _ServerEnv(self.tmp)

    def tearDown(self):
        self.env.close()

    def test_session_job_counts(self):
        """产物数按会话统计。"""
        s1 = self.env.server.create_job_record("t2i", {"prompt": "a"}, "sess-A")
        self.env.server.create_job_record("t2i", {"prompt": "b"}, "sess-A")
        self.env.server.create_job_record("t2v", {"prompt": "c"}, "sess-B")
        counts = self.env.server.session_job_counts(["sess-A", "sess-B"])
        self.assertEqual(counts["sess-A"], 2)
        self.assertEqual(counts["sess-B"], 1)

    def test_session_job_counts_empty_and_unknown(self):
        """空列表 / 未知会话 -> 空 dict 或 0，不报错。"""
        self.assertEqual(self.env.server.session_job_counts([]), {})
        self.assertEqual(self.env.server.session_job_counts(["nope"]), {})

    def test_list_sessions_includes_artifact_count(self):
        """会话列表接口要带上 artifact_count。"""
        sid = self.env.store.create(None)
        self.env.store.add_message(sid, "user", "做图")
        self.env.server.create_job_record("t2i", {"prompt": "x"}, sid)

        sessions = self.env.store.list_sessions()
        self.assertEqual(sessions[0]["artifact_count"], 0,
                         "会话库自己不知道产物数（由 server 覆盖）")

        # server 侧补上
        counts = self.env.server.session_job_counts([s["id"] for s in sessions])
        for item in sessions:
            item["artifact_count"] = counts.get(item["id"], 0)
        self.assertEqual(sessions[0]["artifact_count"], 1)


# ---------------------------------------------------------------- B. 级联清理

class TestCascadeCleanup(unittest.TestCase):
    """删除会话时级联清理产物引用关系。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="h3sess-cascade-"))
        self.env = _ServerEnv(self.tmp)

    def tearDown(self):
        self.env.close()

    def test_clear_session_job_refs_detaches_jobs(self):
        """清理后任务仍在，但不再归属会话。"""
        j1 = self.env.server.create_job_record("t2i", {"prompt": "a"}, "sess-X")
        j2 = self.env.server.create_job_record("t2i", {"prompt": "b"}, "sess-X")

        cleared = self.env.server.clear_session_job_refs("sess-X")
        self.assertEqual(cleared, 2)

        with self.env.server.db() as conn:
            rows = {r["id"]: r["session_id"] for r in conn.execute(
                "SELECT id, session_id FROM jobs WHERE id IN (?,?)", (j1, j2))}
        # 任务记录还在（生成库里还能看到）
        self.assertIn(j1, rows)
        self.assertIn(j2, rows)
        # 但不再归属任何会话
        self.assertEqual(rows[j1], "")
        self.assertEqual(rows[j2], "")

    def test_cascade_does_not_delete_other_sessions_jobs(self):
        """清理只影响本会话，不碰别的会话的任务。"""
        self.env.server.create_job_record("t2i", {"prompt": "a"}, "sess-X")
        other = self.env.server.create_job_record("t2i", {"prompt": "b"}, "sess-Y")

        self.env.server.clear_session_job_refs("sess-X")
        with self.env.server.db() as conn:
            row = conn.execute("SELECT session_id FROM jobs WHERE id=?",
                               (other,)).fetchone()
        self.assertEqual(row["session_id"], "sess-Y")

    def test_clear_refs_unknown_session_is_noop(self):
        """清一个不存在的会话 -> 0，不报错。"""
        self.assertEqual(self.env.server.clear_session_job_refs("nope"), 0)
        self.assertEqual(self.env.server.clear_session_job_refs(""), 0)

    def test_delete_session_keeps_jobs_and_outputs(self):
        """
        删会话后：任务记录**仍在**，outputs 也在。

        【为什么这是必须的】删掉任务记录会让生成列表里凭空少一条，
        而且产物文件还在（孤儿文件）。会话可丢弃、任务不可。

        【注意】任务必须挂在**这个会话 id** 上 —— 否则级联清理找不到它。
        （第一版测试写成 "sess-Z"，与这里创建的 sid 不一致，
          于是清理的是空会话，任务归属没被清掉 —— 测试自己写错了。）
        """
        sid = self.env.store.create(None)
        self.env.store.add_message(sid, "user", "做图")
        self.env.store.add_message(sid, "assistant", "好了")

        job_id = self.env.server.create_job_record(
            "t2i", {"prompt": "雨夜"}, sid)
        # 给任务挂一个产物输出
        with self.env.server.db() as conn:
            conn.execute("UPDATE jobs SET outputs=?, status=? WHERE id=?",
                         (json.dumps([{"kind": "image", "file": "a.png"}]),
                          "done", job_id))

        # 走真实的 delete 链路
        self.env.server.clear_session_job_refs(sid)
        deleted = self.env.store.delete(sid)
        self.assertTrue(deleted)

        with self.env.server.db() as conn:
            row = conn.execute(
                "SELECT session_id, outputs, status FROM jobs WHERE id=?",
                (job_id,)).fetchone()
        self.assertIsNotNone(row, "任务记录不能被删掉")
        self.assertEqual(row["session_id"], "", "任务不该再归属已删的会话")
        self.assertTrue(row["outputs"], "产物路径必须保留")
        self.assertEqual(row["status"], "done")

    def test_delete_session_removes_messages(self):
        """删会话 -> 消息全部清理。"""
        sid = self.env.store.create(None)
        for role in ("user", "assistant", "tool"):
            self.env.store.add_message(sid, role, "x")
        self.assertEqual(len(self.env.store.raw_history(sid)), 3)
        self.env.store.delete(sid)
        self.assertEqual(self.env.store.raw_history(sid), [])


# ---------------------------------------------------------------- C. 前端契约

class TestFrontendContract(unittest.TestCase):
    """前端静态契约（结构层）。"""

    def _read(self, name):
        return (ROOT / "web" / name).read_text(encoding="utf-8")

    def test_search_input_exists(self):
        """会话列表要有搜索框（用户新增要求）。"""
        html = self._read("index.html")
        self.assertIn('id="agSessionSearch"', html)

    def test_frontend_sends_query_param(self):
        """前端要把搜索词发给服务端（服务端做过滤，不在前端过滤）。"""
        js = self._read("agent_sessions.js")
        self.assertIn('state.search', js)
        self.assertIn('&q=', js)

    def test_search_has_debounce(self):
        """搜索必须有防抖（否则每个按键都发请求）。"""
        js = self._read("agent_sessions.js")
        self.assertIn("setTimeout", js)
        self.assertIn("clearTimeout", js)

    def test_artifact_count_rendered(self):
        """会话条目要显示产物数。"""
        js = self._read("agent_sessions.js")
        self.assertIn("artifact_count", js)
        self.assertIn("产物", js)

    def test_locate_job_button_exists(self):
        """产物卡片要有「在生成库中定位」入口（用户新增要求）。"""
        js = self._read("agent_sessions.js")
        self.assertIn('data-act="locate-job"', js)
        self.assertIn("在生成库中定位", js)
        html = self._read("index.html")
        self.assertIn('id="agArtifactsBody"', html)

    def test_pending_artifacts_poll(self):
        """
        未完成任务要持续轮询状态（用户新增要求）。

        【为什么不能无条件 setInterval】
        面板关着时轮询 = 白白发请求。
        要「面板开着 + 有未完成任务」才轮询。
        """
        js = self._read("agent_sessions.js")
        self.assertIn("ARTIFACT_POLL_MS", js)
        self.assertIn("hasPendingArtifacts", js)
        self.assertIn("startArtifactPolling", js)
        self.assertIn("stopArtifactPolling", js)
        # 关闭面板要停轮询
        self.assertIn("stopArtifactPolling", js)

    def test_pending_marker_class(self):
        """未完成的任务卡片要有可辨识的标记。"""
        js = self._read("agent_sessions.js")
        self.assertIn("is-pending", js)
        css = self._read("style.css")
        self.assertIn(".ag-artifact-item.is-pending", css)

    def test_search_and_locate_styles_exist(self):
        """新增元素要有样式（不然是裸 HTML）。"""
        css = self._read("style.css")
        self.assertIn(".ag-session-search", css)
        self.assertIn(".ag-artifact-locate", css)


if __name__ == "__main__":
    unittest.main()
