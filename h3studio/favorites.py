# -*- coding: utf-8 -*-
"""生成媒体库「收藏」（2026-11-20 新增需求）。

【要解决的】
生成媒体库会一直涨（跑几周就是几万张图/视频）。用户真正想「留住」的
那几十张，得有个明确的标记 —— 否则只能靠肉眼记住哪张好，第二次找不到了。

【为什么单独一张表，而不是给 jobs 加一列】
  * `jobs` 表是「任务记录」，收藏是**用户对产物的标记** —— 两件事的生命周期
    不同：任务失败会被清理，收藏是用户的决定。
  * 加列会要求所有写 jobs 的地方都记得带这个字段（INSERT 语句散落在
    server.py / agent / 迁移脚本里，漏一处就丢标记）。独立表只有一处写入点。
  * 独立表天然向后兼容：老库不需要迁移，现有记录**一条都不动**。

【安全边界】
  * 只读写 `job_favorites` 一张表；
  * 收藏只对**已存在的任务**生效（不存在就是 404）；
  * 取消收藏**只删标记**，绝不碰产物文件与任务记录；
  * 删除产物（既有删除语义）时顺带清掉悬空的收藏标记 —— 那是孤儿行，
    留着会让「收藏视图」出现一条点开是 404 的死条目。
"""
from __future__ import annotations

import time

#: 收藏表结构。
#:
#: `job_id` 主键 = 一个任务最多一条收藏记录（重复收藏是幂等更新）。
#: `created_at` = 收藏时刻（用于「按时间排序 / 显示收藏时间」）。
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_favorites (
    job_id TEXT PRIMARY KEY,
    created_at REAL NOT NULL
)
"""


def ensure_table(conn) -> None:
    """确保收藏表存在（幂等，可在每次启动时调用）。

    【向后兼容】这是**新建**表 —— 老库里没有任何记录被改动或误标。
    只加表、不动 jobs / job_batches 的任何行。
    """
    conn.execute(TABLE_SQL)


def set_favorite(conn, job_id: str, favorited: bool) -> bool:
    """把某个任务设为收藏 / 取消收藏。返回**操作后**的状态。

    幂等：重复收藏不会报错，也不会新增第二条记录。
    """
    jid = str(job_id or "").strip()
    if not jid:
        raise ValueError("job_id 不能为空")
    if favorited:
        conn.execute(
            "INSERT OR IGNORE INTO job_favorites (job_id, created_at) VALUES (?, ?)",
            (jid, time.time()),
        )
    else:
        conn.execute("DELETE FROM job_favorites WHERE job_id=?", (jid,))
    return bool(favorited)


def is_favorited(conn, job_id: str) -> bool:
    """单个任务是否已收藏。"""
    jid = str(job_id or "").strip()
    if not jid:
        return False
    row = conn.execute(
        "SELECT 1 FROM job_favorites WHERE job_id=?", (jid,)).fetchone()
    return row is not None


def favorite_ids(conn, job_ids=None) -> set[str]:
    """取一批任务的收藏状态（一次查询，避免 N+1）。

    job_ids 为 None 时返回**全部**收藏 id。
    """
    if job_ids is None:
        rows = conn.execute("SELECT job_id FROM job_favorites").fetchall()
        return {str(r[0]) for r in rows}
    ids = [str(j) for j in job_ids if j]
    if not ids:
        return set()
    out: set[str] = set()
    # 分批 IN 查询 —— SQLite 的变量上限是 999，收藏多了不能一次塞进去。
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT job_id FROM job_favorites WHERE job_id IN (%s)" % placeholders,
            chunk,
        ).fetchall()
        out.update(str(r[0]) for r in rows)
    return out


def count(conn) -> int:
    """收藏总数。"""
    row = conn.execute("SELECT COUNT(*) FROM job_favorites").fetchone()
    return int(row[0]) if row else 0


def remove_for_job(conn, job_id: str) -> None:
    """删除任务时顺带清掉它的收藏标记（防孤儿行）。

    【注意】这里**只**删标记行，不碰产物文件 —— 产物文件的删除
    走既有删除语义（server.api_delete / media_cleanup）。
    """
    jid = str(job_id or "").strip()
    if not jid:
        return
    conn.execute("DELETE FROM job_favorites WHERE job_id=?", (jid,))
