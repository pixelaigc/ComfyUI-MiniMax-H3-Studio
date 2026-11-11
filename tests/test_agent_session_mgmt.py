# -*- coding: utf-8 -*-
"""
会话持久化与会话管理 —— 后端回归测试。

覆盖：
  A. 会话库：meta 列的增量迁移 / 存取 / 坏数据降级
  B. 会话 CRUD：新建 / 列表 / 详情 / 删除 / 标题
  C. 会话 <-> 任务关联：jobs.session_id 写入与查询
  D. 消息 meta 落库：结构化信息能被恢复
  E. 既有两项约束不回退：真编造 job id 仍拦截 / 截断不误标失败
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------- A. meta 列

def test_agent_messages_gets_meta_column_on_fresh_db():
    """新建库时 agent_messages 必须带 meta 列。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        cols = {r[1] for r in store.connect().execute(
            "PRAGMA table_info(agent_messages)").fetchall()}
        assert "meta" in cols


def test_meta_column_migrated_on_legacy_db():
    """
    老库（没有 meta 列）必须能自动升级。

    【为什么这是必须测的】生产库 agent_sessions.db 是**已有数据**的旧结构。
    CREATE TABLE IF NOT EXISTS 对已存在的表是整句跳过 —— 老库永远拿不到
    新列。不测这条，上线后老会话全部读不出 meta。
    """
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "legacy.db"
        # 手工造一个「旧结构」库（无 meta 列）
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE agent_sessions (
                id TEXT PRIMARY KEY, created_at REAL,
                updated_at REAL, title TEXT DEFAULT ''
            );
            CREATE TABLE agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT DEFAULT '', tool_name TEXT DEFAULT '',
                created_at REAL
            );
            """
        )
        conn.commit()
        conn.close()

        store = agent_sessions.SessionStore(path)
        cols = {r[1] for r in store.connect().execute(
            "PRAGMA table_info(agent_messages)").fetchall()}
        assert "meta" in cols, "老库必须自动补 meta 列"

        # 迁移后还能正常写入/读取
        sid = store.create(None)
        store.add_message(sid, "assistant", "hello", meta={"status": "completed"})
        hist = store.raw_history(sid)
        assert hist[0]["meta"]["status"] == "completed"


def test_meta_bad_json_degrades_to_empty_dict():
    """
    坏 meta 不能让整个会话历史打不开。

    【为什么】meta 可能被手工改库截断成半截 JSON。用户宁可少看一个附件，
    也不该丢掉整段对话。
    """
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        store = agent_sessions.SessionStore(path)
        sid = store.create(None)
        store.add_message(sid, "assistant", "ok")
        # 直接塞半截 JSON
        with store.connect() as conn:
            conn.execute("UPDATE agent_messages SET meta=? WHERE session_id=?",
                         ('{"jobs": [', sid))
        hist = store.raw_history(sid)
        assert len(hist) == 1
        assert hist[0]["meta"] == {}
        assert hist[0]["content"] == "ok"


def test_meta_oversize_is_truncated_not_crashed():
    """超长 meta 截断但不抛异常。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        sid = store.create(None)
        huge = {"blob": "x" * (agent_sessions.MAX_META_CHARS * 3)}
        store.add_message(sid, "assistant", "ok", meta=huge)
        hist = store.raw_history(sid)
        assert len(hist) == 1


# ---------------------------------------------------------------- B. 会话 CRUD

def test_create_session_persists_and_lists():
    """新建的会话必须真的落库（不是只在内存里）。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        store = agent_sessions.SessionStore(path)
        sid = store.create(None)
        assert store.exists(sid)

        # 换个 store 实例（模拟进程重启）仍能读到
        store2 = agent_sessions.SessionStore(path)
        assert store2.exists(sid)


def test_list_sessions_includes_title_preview_and_count():
    """列表要带标题、预览、消息数 —— 侧栏直接用。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        sid = store.create(None)
        store.add_message(sid, "user", "帮我做一段雨夜的视频")
        store.add_message(sid, "assistant", "好的")

        sessions = store.list_sessions()
        assert len(sessions) == 1
        item = sessions[0]
        assert item["id"] == sid
        assert item["message_count"] == 2
        assert item["preview"].startswith("帮我做一段雨夜的视频")
        assert item["title"]


def test_empty_session_title_falls_back_to_label():
    """
    刚建、没发过消息的会话，标题不能是空串。

    【为什么】空标题在列表里显示成空白行，用户会以为页面没加载出来。
    """
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        store.create(None)
        items = store.list_sessions()
        assert items[0]["title"] == "新会话"


def test_delete_empty_session_returns_true():
    """
    删除**空会话**（刚建没发消息）必须返回 True。

    【为什么这是本轮修的 bug】原实现返回「删掉了几条消息」> 0。
    空会话消息数是 0 -> 返回 False -> 前端显示「删除失败」，
    但会话其实已经被删了。
    """
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        sid = store.create(None)
        assert store.delete(sid) is True
        assert not store.exists(sid)


def test_delete_session_removes_messages_too():
    """删会话要连消息一起删（不留孤儿行）。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        sid = store.create(None)
        store.add_message(sid, "user", "x")
        store.add_message(sid, "assistant", "y")
        assert store.delete(sid) is True
        with store.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM agent_messages WHERE session_id=?",
                (sid,)).fetchone()[0]
        assert n == 0


def test_rename_session():
    """改名接口（用户可以给会话起自己的名字）。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        store = agent_sessions.SessionStore(Path(tmp) / "s.db")
        sid = store.create(None)
        assert store.rename(sid, "林黛玉短剧") is True
        assert store.title_of(sid) == "林黛玉短剧"
        assert store.rename("nonexistent", "x") is False


# ---------------------------------------------------------------- C. 会话 <-> 任务

class _ServerEnv:
    """隔离的 server 模块环境（独立 jobs.db）。

    server 模块在 **import 时**就会建 data 目录与 DB_PATH，
    所以必须先设好 H3STUDIO_DATA_DIR 再 import，否则会污染生产 data/。
    """

    def __init__(self, tmp: Path):
        self.tmp = Path(tmp)
        self._old = os.environ.get("H3STUDIO_DATA_DIR")
        os.environ["H3STUDIO_DATA_DIR"] = str(self.tmp / "studio")
        sys.modules.pop("server", None)
        self.server = importlib.import_module("server")
        self.server.init_db()

    def close(self):
        if self._old is None:
            os.environ.pop("H3STUDIO_DATA_DIR", None)
        else:
            os.environ["H3STUDIO_DATA_DIR"] = self._old
        sys.modules.pop("server", None)


def test_jobs_table_gets_session_id_column():
    """jobs 表必须有 session_id 列（增量迁移）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            with env.server.db() as conn:
                cols = {r[1] for r in conn.execute(
                    "PRAGMA table_info(jobs)").fetchall()}
            assert "session_id" in cols
        finally:
            env.close()


def test_create_job_record_stores_session_id():
    """Agent 建任务时必须把会话 id 写进 jobs.session_id。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            job_id = env.server.create_job_record(
                "t2i", {"prompt": "x"}, "sess-abc")
            with env.server.db() as conn:
                row = conn.execute("SELECT session_id FROM jobs WHERE id=?",
                                   (job_id,)).fetchone()
            assert row["session_id"] == "sess-abc"
        finally:
            env.close()


def test_create_job_record_without_session_is_blank():
    """面板侧建任务（无会话）-> session_id 为空串，不报错。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            job_id = env.server.create_job_record("t2i", {"prompt": "x"})
            with env.server.db() as conn:
                row = conn.execute("SELECT session_id FROM jobs WHERE id=?",
                                   (job_id,)).fetchone()
            assert row["session_id"] == ""
        finally:
            env.close()


def test_list_jobs_for_session_isolates_sessions():
    """
    会话产物必须**按会话隔离** —— 不能串味。

    【为什么这是必须测的】如果按时间窗猜，用户在会话 A 里会看到会话 B 的任务。
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            j1 = env.server.create_job_record("t2i", {"prompt": "a"}, "sess-A")
            j2 = env.server.create_job_record("t2i", {"prompt": "b"}, "sess-A")
            j3 = env.server.create_job_record("t2v", {"prompt": "c"}, "sess-B")

            jobs_a = env.server.list_jobs_for_session("sess-A")
            ids_a = {j["id"] for j in jobs_a}
            assert ids_a == {j1, j2}
            assert j3 not in ids_a

            jobs_b = env.server.list_jobs_for_session("sess-B")
            assert {j["id"] for j in jobs_b} == {j3}
        finally:
            env.close()


def test_list_jobs_for_session_empty_and_blank():
    """空会话 / 空 session_id -> 返回空列表（不报错）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            assert env.server.list_jobs_for_session("") == []
            assert env.server.list_jobs_for_session("nope") == []
        finally:
            env.close()


def test_list_jobs_for_session_carries_render_fields():
    """产物条目要带前端渲染需要的字段（mode/status/params/outputs）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            job_id = env.server.create_job_record(
                "t2i", {"prompt": "雨夜", "aspect": "16:9"}, "sess-X")
            jobs = env.server.list_jobs_for_session("sess-X")
            assert len(jobs) == 1
            job = jobs[0]
            assert job["id"] == job_id
            assert job["mode"] == "t2i"
            assert job["status"] == "queued"
            assert job["params"]["prompt"] == "雨夜"
            assert job["session_id"] == "sess-X"
        finally:
            env.close()


# ---------------------------------------------------------------- D. meta 落库

def test_agent_message_meta_extracts_jobs_status_attachments():
    """从 Agent 结果里抽出前端恢复需要的最小结构。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            result = {
                "status": "completed",
                "answer": "搞定了",
                "jobs": [{
                    "job_id": "abc123", "mode": "t2i", "label": "文生图",
                    "status": "done", "prompt": "雨夜",
                    "outputs": [{"kind": "image", "file": "a.png"}],
                }],
                "attachments": [{"asset_id": "asset-1", "name": "a.png",
                                 "preview_url": "/files/a.png"}],
            }
            meta = env.server._agent_message_meta(result)
            assert meta["status"] == "completed"
            assert meta["jobs"][0]["job_id"] == "abc123"
            assert meta["jobs"][0]["outputs"]
            assert meta["attachments"][0]["asset_id"] == "asset-1"
        finally:
            env.close()


def test_agent_message_meta_drops_empty_fields():
    """空的 status/jobs/attachments 不该进 meta（保持库小）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            meta = env.server._agent_message_meta(
                {"status": "", "jobs": [], "answer": "x"})
            assert "status" not in meta
            assert "jobs" not in meta
            assert "attachments" not in meta
        finally:
            env.close()


def test_agent_user_meta_from_string_ids():
    """附件传 asset_id 字符串列表时也能落 meta。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            meta = env.server._agent_user_meta(["asset-1", "asset-2"])
            assert len(meta["attachments"]) == 2
            assert meta["attachments"][0]["asset_id"] == "asset-1"
        finally:
            env.close()


def test_agent_user_meta_empty_is_empty_dict():
    """没有附件 -> 空 meta（不留空数组噪音）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            assert env.server._agent_user_meta([]) == {}
            assert env.server._agent_user_meta(None) == {}
        finally:
            env.close()


def test_roundtrip_meta_through_store():
    """端到端：result -> meta -> 落库 -> 读回，字段不丢。"""
    from h3studio import agent_sessions

    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            store = agent_sessions.SessionStore(Path(tmp) / "s.db")
            sid = store.create(None)
            result = {
                "status": "truncated",
                "notice": "已达到步骤上限",
                "jobs": [{"job_id": "j1", "mode": "t2v", "status": "queued"}],
            }
            store.add_message(sid, "assistant", "部分结果",
                              meta=env.server._agent_message_meta(result))
            hist = store.raw_history(sid)
            meta = hist[0]["meta"]
            assert meta["status"] == "truncated"
            assert meta["notice"] == "已达到步骤上限"
            assert meta["jobs"][0]["job_id"] == "j1"
        finally:
            env.close()


# ---------------------------------------------------------------- E. 既有两项约束

def test_fabricated_job_id_still_blocked_after_session_changes():
    """
    既有约束 1：真编造 job id 仍必须被拦截。

    【为什么在这轮测】会话管理加了 ctx.session_id / create_job_record 新参数，
    这两处都在编造判定链路上 —— 必须确认没把拦截能力改弱。
    """
    from h3studio import agent as agent_mod

    # 编造的 12 位十六进制 id，不在任何真实列表里 -> 必须判为编造。
    # 文案要含「已下发」这类**完成时**声明（_FABRICATED_JOB_MARKERS），
    # 否则第一层判据就不成立 —— 那是「压根没声称建任务」，不是编造。
    assert agent_mod._looks_like_fabricated_job_claim(
        "已下发任务（job deadbeefcafe）", []) is True
    # 真实存在的 id -> 不能判为编造
    assert agent_mod._looks_like_fabricated_job_claim(
        "已下发任务（job ac8778a6ea84）", ["ac8778a6ea84"]) is False
    # 真实 id 的抄写变体（实测差 3 位）-> 仍不能判为编造
    assert agent_mod._looks_like_fabricated_job_claim(
        "已下发任务（job ac8778a6ea84）", ["ac8778a06a04"]) is False
    # 格式完全不同的伪造（带前缀）-> 必须仍被拦
    assert agent_mod._looks_like_fabricated_job_claim(
        "已下发任务（job t2v_019d874a）", ["ac8778a06a04"]) is True


def test_truncated_status_not_failed_after_session_changes():
    """
    既有约束 2：步骤上限截断仍是中性「已截断」，不是红色失败。

    截断的 reason 走 notice（前端黄色），error 字段保留但前端按 status 分流。
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = _ServerEnv(tmp)
        try:
            result = {"status": "truncated", "error": "已达到步骤上限（8 步）",
                      "notice": "已达到步骤上限（8 步），返回已完成的部分结果"}
            meta = env.server._agent_message_meta(result)
            assert meta["status"] == "truncated"
            assert meta["notice"]
            # 截断的 meta 里**不能**有 failed 标记
            assert "failed" not in json.dumps(meta, ensure_ascii=False)
        finally:
            env.close()
