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
  agent_messages   消息表（session_id / role / content / tool_name /
                           meta / created_at）

消息只存**摘要**级别的东西：用户输入、模型文本、工具调用名与结果摘要。
完整的原始响应体不落库 —— 那会让库迅速膨胀，而且对恢复对话没有价值。

【meta 列 —— 2026-11-11 会话管理需求】
原先「刷新页面恢复历史」做不了，根因不是没落库，而是**落库的信息不够**：
  * 工具调用只存了 "create_job: ok" 这种字符串 -> 前端恢复不出任务卡；
  * 任务卡里的 job id / 模式 / 缩略图全丢了 -> 恢复后是「一句话」而不是
    「带产物缩略图的对话」；
  * 状态徽章（完成/截断/失败）也丢了 -> 恢复后无法还原语义。
所以加一列 meta（JSON），存**结构化**的最小可渲染信息：
  {"jobs":[{job_id,mode,status,...}], "status":"completed", "attachments":[...]}
只存前端渲染需要的字段，不存完整响应体。

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

#: 单条消息的 meta JSON 最大长度 —— 同样是为了防止库膨胀。
#: meta 只存结构化渲染信息（任务卡 / 状态 / 附件），正常远小于这个上限。
MAX_META_CHARS = 8000

#: 保留的历史轮数（超过的旧消息会被裁掉）
HISTORY_LIMIT = 8


def _parse_meta(raw) -> dict:
    """
    解析 meta 列。坏数据一律降级成空 dict。

    【为什么不能抛异常】
    meta 是 JSON 字符串，但库里可能存在：
      * 老版本写入的空串
      * 手工改库留下的半截 JSON
      * 超长被截断的 JSON（MAX_META_CHARS 截断）
    任何一种都不该让**整个会话历史打不开** —— 用户宁可少看一个附件，
    也不该丢掉整段对话。所以这里只吞不抛。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
                    meta TEXT DEFAULT '',        -- JSON：结构化渲染信息（2026-11-11）
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                    ON agent_messages(session_id, id);
                """
            )
            self._ensure_columns(conn)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """
        增量加列（老库升级）。

        【为什么不能用 CREATE TABLE IF NOT EXISTS 解决】
        表已经存在时，IF NOT EXISTS 会**整句跳过** —— 老库永远拿不到新列。
        所以每次启动都要查一遍实际列名，缺什么补什么。

        【为什么用 PRAGMA table_info 而不是写死版本号】
        版本号迁移一次写错就永久错（改不回去）。查列名是**幂等**的：
        已经有该列就跳过，没有就 ALTER TABLE ADD COLUMN。
        """
        rows = conn.execute("PRAGMA table_info(agent_messages)").fetchall()
        columns = {str(r[1]) for r in rows}
        if "meta" not in columns:
            conn.execute("ALTER TABLE agent_messages ADD COLUMN meta TEXT DEFAULT ''")

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

    def title_of(self, session_id: str) -> str:
        """取会话标题（没有就空串）。"""
        with self.connect() as conn:
            row = conn.execute("SELECT title FROM agent_sessions WHERE id=?",
                               (session_id,)).fetchone()
        if not row:
            return ""
        return (row["title"] or "").strip()

    def rename(self, session_id: str, title: str) -> bool:
        """
        改会话标题。返回是否真的改到了（会话不存在时 False）。

        【为什么要支持改名 —— 2026-11-11】
        标题默认取首条用户消息前 40 字。但用户建会话时可能已经有想法了
        （比如「林黛玉短剧」「雨夜素材测试」），自动标题反而不如手写。
        """
        text = (title or "").strip()[:120]
        if not text:
            return False
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE agent_sessions SET title=?, updated_at=? WHERE id=?",
                (text, time.time(), session_id))
            return cur.rowcount > 0

    def delete(self, session_id: str) -> bool:
        """
        删除会话及其全部消息。

        返回值语义：**会话本身是否存在**（不是「有没有删掉消息」）。

        【为什么要改返回语义 —— 2026-11-11】
        原实现返回 `cur.rowcount > 0`（删掉了几条**消息**）。
        这导致一个反直觉的结果：
          * 删一个**有消息**的会话 -> True（碰巧对）
          * 删一个**空会话**（刚建、没发过消息）-> False
        前端拿这个值判断「删除成功了吗」-> 空会话删不掉但显示「删除失败」。
        会话存在就该删掉（它只是个空壳），所以看会话行数。
        """
        with self.connect() as conn:
            conn.execute("DELETE FROM agent_messages WHERE session_id=?",
                         (session_id,))
            cur = conn.execute("DELETE FROM agent_sessions WHERE id=?",
                                (session_id,))
            return cur.rowcount > 0

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """
        会话清单（最近 N 条，按最近活跃排序）。

        每条返回：
          id / title / created_at / updated_at / message_count / preview

        【title 与 preview 的区别 —— 2026-11-11】
        title 是**自动生成**的会话名（首条用户消息前 40 字），用于侧栏列表；
        preview 是首条用户消息的**完整**片段，用于列表里的两行摘要。

        为什么两个都要：
          * 只给 title 的话，列表里只能显示一行 40 字，短的会话（"ping"）
            看不出是哪个；
          * 只给 preview 的话，等于把 title 的活重做一遍，前端得自己截断
            —— 又一份必然漂移的格式规则。
        在库里一次算好，前端只管显示。

        【空标题兜底】
        刚建的会话还没发过消息 -> title 是空串。这时列表里显示成
        「新会话」，而不是留一个空白项让用户以为页面没加载出来。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM agent_messages m "
                "                WHERE m.session_id=s.id) AS message_count "
                "FROM agent_sessions s ORDER BY s.updated_at DESC LIMIT ?",
                (limit,)).fetchall()
            # 首条用户消息（preview）。单独查是因为 SQLite 不支持
            #  correlated subquery 取「第一条」的直接写法（LIMIT 不进子查询）。
            previews: dict[str, str] = {}
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                first_rows = conn.execute(
                    f"SELECT session_id, content FROM agent_messages "
                    f"WHERE session_id IN ({placeholders}) AND role='user' "
                    f"ORDER BY id ASC", ids).fetchall()
                for fr in first_rows:
                    sid = str(fr["session_id"])
                    if sid not in previews:
                        previews[sid] = (fr["content"] or "").strip()
        result = []
        for r in rows:
            title = (r["title"] or "").strip()
            preview = previews.get(str(r["id"]), "")
            result.append({
                "id": r["id"],
                "title": title or "新会话",
                "preview": preview[:120],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "message_count": r["message_count"],
            })
        return result

    # ------------------------------------------------------------------ 消息

    def add_message(self, session_id: str, role: str, content: str,
                    tool_name: str = "", meta: dict | None = None) -> None:
        """
        追加一条消息。

        meta:
          结构化渲染信息（dict）。会被序列化成 JSON 存进 meta 列。
          只存前端渲染需要的最小信息（任务卡 / 状态徽章 / 附件），
          不存完整响应体 —— 那会让库迅速膨胀。

        【为什么要单独加 meta 列，而不是把 JSON 拼进 content】
        content 是给模型看的**对话文本**（history() 会把它喂回 LLM）。
        把结构化数据拼进去会污染模型上下文，而且恢复时要再反向解析
        —— 又一份必然漂移的格式。分开存，两个用途互不干扰。
        """
        text = (content or "")[:MAX_MESSAGE_CHARS]
        meta_json = ""
        if meta:
            try:
                meta_json = json.dumps(meta, ensure_ascii=False)[:MAX_META_CHARS]
            except (TypeError, ValueError):
                meta_json = ""
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_messages (session_id, role, content, tool_name, meta, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_id, role, text, tool_name or "", meta_json, now))
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
        """
        完整消息列表（给前端展示对话气泡用，不做轮次裁剪）。

        返回的每条带 meta（dict）—— 前端据此恢复任务卡 / 状态徽章 / 附件。
        meta 解析失败时返回空 dict（**不能**因为一条坏数据让整个历史打不开）。
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT role, content, tool_name, meta, created_at FROM agent_messages "
                "WHERE session_id=? ORDER BY id ASC LIMIT ?",
                (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"] or "",
                 "tool_name": r["tool_name"] or "",
                 "meta": _parse_meta(r["meta"]),
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
