#!/usr/bin/env python3
"""服务端内部耗时分布取证（只读，不碰线上进程）。

做法：
  * 把线上真实 jobs.db 复制到临时目录（不动线上库）
  * 用 H3STUDIO_DATA_DIR 指过去 import 线上那份 server.py
  * 用 sqlite3 的 set_trace_callback 统计**每条 SQL**
  * 逐阶段计时：DB 查询 / row_to_job(含 batches_of + continuation) / 收藏批查 /
    live_states_for_jobs
"""
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

LIVE = "/root/projects/h3studio-live"
REAL_DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

tmp = tempfile.mkdtemp(prefix="h3perf-")
shutil.copy2(REAL_DB, os.path.join(tmp, "jobs.db"))
os.environ["H3STUDIO_DATA_DIR"] = tmp
os.environ["H3STUDIO_PORT"] = "8399"
sys.path.insert(0, LIVE)

import server as S  # noqa: E402

SQL = []
_orig_connect = sqlite3.connect


class TracedConn(sqlite3.Connection):
    def execute(self, sql, *a, **kw):
        SQL.append(sql)
        return super().execute(sql, *a, **kw)


def _connect(*a, **kw):
    kw["factory"] = TracedConn
    return _orig_connect(*a, **kw)


sqlite3.connect = _connect

print("=== server module loaded from:", S.__file__)
print("=== DATA:", S.DATA)
print("=== DB_PATH:", S.DB_PATH)


class FakeReq:
    def __init__(self, query):
        self.query = query
        self.match_info = {}


def run_case(name, query):
    SQL.clear()
    t0 = time.time()
    # ---- phase 1: DB 主查询 ----
    t = time.time()
    with S.db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs" + (" WHERE status=?" if query.get("status") and query["status"] != "all" else "")
            + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            ([query["status"]] if query.get("status") and query["status"] != "all" else [])
            + [int(query.get("page_size", 12)), (int(query.get("page", 1)) - 1) * int(query.get("page_size", 12))],
        ).fetchall()
    t_db = time.time() - t

    # ---- phase 2: row_to_job（含 batches_of / continuation / lineage）----
    t = time.time()
    jobs = []
    for r in rows:
        job = S.row_to_job(r)
        job["batches"] = S.batches_of(r["id"])
        jobs.append(job)
    t_rows = time.time() - t

    # ---- phase 3: 收藏批查 ----
    t = time.time()
    with S.db() as conn:
        fav_ids = S.favorites.favorite_ids(conn, [j["id"] for j in jobs])
    t_fav = time.time() - t

    total = time.time() - t0
    kinds = {}
    for s in SQL:
        head = " ".join(s.split())[:70]
        kinds[head] = kinds.get(head, 0) + 1
    print(f"\n### {name}  query={query}")
    print(f"  rows={len(rows)}  SQL_count={len(SQL)}")
    print(f"  phase_db={t_db*1000:.1f}ms  phase_row_to_job+batches={t_rows*1000:.1f}ms"
          f"  phase_favorites={t_fav*1000:.1f}ms  total={total*1000:.1f}ms")
    print("  SQL breakdown:")
    for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:12]:
        print(f"    {v:4d}x {k}")
    return {"name": name, "rows": len(rows), "sql_count": len(SQL),
            "t_db_ms": round(t_db * 1000, 1),
            "t_rows_ms": round(t_rows * 1000, 1),
            "t_fav_ms": round(t_fav * 1000, 1),
            "total_ms": round(total * 1000, 1),
            "sql_breakdown": kinds}


results = []
for nm, q in [
    ("tab=全部 page_size=12", {"status": "all", "page": "1", "page_size": "12"}),
    ("tab=全部 page_size=100", {"status": "all", "page": "1", "page_size": "100"}),
    ("tab=已完成 page_size=100", {"status": "done", "page": "1", "page_size": "100"}),
    ("tab=进行中 page_size=100", {"status": "running", "page": "1", "page_size": "100"}),
    ("翻页 p=3 page_size=100", {"status": "all", "page": "3", "page_size": "100"}),
]:
    results.append(run_case(nm, q))

print("\n=== JSON ===")
print(json.dumps(results, ensure_ascii=False, indent=2))
