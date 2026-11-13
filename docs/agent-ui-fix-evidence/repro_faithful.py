# -*- coding: utf-8 -*-
"""
忠实复现脚本 —— B（新建交互不跳转）+ C（工具密度漂移）。

与 repro_before.py 的区别：
  * 直接往**服务端真实 SQLite 会话库**里写消息（和 server.py 落库的形状一致），
    走真实的 GET /api/agent/sessions/{id} -> restoreMessages -> renderMessages 链路；
  * 切换会话走**真实 UI**（点会话列表项），不是直接调内部函数；
  * 测密度时对比「切走前」与「切回后」的**工具行几何**。

跑法：python3 repro_faithful.py <repo_root> <out_dir>
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3studio-agent-ui")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "./evidence")
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright   # noqa: E402
from test_frontend_defaults import StudioServer  # noqa: E402


def seed_session(db: Path, sid: str, title: str, tool_msgs: list[tuple[str, str]]):
    """按 server.py 的落库形状写一条会话：user + assistant(meta) + N 条 tool。"""
    now = time.time()
    conn = sqlite3.connect(db)
    # 会话库是**懒建表**的（get_store() 首次调用才建），
    # 所以这里先按 h3studio/agent_sessions.py 的 schema 建好。
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id TEXT PRIMARY KEY, created_at REAL, updated_at REAL, title TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT DEFAULT '', tool_name TEXT DEFAULT '',
            meta TEXT DEFAULT '', created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_messages_session
            ON agent_messages(session_id, id);
        """)
    conn.execute(
        "INSERT OR REPLACE INTO agent_sessions (id, created_at, updated_at, title) "
        "VALUES (?,?,?,?)", (sid, now, now, title))
    conn.execute("DELETE FROM agent_messages WHERE session_id=?", (sid,))
    conn.execute(
        "INSERT INTO agent_messages (session_id, role, content, tool_name, meta, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (sid, "user", title, "", "", now))
    meta = json.dumps({"status": "completed"}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO agent_messages (session_id, role, content, tool_name, meta, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (sid, "assistant", "已完成一轮。", "", meta, now))
    for i, (tool_name, note) in enumerate(tool_msgs):
        conn.execute(
            "INSERT INTO agent_messages (session_id, role, content, tool_name, meta, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (sid, "tool", f"{tool_name}: {note}", tool_name, "", now + i))
    conn.commit()
    conn.close()


MEASURE = """() => {
  const rows = Array.from(document.querySelectorAll('.ag-tool'));
  const blocks = Array.from(document.querySelectorAll('.ag-tools'));
  const steps = Array.from(document.querySelectorAll('.ag-step'));
  const r = el => { const b = el.getBoundingClientRect();
    return {h: Math.round(b.height), w: Math.round(b.width)}; };
  return {
    toolRows: rows.length,
    stepRows: steps.length,
    toolBlocks: blocks.length,
    blockH: blocks.map(b => Math.round(b.getBoundingClientRect().height)),
    rowH: rows.map(x => Math.round(x.getBoundingClientRect().height)),
    rowOpen: rows.map(x => x.open),
    stepH: steps.map(x => Math.round(x.getBoundingClientRect().height)),
    listH: (() => { const l = document.querySelector('.ag-tool-list');
                     return l ? r(l) : null; })(),
    firstRowText: rows.length ? rows[0].textContent.trim().slice(0, 60) : null,
  };
}"""


def main() -> int:
    server = StudioServer()
    if not server.start():
        print("!! Studio 实例启动失败")
        return 2
    db = server.data_dir / "agent_sessions.db"
    print("db =", db, "exists =", db.exists())

    seed_session(db, "sess-alpha", "会话A 密度基准",
                 [("list_modes", "ok"), ("search_assets", "ok")])
    seed_session(db, "sess-bravo", "会话B 中转",
                 [("search_assets", "ok")])

    console: list[str] = []
    page_errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            page.click("#navAgent")
            page.wait_for_timeout(800)

            # ---------- B. 新建交互 ----------
            url_before = page.url
            hash_before = page.evaluate("() => location.hash")
            sid_before = page.evaluate("() => window.AgentUI.state.sessionId")
            msgs_before = page.evaluate(
                "() => window.AgentUI.state.messages.length")
            page.click("#agMenu")
            page.wait_for_timeout(300)
            page.click("#agDrawerNew")
            page.wait_for_timeout(1500)
            url_after = page.url
            hash_after = page.evaluate("() => location.hash")
            sid_after = page.evaluate("() => window.AgentUI.state.sessionId")
            msgs_after = page.evaluate(
                "() => window.AgentUI.state.messages.length")
            active_after = page.eval_on_selector_all(
                ".ag-session-item.ag-active",
                "els => els.map(e => e.dataset.sessionId)")
            b_result = {
                "url_before": url_before, "url_after": url_after,
                "hash_before": hash_before, "hash_after": hash_after,
                "sid_before": sid_before, "sid_after": sid_after,
                "messages_before": msgs_before, "messages_after": msgs_after,
                "active_session_after": active_after,
                "url_changed": url_before != url_after,
                "sid_changed": bool(sid_after) and sid_after != sid_before,
                "messages_cleared": msgs_after == 0,
            }
            print("[B]", json.dumps(b_result, ensure_ascii=False, indent=2))
            page.screenshot(path=str(OUT / "B-new-session.png"))
            (OUT / "B-new-session.json").write_text(
                json.dumps(b_result, ensure_ascii=False, indent=2), encoding="utf-8")

            # ---------- C. 密度漂移（真实链路） ----------
            # 先切到会话A（密度基准）
            page.click("#agMenu")
            page.wait_for_timeout(300)
            page.click('.ag-session-item[data-session-id="sess-alpha"]')
            page.wait_for_timeout(1200)
            base = page.evaluate(MEASURE)
            print("[C] 会话A 基准 =", json.dumps(base, ensure_ascii=False))

            # 切到 B，再切回 A，来回 3 次
            rounds = []
            for i in range(3):
                page.click("#agMenu")
                page.wait_for_timeout(250)
                page.click('.ag-session-item[data-session-id="sess-bravo"]')
                page.wait_for_timeout(900)
                mid = page.evaluate(MEASURE)
                page.click("#agMenu")
                page.wait_for_timeout(250)
                page.click('.ag-session-item[data-session-id="sess-alpha"]')
                page.wait_for_timeout(900)
                back = page.evaluate(MEASURE)
                rounds.append({"round": i + 1, "on_bravo": mid, "back_on_alpha": back})
                print(f"[C] 第 {i+1} 轮 切回A =",
                      json.dumps(back, ensure_ascii=False))
                print(f"[C] 第 {i+1} 轮 B   =",
                      json.dumps(mid, ensure_ascii=False))

            def key(m):
                return {"toolRows": m["toolRows"], "blockH": m["blockH"],
                        "rowH": m["rowH"], "rowOpen": m["rowOpen"]}

            stable = all(key(r["back_on_alpha"]) == key(base) for r in rounds)
            c_result = {
                "baseline_alpha": base,
                "rounds": rounds,
                "stable": stable,
                "baseline_key": key(base),
                "round_keys": [key(r["back_on_alpha"]) for r in rounds],
            }
            print("[C] 密度稳定 =", stable)
            page.screenshot(path=str(OUT / "C-density.png"))
            (OUT / "C-density.json").write_text(
                json.dumps(c_result, ensure_ascii=False, indent=2), encoding="utf-8")

            (OUT / "console.log").write_text("\n".join(console), encoding="utf-8")
            (OUT / "pageerrors.log").write_text("\n".join(page_errors), encoding="utf-8")
            browser.close()
    finally:
        server.stop()

    print("\n=== 证据目录:", OUT, "===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
