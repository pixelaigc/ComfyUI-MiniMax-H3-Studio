# -*- coding: utf-8 -*-
"""提示词历史（Prompt History）—— 各功能面板的提示词留痕。

【为什么独立成模块】
项目已有的 `jobs` 表只存「任务」，而提示词历史要回答的是另一个问题：
「这个面板上我用过哪些提示词？原始版是什么？增强版是什么？」
所以单独立一张表，而不是往 jobs.params 里塞字段 ——
否则「查历史」要全表扫描 jobs，还要 json_extract 拆 params。

【数据模型】
    id            自增主键（分页游标 / 定位用）
    panel_key     功能/模型标识（t2i / t2i_qwen / i2i_qwen / t2i_moss /
                  t2v / flf / r2v / t2a / a2a …）—— **跨面板隔离的判据**
    model_key     该次提交实际用的模型 key（可空）
    original_prompt  原始提示词（用户写的）
    enhanced_prompt  最终使用的提示词（增强后）；未走增强流程时为空串
    source        记录来源：generate（提交生成）/ enhance（增强流程）
    created_at    时间戳（epoch 秒）
    use_count     该提示词被**提交生成**的次数（增强不计数）

【去重口径】
同一面板 + 同一原始提示词 = 同一条记录。
重复提交时**不新增行**，而是：
    * use_count += 1（仅提交生成才计）
    * created_at 更新为最近一次时间（历史列表按最近排）
    * enhanced_prompt：本次有**不同的**增强结果就覆盖；
      本次没增强（或增强结果与原文相同）就保留历史的 ——
      否则「未增强直出」会把之前那次增强的成果抹掉。

【上限】
每个面板最多保留 MAX_PER_PANEL 条，超出时删最旧的（created_at 最小）。
去重后总表大小有硬上限（面板数 × 每面板上限），不会无限增长。

【分页】
读取接口一律带 limit / offset，**不返回全量** ——
前端浮层滚动加载用。默认 limit=20，MAX_LIMIT=100。
"""

from __future__ import annotations

import sqlite3
import time

# 每个面板保留的最大条数。超出时按 created_at 淘汰最旧的。
MAX_PER_PANEL = 200

# 读取接口的默认 / 最大分页大小。
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# 记录来源
SOURCE_GENERATE = "generate"
SOURCE_ENHANCE = "enhance"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_key TEXT NOT NULL,
    model_key TEXT NOT NULL DEFAULT '',
    original_prompt TEXT NOT NULL,
    enhanced_prompt TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'generate',
    created_at REAL NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 1
);
-- 同面板同原文的查重 + 按时间排序，各走一条索引
CREATE INDEX IF NOT EXISTS idx_prompt_history_panel_orig
    ON prompt_history(panel_key, original_prompt);
CREATE INDEX IF NOT EXISTS idx_prompt_history_panel_time
    ON prompt_history(panel_key, created_at DESC);
-- 按模型过滤（读取接口的 model_key 过滤走这条）
CREATE INDEX IF NOT EXISTS idx_prompt_history_panel_model
    ON prompt_history(panel_key, model_key);
"""


def ensure_table(conn: sqlite3.Connection) -> None:
    """建表（幂等）。由 server.init_db() 在启动时调用。"""
    conn.executescript(_SCHEMA)


def _now() -> float:
    return time.time()


def _normalize_prompt(text: str) -> str:
    """提示词归一：只去首尾空白。

    **不做**内部空白折叠 —— 提示词里的换行与缩进是有意义的
    （多镜头分镜写法、歌词段落标记），折叠会毁掉内容。
    """
    return (text or "").strip()


def record(
    conn: sqlite3.Connection,
    panel_key: str,
    original_prompt: str,
    enhanced_prompt: str = "",
    model_key: str = "",
    source: str = SOURCE_GENERATE,
) -> int:
    """写入一条提示词历史（同一面板 + 同一原文则合并计数）。

    返回该记录的 id（新增或命中已有记录都是它；0 = 没写进去）。
    """
    panel_key = (panel_key or "").strip()
    original = _normalize_prompt(original_prompt)
    if not panel_key or not original:
        return 0
    enhanced = _normalize_prompt(enhanced_prompt)
    # 「增强结果与原文相同」= 这次没真增强 —— 不能算作有增强版本。
    if enhanced == original:
        enhanced = ""
    now = _now()
    is_generate = source == SOURCE_GENERATE

    row = conn.execute(
        "SELECT id, enhanced_prompt, use_count FROM prompt_history "
        "WHERE panel_key=? AND original_prompt=? ORDER BY id LIMIT 1",
        (panel_key, original),
    ).fetchone()

    if row is not None:
        # 命中已有记录 —— 更新计数与时间，不新增行。
        # 增强结果：本次真增强了就用本次的（最新的那次更有意义）；
        # 本次没增强但历史上有 —— 保留历史的（否则「未增强直出」会把
        # 之前那次增强的记录抹掉）。
        old_enhanced = row["enhanced_prompt"] or ""
        new_enhanced = enhanced or old_enhanced
        # use_count 只统计「提交生成」的次数 —— 增强本身不算一次使用。
        new_count = int(row["use_count"] or 0) + (1 if is_generate else 0)
        conn.execute(
            "UPDATE prompt_history SET use_count=?, created_at=?, "
            "enhanced_prompt=?, model_key=CASE WHEN ?<>'' THEN ? ELSE model_key END, "
            "source=? WHERE id=?",
            (new_count, now, new_enhanced, model_key, model_key, source, row["id"]),
        )
        record_id = int(row["id"])
    else:
        cur = conn.execute(
            "INSERT INTO prompt_history "
            "(panel_key, model_key, original_prompt, enhanced_prompt, source, created_at, use_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (panel_key, model_key, original, enhanced, source, now,
             1 if is_generate else 0),
        )
        record_id = int(cur.lastrowid or 0)

    _trim_panel(conn, panel_key)
    return record_id


def _trim_panel(conn: sqlite3.Connection, panel_key: str) -> None:
    """每个面板只保留最近 MAX_PER_PANEL 条 —— 超出删最旧的。"""
    conn.execute(
        "DELETE FROM prompt_history WHERE panel_key=? AND id NOT IN ("
        "  SELECT id FROM prompt_history WHERE panel_key=? "
        "  ORDER BY created_at DESC, id DESC LIMIT ?)",
        (panel_key, panel_key, MAX_PER_PANEL),
    )


def list_history(
    conn: sqlite3.Connection,
    panel_key: str = "",
    model_key: str = "",
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[dict]:
    """按面板（+ 模型）读取历史（分页）。

    panel_key 为空 = 不按面板过滤（返回全部面板，按时间倒序）。
    model_key 为空 = 不按模型过滤。
    """
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    conditions = []
    args: list = []
    if panel_key:
        conditions.append("panel_key=?")
        args.append(panel_key)
    if model_key:
        conditions.append("model_key=?")
        args.append(model_key)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    args.extend([limit, offset])
    rows = conn.execute(
        f"SELECT * FROM prompt_history{where} "
        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        args,
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def count_history(conn: sqlite3.Connection, panel_key: str = "",
                  model_key: str = "") -> int:
    """某个面板（或全部）的记录总数 —— 前端判断「还有没有更多」。"""
    conditions = []
    args: list = []
    if panel_key:
        conditions.append("panel_key=?")
        args.append(panel_key)
    if model_key:
        conditions.append("model_key=?")
        args.append(model_key)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM prompt_history{where}", args).fetchone()
    return int(row["n"] if row else 0)


def row_to_dict(row) -> dict:
    """数据库行 -> 接口返回结构。"""
    original = row["original_prompt"] or ""
    enhanced = row["enhanced_prompt"] or ""
    return {
        "id": int(row["id"]),
        "panel_key": row["panel_key"],
        "model_key": row["model_key"] or "",
        "original_prompt": original,
        "enhanced_prompt": enhanced,
        # 有增强版本 = 这条记录带「原始 -> 增强」的对应关系
        "has_enhanced": bool(enhanced) and enhanced != original,
        "source": row["source"] or SOURCE_GENERATE,
        "timestamp": float(row["created_at"]),
        "use_count": int(row["use_count"] or 0),
    }


def list_panels(conn: sqlite3.Connection) -> list[dict]:
    """每个面板的历史条数（前端可用于显示「历史 (12)」）。"""
    rows = conn.execute(
        "SELECT panel_key, COUNT(*) AS n FROM prompt_history "
        "GROUP BY panel_key ORDER BY panel_key").fetchall()
    return [{"panel_key": r["panel_key"], "count": int(r["n"])} for r in rows]


def delete_panel(conn: sqlite3.Connection, panel_key: str) -> int:
    """清空某个面板的历史（接口预留）。"""
    cur = conn.execute("DELETE FROM prompt_history WHERE panel_key=?", (panel_key,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
