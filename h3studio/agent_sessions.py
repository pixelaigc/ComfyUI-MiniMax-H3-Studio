# -*- coding: utf-8 -*-
"""
内置 Agent 的会话存储。

为什么单独一个库（不放在 jobs.db）：
  jobs.db 是任务库，里面有 jobs / job_batches / model_providers / model_provider_prefs。
  Agent 会话是**另一类数据**（对话历史），而且并发写入模式完全不同：
  jobs 表是 worker 独占写，Agent 会话是多个 HTTP 请求并发写。
  混在一张库里会带来两个问题：
    1. PRAGMA foreign_keys 打开后，老数据里的悬空引用会让建表失败
       （这是素材库分库时踩过的坑，见 h3studio/assets.py 的说明）；
    2. 迁移时互相牵制 —— 改 Agent 不该动到任务库的结构。

表结构：
  agent_sessions   会话主表（id / created_at / updated_at / title）
  agent_messages   消息表（session_id / role / content / tool_name / created_at）

消息只存**摘要**级别的东西：用户输入、模型文本、工具调用名与结果摘要。
完整的原始响应体不落库 —— 那会让库迅速膨胀，而且对恢复对话没有价值。

会话数据是**可丢弃**的：删掉只是丢历史，不影响任何已生成的任务。
所以没有做引用计数保护（不像素材库那样有 409 保护）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

#: 单条消息的最大存储长度（超出截断）—— 防止模型返回超长文本把库撑爆
MAX_MESSAGE_CHARS = 4000

#: 保留的历史轮数（超过的旧消息会被裁掉）
HISTORY_LIMIT = 8


def default_db_path() -> Path:
    """Agent 会话库路径。可用 AGENT_SESSION_DB 环境变量覆盖（测试用）。"""
    override = os.environ.get("AGENT_SESSION_DB")
    if override:
        return Path(override)
    data_dir = os.environ.get("H3STUDIO_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "agent_sessions.db"
    return Path(__file__).resolve().parent.parent / "data" / "agent_sessions.db"


class SessionStore:
    """会话存储。连接按需开关（sqlite3 连接不能跨线程共享）。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else default_db_path()
        self._ensure_schema()

    # ------------------------------------------------------------------ 连接

    def connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY,
                    created_at REAL,
                    updated_at REAL,
                    title TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,          -- user | assistant | tool
                    content TEXT DEFAULT '',
                    tool_name TEXT DEFAULT '',
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                    ON agent_messages(session_id, id);
                """
            )

    # ------------------------------------------------------------------ 会话

    def create(self, session_id: str | None = None) -> str:
        sid = (session_id or "").strip() or uuid.uuid4().hex[:16]
        now = time.time()
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM agent_sessions WHERE id=?",
                               (sid,)).fetchone()
            if row:
                return sid
            conn.execute(
                "INSERT INTO agent_sessions (id, created_at, updated_at, title) "
                "VALUES (?,?,?,'')",
                (sid, now, now))
        return sid

    def exists(self, session_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM agent_sessions WHERE id=?",
                               (session_id,)).fetchone()
        return bool(row)

    def delete(self, session_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM agent_messages WHERE session_id=?",
                               (session_id,))
            conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            return cur.rowcount > 0

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM agent_messages m "
                "                WHERE m.session_id=s.id) AS message_count "
                "FROM agent_sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"id": r["id"], "title": r["title"] or "",
                 "created_at": r["created_at"], "updated_at": r["updated_at"],
                 "message_count": r["message_count"]} for r in rows]

    # ------------------------------------------------------------------ 消息

    def add_message(self, session_id: str, role: str, content: str,
                    tool_name: str = "") -> None:
        text = (content or "")[:MAX_MESSAGE_CHARS]
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages (session_id, role, content, tool_name, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, role, text, tool_name or "", now))
            conn.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?",
                         (now, session_id))
            # 标题：首条用户消息的前 40 字
            if role == "user":
                conn.execute(
                    "UPDATE agent_sessions SET title=? "
                    "WHERE id=? AND (title='' OR title IS NULL)",
                    (text[:40], session_id))

    def history(self, session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        """
        取最近 limit 轮对话（按 role 分组成 OpenAI 风格的 messages）。

        裁剪规则：
          1. 只保留最近 limit 轮「用户轮」—— 一轮 = 一条 user + 其后的 assistant/tool；
          2. 工具消息（role=tool）保留，但内容在存入时已截断（MAX_MESSAGE_CHARS）；
          3. 若裁剪后第一条不是 user，就从第一条 user 开始（避免半截的 tool 消息）。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_name FROM agent_messages "
                "WHERE session_id=? ORDER BY id ASC",
                (session_id,)).fetchall()
        if not rows:
            return []

        items = [{"role": r["role"], "content": r["content"] or "",
                  "tool_name": r["tool_name"] or ""} for r in rows]

        # 找到最后 limit 个 user 消息的位置
        user_positions = [i for i, m in enumerate(items) if m["role"] == "user"]
        if not user_positions:
            return []
        keep_from = user_positions[-limit] if len(user_positions) > limit else 0
        trimmed = items[keep_from:]

        messages = []
        for item in trimmed:
            if item["role"] == "tool":
                messages.append({"role": "tool",
                                 "content": item["content"],
                                 "_tool_name": item["tool_name"]})
            elif item["role"] == "assistant":
                messages.append({"role": "assistant", "content": item["content"]})
            else:
                messages.append({"role": "user", "content": item["content"]})
        return messages

    def raw_history(self, session_id: str, limit: int = 200) -> list[dict]:
        """完整消息列表（给前端展示对话气泡用，不做轮次裁剪）。"""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_name, created_at FROM agent_messages "
                "WHERE session_id=? ORDER BY id ASC LIMIT ?",
                (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"] or "",
                 "tool_name": r["tool_name"] or "",
                 "created_at": r["created_at"]} for r in rows]

    def clear(self, session_id: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM agent_messages WHERE session_id=?",
                               (session_id,))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ 检索

    def search(self, query: str, limit: int = 20,
               session_id: str = "", roles: tuple = ()) -> list[dict]:
        """
        跨会话检索历史消息。

        用于 Agent 的 search_conversations 工具 —— 用户问
        「我上次让它做那个视频是什么时候用的什么比例？」时，
        Agent 需要能真的**找回**那句话，而不是靠模型瞎编。

        实现要点：
          * 用 LIKE 做**子串**匹配（不是 FTS）—— SQLite 默认不带 FTS5 的
            Python 构建也有 FTS5，但 LIKE 足够：会话消息量是几百条量级，
            全表扫的代价可以忽略，换来的是零依赖 + 行为可预测。
          * 结果按时间倒序（最近的优先）—— 用户问的通常是最近那次。
          * 命中片段做高亮裁剪 —— 只回传命中位置附近的上下文，
            否则一条长消息会把工具结果撑爆（模型看到的是被截断的 JSON）。
        """
        needle = (query or "").strip()
        if not needle:
            return []
        limit = max(1, min(int(limit or 20), 100))

        sql = ("SELECT m.session_id, m.role, m.content, m.tool_name, "
               "m.created_at, s.title FROM agent_messages m "
               "LEFT JOIN agent_sessions s ON s.id = m.session_id "
               "WHERE m.content LIKE ?")
        args: list = [f"%{needle}%"]
        if session_id:
            sql += " AND m.session_id=?"
            args.append(session_id)
        if roles:
            placeholders = ",".join("?" * len(roles))
            sql += f" AND m.role IN ({placeholders})"
            args.extend(list(roles))
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        args.append(limit)

        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()

        results = []
        for row in rows:
            content = row["content"] or ""
            position = content.lower().find(needle.lower())
            if position >= 0:
                start = max(0, position - 60)
                snippet = content[start:position + len(needle) + 120]
                if start > 0:
                    snippet = "…" + snippet
                if position + len(needle) + 120 < len(content):
                    snippet = snippet + "…"
            else:
                snippet = content[:180]
            results.append({
                "session_id": row["session_id"],
                "session_title": row["title"] or "",
                "role": row["role"],
                "tool_name": row["tool_name"] or "",
                "snippet": snippet,
                "created_at": row["created_at"],
            })
        return results

    def stats(self) -> dict:
        """会话库统计（给前端/调试看）。"""
        with self.connect() as conn:
            sessions = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_sessions").fetchone()["c"]
            messages = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_messages").fetchone()["c"]
        return {"sessions": sessions, "messages": messages,
                "max_message_chars": MAX_MESSAGE_CHARS,
                "history_limit": HISTORY_LIMIT}


_STORE: SessionStore | None = None


def get_store() -> SessionStore:
    """全局单例。首次调用时建表。"""
    global _STORE
    if _STORE is None:
        _STORE = SessionStore()
    return _STORE


def reset_store(path: Path | None = None) -> SessionStore:
    """替换全局 store（测试用）。"""
    global _STORE
    _STORE = SessionStore(path)
    return _STORE
