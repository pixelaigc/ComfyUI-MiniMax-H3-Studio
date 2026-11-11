#!/usr/bin/env python3
"""
线上自检：Agent 会话管理（部署到 8190 之后跑）。

检查项：
  1. 服务 active + 关键端点 200
  2. /api/agent/sessions 能列出真实会话
  3. POST /api/agent/sessions 能新建（并出现在列表里）
  4. GET /api/agent/sessions/{id} 能读回详情（消息 + 产物）
  5. GET /api/agent/sessions/{id}/jobs 能读回该会话的任务
  6. DELETE /api/agent/sessions/{id} 能删掉
  7. 生产 agent_sessions.db 的 meta 列已迁移
  8. 生产 jobs.db 的 session_id 列已迁移
  9. journal 无 error/traceback

**会真的写生产库**（建一个自检会话再删掉）—— 这是必须的：
只读检查证明不了「新建/删除真的落库」。
自检会话用固定标题，结束时一定删掉（不留垃圾）。
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_URL", "http://127.0.0.1:8190")
SELF_TITLE = "__会话管理自检临时会话__"

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(f"{name} {detail}")


def api(path, method="GET", body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"error": raw}


def main():
    print("=== 线上自检：Agent 会话管理 ===")

    # 1) 服务状态
    svc = subprocess.run(["systemctl", "is-active", "h3-studio.service"],
                         capture_output=True, text=True).stdout.strip()
    check("服务 active", svc == "active", f"实际 {svc}")

    for path in ("/", "/api/agent/sessions", "/api/agent/tools", "/api/jobs"):
        status, _ = api(path)
        check(f"端点 {path} -> 200", status == 200, f"实际 {status}")

    # 2) 列表
    status, data = api("/api/agent/sessions")
    sessions_before = len(data.get("sessions") or [])
    check("会话列表可读", status == 200 and isinstance(data.get("sessions"), list),
          f"status={status}")

    # 3) 新建
    status, created = api("/api/agent/sessions", "POST", {"title": SELF_TITLE})
    sid = created.get("session_id") or ""
    check("新建会话 -> 200 且返回 id", status == 200 and bool(sid),
          f"status={status} body={created}")

    if not sid:
        print("  跳过后续检查（没有拿到 session_id）")
        return 1

    try:
        # 列表里能看到
        status, data = api("/api/agent/sessions")
        ids = [s.get("id") for s in data.get("sessions") or []]
        check("新建的会话出现在列表里", sid in ids, f"ids={ids}")
        check("会话数 +1", len(ids) == sessions_before + 1,
              f"{sessions_before} -> {len(ids)}")

        item = next((s for s in data.get("sessions") or []
                     if s.get("id") == sid), {})
        check("会话带标题", bool(item.get("title")), f"title={item.get('title')}")
        check("会话带 message_count", "message_count" in item,
              f"item={item}")

        # 4) 详情
        status, detail = api("/api/agent/sessions/" + sid)
        check("会话详情 -> 200", status == 200, f"status={status}")
        check("详情带 messages 数组", isinstance(detail.get("messages"), list),
              f"messages={detail.get('messages')}")
        check("详情带 jobs 数组", isinstance(detail.get("jobs"), list),
              f"jobs={detail.get('jobs')}")

        # 5) 产物接口
        status, jobs = api("/api/agent/sessions/" + sid + "/jobs")
        check("会话 jobs 接口 -> 200", status == 200, f"status={status}")
        check("jobs 接口带 count", "count" in jobs, f"body={jobs}")

        # 6) 删除
        status, deleted = api("/api/agent/sessions/" + sid, "DELETE")
        check("删除会话 -> 200", status == 200, f"status={status}")
        check("删除返回 ok=True", deleted.get("ok") is True, f"body={deleted}")

        status, data = api("/api/agent/sessions")
        ids2 = [s.get("id") for s in data.get("sessions") or []]
        check("删除后不再出现在列表里", sid not in ids2, f"ids={ids2}")
    finally:
        # 兜底：万一中途出错，自检会话也要删掉
        api("/api/agent/sessions/" + sid, "DELETE")

    # 7) 生产会话库 meta 列
    sess_db = Path("/root/projects/h3studio-live/data/agent_sessions.db")
    if sess_db.exists():
        cols = {r[1] for r in sqlite3.connect(sess_db).execute(
            "PRAGMA table_info(agent_messages)")}
        check("agent_sessions.db 已迁移 meta 列", "meta" in cols, f"cols={sorted(cols)}")
    else:
        check("agent_sessions.db 存在", False, str(sess_db))

    # 8) 生产任务库 session_id 列
    jobs_db = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")
    if jobs_db.exists():
        cols = {r[1] for r in sqlite3.connect(jobs_db).execute(
            "PRAGMA table_info(jobs)")}
        check("jobs.db 已迁移 session_id 列", "session_id" in cols,
              f"cols={sorted(cols)}")
    else:
        check("jobs.db 存在", False, str(jobs_db))

    # 9) journal
    out = subprocess.run(
        ["journalctl", "-u", "h3-studio.service", "--since", "-15 min",
         "--no-pager"], capture_output=True, text=True).stdout
    bad = [ln for ln in out.splitlines()
           if any(k in ln.lower() for k in ("error", "traceback", "exception"))
           and "self-check" not in ln.lower()]
    check("journal 无 error/traceback", not bad, f"{bad[:3]}")

    print(f"\n=== {checks - len(failures)}/{checks} passed ===")
    if failures:
        for f in failures:
            print("  FAILED:", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
