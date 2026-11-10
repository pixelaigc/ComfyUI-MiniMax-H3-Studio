# -*- coding: utf-8 -*-
"""
内置 Agent 的**长期记忆**层（跨会话）。

【为什么单独一个库（不放在 agent_sessions.db，也不放在 jobs.db）】
  * agent_sessions.db  —— 对话历史，**会话内**语义（上一轮说了什么）
  * jobs.db           —— 生成任务
  * agent_memory.db   —— 用户**偏好**（跨会话语义）

三者的写入模式与生命周期都不同，混在一起会让「改 Agent 记忆」这种小改动
牵动到任务库结构。独立库还有一个好处：**丢记忆不影响任何已生成的任务**。

【记忆模型】
刻意做成「**键值 + 作用域**」的极简模型，而不是向量库 / 自由文本：
  * key       —— 稳定的语义标识（如 "preferred_aspect"）
  * scope     —— "global"（跨会话默认）| "session:<id>"（仅该会话）
  * value     —— JSON 编码的值（字符串 / 数字 / 列表 / 字典）
  * source    —— 来源标记（如 "tool:recall_memory" / "auto:create_job"）

为什么不用向量库：本地单机场景，记忆条目是**几十条量级**的显式偏好
（喜欢什么比例、默认用什么模型、命名习惯），关键词精确匹配 + 作用域过滤
就足够，语义相似度反而带来不可控的召回。

【上限（硬约束，不是建议）】
记忆库是个**无主的长尾存储** —— 没人会来清理它。所以每一条都必须在写入时
被限制住，否则一次异常循环就能把它撑爆：
  * 条目数上限   MAX_ENTRIES（默认 500）
  * 单条值长度   MAX_VALUE_CHARS（默认 2000）
  * 键长度       MAX_KEY_CHARS（默认 120）
写入超限时**淘汰最旧的全局条目**（LRU by updated_at），而不是直接报错 ——
用户不该因为「记忆满了」而无法继续使用。

【安全】
记忆值来自用户输入/模型输出，未来会被**回灌进 system 提示词**。
所以写入前统一做：长度截断 + 控制字符剥离 + 提示注入标记检测。
注入命中的条目**不入库**（宁可少一条记忆，也不要让一条被污染的偏好
在未来每一轮都污染模型的行为）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

# ------------------------------------------------------------------ 上限

#: 记忆条目总数上限。超出时按 updated_at 淘汰最旧的条目。
MAX_ENTRIES = 500
#: 单条记忆值的最大字符数（超出截断）。
MAX_VALUE_CHARS = 2000
#: 键的最大字符数。
MAX_KEY_CHARS = 120
#: 单次读取返回的条目数上限。
MAX_READ_LIMIT = 100

#: 作用域前缀 —— 其它前缀一律当非法值处理。
SCOPE_GLOBAL = "global"
SCOPE_SESSION_PREFIX = "session:"


def default_db_path() -> Path:
    """记忆库路径。可用 AGENT_MEMORY_DB 环境变量覆盖（测试用）。"""
    override = os.environ.get("AGENT_MEMORY_DB")
    if override:
        return Path(override)
    data_dir = os.environ.get("H3STUDIO_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "agent_memory.db"
    return Path(__file__).resolve().parent.parent / "data" / "agent_memory.db"


# ------------------------------------------------------------------ 注入检测

#: 提示注入特征。命中即拒绝写入（宁可少一条记忆，也不要污染未来每一轮）。
#
# 判据要**窄**：宁可漏判（那只是一条普通偏好），也不误判（误判会让用户
# 「设置偏好」被莫名拒绝，非常恼人）。这里只匹配明确试图改写模型行为的句式。
_INJECTION_PATTERNS = (
    # 「忽略(之前|以上|前面|先前)?(所有)?(的)?(指令|命令|提示|规则|设定)」
    # —— 中文里「的所有指令」「所有指令」两种语序都常见，所以量词段用
    #    可选修饰的宽松写法，而不是固定顺序（固定顺序会漏判「忽略之前的所有指令」）。
    re.compile(r"忽略[^。；\n]{0,12}?(?:指令|命令|提示|规则|设定|系统提示)", re.I),
    re.compile(r"(?:不要|别|无需|不用)[^。；\n]{0,8}?(?:遵守|执行)[^。；\n]{0,8}?(?:规则|指令|限制|策略)", re.I),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|directives?)", re.I),
    re.compile(r"(?:disregard|override|bypass|forget)\s+(?:your\s+|all\s+|any\s+)?(?:instructions?|rules?|guardrails?|safety|system\s+prompt)", re.I),
    re.compile(r"(?:你现在是|from now on|现在开始|从现在起)[^\n]{0,24}(?:扮演|假装|成为|无视|绕过|不受)", re.I),
    re.compile(r"(?:执行|运行|调用)[^\n]{0,12}(?:rm\s+-rf|curl\s+[^\n]*\|[^\n]*sh|删除所有文件|格式化磁盘)", re.I),
    re.compile(r"(?:reveal|leak|print|输出|泄露|复述)[^\n]{0,20}(?:system\s+prompt|系统提示词|你的指令|初始指令)", re.I),
    re.compile(r"</?\s*(?:system|assistant|developer)\s*>", re.I),
    re.compile(r"###\s*(?:system|instruction|指令|系统)\s*(?:prompt)?\s*:", re.I),
)


def detect_injection(text: str) -> dict:
    """
    检测一段文本里是否有提示注入特征。

    返回 {"injection": bool, "matches": [...], "sanitized": str}
    sanitized 是**清理后**的文本（控制字符剥离 + 超长截断），
    即使没命中注入也可以安全入库。
    """
    raw = text or ""
    matches = []
    for pattern in _INJECTION_PATTERNS:
        found = pattern.search(raw)
        if found:
            matches.append(found.group(0)[:80])
    cleaned = sanitize_value(raw)
    return {
        "injection": bool(matches),
        "matches": matches[:5],
        "sanitized": cleaned,
    }


def sanitize_value(text: str) -> str:
    """
    把值清理成可安全存储/回灌的文本。

    做三件事：
      1. 去掉控制字符（除了换行/制表符）—— 防日志注入与终端转义
      2. 去掉零宽字符 —— 用来藏「忽略之前指令」的注入特征
      3. 截断到 MAX_VALUE_CHARS
    """
    if not text:
        return ""
    # 控制字符（保留 \n \t），零宽字符，全角/半角空白规整
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    cleaned = re.sub(r"[‌‍⁠﻿]", "", cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > MAX_VALUE_CHARS:
        cleaned = cleaned[:MAX_VALUE_CHARS] + "…（已截断）"
    return cleaned


def normalize_scope(scope: str | None) -> str:
    """规整作用域。空值 = global；session:<id> 形式原样保留。"""
    text = (scope or "").strip()
    if not text:
        return SCOPE_GLOBAL
    if text.startswith(SCOPE_SESSION_PREFIX):
        sid = text[len(SCOPE_SESSION_PREFIX):].strip()
        if not sid:
            return SCOPE_GLOBAL
        # 会话 id 只允许安全字符
        sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)[:64]
        return SCOPE_SESSION_PREFIX + sid if sid else SCOPE_GLOBAL
    if text == SCOPE_GLOBAL:
        return SCOPE_GLOBAL
    # 未知作用域 —— 只允许字母数字下划线连字符
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", text)[:64]
    return cleaned or SCOPE_GLOBAL


def _encode(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _decode(raw: str):
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ------------------------------------------------------------------ 存储


class MemoryStore:
    """长期记忆存储。连接按需开关（sqlite3 连接不能跨线程共享）。"""

    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else default_db_path()
        self._ensure_schema()

    # ---------------------------------------------------------------- 连接

    def connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'global',
                    value TEXT NOT NULL,
                    source TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    UNIQUE(key, scope)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memory_scope
                    ON agent_memory(scope, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_agent_memory_key
                    ON agent_memory(key);
                """
            )

    # ---------------------------------------------------------------- 写

    def remember(self, key: str, value, scope: str | None = None,
                 source: str = "") -> dict:
        """
        写入一条记忆。返回 {"key","scope","value","created","evicted"}。

        重复写同一个 (key, scope) 是**覆盖**（不是追加）——
        偏好类记忆的语义就是「当前值」。

        写入前会做注入检测：命中则**拒绝写入**并返回 error 字段。
        """
        clean_key = re.sub(r"[\s\x00-\x1f]+", "_", (key or "").strip())[:MAX_KEY_CHARS]
        if not clean_key:
            return {"error": "记忆键不能为空", "code": "bad_request"}

        scope = normalize_scope(scope)
        payload = value if isinstance(value, (dict, list, int, float, bool)) else str(value)

        # 注入检测：把值转成字符串后检测（dict/list 转 JSON 再检测）
        probe = payload if isinstance(payload, str) else _encode(payload)
        check = detect_injection(probe)
        if check["injection"]:
            return {
                "error": ("该内容包含试图改写模型行为的指令（提示注入），"
                          "已拒绝写入记忆。"),
                "code": "prompt_injection_detected",
                "matches": check["matches"],
            }

        # 清理：截断 + 控制字符剥离。dict/list 走 JSON 长度控制。
        if isinstance(payload, str):
            stored = check["sanitized"]
        else:
            stored = _encode(payload)
            if len(stored) > MAX_VALUE_CHARS:
                stored = stored[:MAX_VALUE_CHARS] + "…（已截断）"

        now = time.time()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, created_at FROM agent_memory WHERE key=? AND scope=?",
                (clean_key, scope)).fetchone()
            if row:
                conn.execute(
                    "UPDATE agent_memory SET value=?, source=?, updated_at=? "
                    "WHERE key=? AND scope=?",
                    (stored, (source or "")[:120], now, clean_key, scope))
                created = False
            else:
                conn.execute(
                    "INSERT INTO agent_memory (key, scope, value, source, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (clean_key, scope, stored, (source or "")[:120], now, now))
                created = True
            evicted = self._enforce_limit(conn)
        return {"key": clean_key, "scope": scope,
                "value": _decode(stored), "created": created,
                "evicted": evicted}

    def _enforce_limit(self, conn) -> int:
        """条目数超限时淘汰最旧的。返回淘汰条数。"""
        count = conn.execute("SELECT COUNT(*) AS c FROM agent_memory").fetchone()["c"]
        if count <= MAX_ENTRIES:
            return 0
        excess = count - MAX_ENTRIES
        rows = conn.execute(
            "SELECT id FROM agent_memory ORDER BY updated_at ASC LIMIT ?",
            (excess,)).fetchall()
        for row in rows:
            conn.execute("DELETE FROM agent_memory WHERE id=?", (row["id"],))
        return len(rows)

    def forget(self, key: str, scope: str | None = None) -> bool:
        """删除一条记忆。返回是否真的删掉了。"""
        clean_key = (key or "").strip()[:MAX_KEY_CHARS]
        if not clean_key:
            return False
        scope = normalize_scope(scope)
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM agent_memory WHERE key=? AND scope=?",
                (clean_key, scope))
            return cur.rowcount > 0

    def clear(self, scope: str | None = None) -> int:
        """清空某个作用域的全部记忆（scope=None 清空全部）。返回删除条数。"""
        with self.connect() as conn:
            if scope is None:
                cur = conn.execute("DELETE FROM agent_memory")
            else:
                cur = conn.execute("DELETE FROM agent_memory WHERE scope=?",
                                   (normalize_scope(scope),))
            return cur.rowcount

    # ---------------------------------------------------------------- 读

    def recall(self, query: str = "", scope: str | None = None,
               limit: int = 20) -> list[dict]:
        """
        读记忆。

        query 非空时按「键包含关键词」过滤（大小写不敏感）；
        query 为空时按 scope 过滤返回全部。
        """
        limit = max(1, min(int(limit or 20), MAX_READ_LIMIT))
        sql = ("SELECT key, scope, value, source, created_at, updated_at "
               "FROM agent_memory")
        args: list = []
        conditions = []
        if scope is not None:
            conditions.append("scope=?")
            args.append(normalize_scope(scope))
        needle = (query or "").strip().lower()
        if needle:
            conditions.append("LOWER(key) LIKE ?")
            args.append(f"%{needle}%")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [{
            "key": r["key"],
            "scope": r["scope"],
            "value": _decode(r["value"]),
            "source": r["source"] or "",
            "updated_at": r["updated_at"],
        } for r in rows]

    def get(self, key: str, scope: str | None = None):
        """取单条记忆的值；不存在返回 None。"""
        clean_key = (key or "").strip()[:MAX_KEY_CHARS]
        if not clean_key:
            return None
        scope = normalize_scope(scope)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM agent_memory WHERE key=? AND scope=?",
                (clean_key, scope)).fetchone()
        return _decode(row["value"]) if row else None

    def stats(self) -> dict:
        """记忆库统计（给前端/调试看）。"""
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM agent_memory").fetchone()["c"]
            globals_ = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_memory WHERE scope='global'"
            ).fetchone()["c"]
            sessions = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_memory "
                "WHERE scope LIKE 'session:%'").fetchone()["c"]
        return {"total": total, "global": globals_, "session_scoped": sessions,
                "max_entries": MAX_ENTRIES, "max_value_chars": MAX_VALUE_CHARS}

    def summary_for_prompt(self, session_id: str | None = None,
                           limit: int = 12) -> str:
        """
        生成一段可回灌进 system 提示词的**记忆摘要**。

        只取全局作用域 + 当前会话作用域（会话作用域优先，取最新的）。
        刻意保持很短（最多 limit 条）—— system 提示词每轮都会重发，
        塞太多会挤占上下文并拖慢本地小模型。
        """
        items = []
        seen = set()
        for scope in (normalize_scope(f"session:{session_id}" if session_id else None),
                      SCOPE_GLOBAL):
            for entry in self.recall("", scope=scope, limit=limit):
                if entry["key"] in seen:
                    continue
                seen.add(entry["key"])
                items.append(entry)
            if len(items) >= limit:
                break
        items = items[:limit]
        if not items:
            return ""
        lines = []
        for entry in items:
            value = entry["value"]
            if not isinstance(value, str):
                value = _encode(value)
            value = sanitize_value(value)
            if not value:
                continue
            lines.append(f"- {entry['key']}：{value}")
        if not lines:
            return ""
        return ("用户既往偏好（来自记忆库，仅供参考；"
                "若与本次明确指令冲突，以本次指令为准）：\n" + "\n".join(lines))


# ------------------------------------------------------------------ 单例

_STORE: MemoryStore | None = None


def get_store() -> MemoryStore:
    """全局单例。首次调用时建表。"""
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE


def reset_store(path: Path | None = None) -> MemoryStore:
    """替换全局 store（测试用）。"""
    global _STORE
    _STORE = MemoryStore(path)
    return _STORE
