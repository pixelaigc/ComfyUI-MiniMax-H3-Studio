# -*- coding: utf-8 -*-
"""
修复后的**验收测试**（真实浏览器 / 真实 Studio 实例）。

覆盖用户要求的三项验证：
  a) 两条带快捷选项的 Agent 消息 -> 点第二条的选项 -> 只有第二条输入框被填；
     跨会话切换后同样不串填。
  b) 点「新建交互」-> URL / 选中 session 确实变化。
  c) 同一 session 反复切换 3 次 -> 工具区布局与密度一致。

另外加一条守护：会话 A 展开的工具行，切到 B 再切回 A，必须**恢复成展开**
（这才是「密度一致」的完整语义，不只是「默认值一致」）。

跑法：python3 verify_agent_ui.py <repo_root> <out_dir>
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3studio-agent-ui")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "./evidence_verify")
OUT.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright   # noqa: E402
from test_frontend_defaults import StudioServer  # noqa: E402


TWO_CLARIFY_MESSAGES = [
    {
        "role": "assistant",
        "content": "第一组：我先确认一下分镜政策。",
        "clarify": {
            "question": "先处理哪个？",
            "options": ["帮我压缩分镜政策到 3 条", "先完成一段 15 秒的开场试戏"],
        },
        "tools": [{"name": "list_modes", "ok": True, "ms": 12,
                   "arguments": "{}", "result": "t2v, flf, r2v"}],
    },
    {
        "role": "assistant",
        "content": "第二组：画面比例怎么定？",
        "clarify": {
            "question": "选一个画幅方案。",
            "options": ["16:9 横屏·吉凡暗调", "9:16 竖屏",
                        "16:9 横屏·标红圆角边框开场"],
        },
        "tools": [{"name": "search_assets", "ok": True, "ms": 30,
                   "arguments": "{\"q\":\"雨夜\"}", "result": "命中 12 条"}],
    },
]


def seed_session(db: Path, sid: str, title: str, tool_msgs):
    now = time.time()
    conn = sqlite3.connect(db)
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
    conn.execute("INSERT OR REPLACE INTO agent_sessions "
                 "(id, created_at, updated_at, title) VALUES (?,?,?,?)",
                 (sid, now, now, title))
    conn.execute("DELETE FROM agent_messages WHERE session_id=?", (sid,))
    conn.execute("INSERT INTO agent_messages "
                 "(session_id, role, content, tool_name, meta, created_at) "
                 "VALUES (?,?,?,?,?,?)", (sid, "user", title, "", "", now))
    conn.execute("INSERT INTO agent_messages "
                 "(session_id, role, content, tool_name, meta, created_at) "
                 "VALUES (?,?,?,?,?,?)",
                 (sid, "assistant", "已完成一轮。", "",
                  json.dumps({"status": "completed"}, ensure_ascii=False), now))
    for i, (tool_name, note) in enumerate(tool_msgs):
        conn.execute("INSERT INTO agent_messages "
                     "(session_id, role, content, tool_name, meta, created_at) "
                     "VALUES (?,?,?,?,?,?)",
                     (sid, "tool", f"{tool_name}: {note}", tool_name, "", now + i))
    conn.commit()
    conn.close()


MEASURE = """() => {
  const blocks = Array.from(document.querySelectorAll('.ag-tools'));
  const rows = Array.from(document.querySelectorAll('.ag-tool'));
  let total = 0;
  blocks.forEach(b => { total += b.getBoundingClientRect().height; });
  return {
    toolBlocks: blocks.length,
    toolRows: rows.length,
    totalToolAreaH: Math.round(total),
    blockH: blocks.map(b => Math.round(b.getBoundingClientRect().height)),
    rowH: rows.map(x => Math.round(x.getBoundingClientRect().height)),
    rowOpen: rows.map(x => x.open),
    rowKeys: rows.map(x => x.getAttribute('data-detail-key')),
  };
}"""



def open_drawer(page):
    """打开会话抽屉，并等到它**真的**打开（不等固定时长）。

    【为什么不等固定时长】
    抽屉有开合过渡。固定 sleep 会偶发撞在动画中途 ——
    按钮还没到可点位置，click 会「点了没反应」。
    实测这一条会让 b4/b5 偶发失败（排查后确认是**测试脚本时序问题**，
    不是产品缺陷）。等 DOM 状态（class）才是确定性的。
    """
    if page.evaluate(
            "()=>document.querySelector('#agentPage')"
            ".classList.contains('ag-drawer-open')"):
        return
    page.click("#agMenu")
    page.wait_for_function(
        "()=>document.querySelector('#agentPage')"
        ".classList.contains('ag-drawer-open')",
        timeout=5000)
    page.wait_for_timeout(150)

def main() -> int:
    server = StudioServer()
    if not server.start():
        print("!! Studio 实例启动失败")
        return 2
    db = server.data_dir / "agent_sessions.db"
    seed_session(db, "sess-alpha", "会话A 密度基准",
                 [("list_modes", "ok"), ("search_assets", "ok")])
    seed_session(db, "sess-bravo", "会话B 中转", [("search_assets", "ok")])

    console: list[str] = []
    page_errors: list[str] = []
    results: dict = {}
    failed: list[str] = []

    def check(name, cond, detail=""):
        results[name] = {"pass": bool(cond), "detail": detail}
        if not cond:
            failed.append(name)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
              + (f"  ({detail})" if detail else ""))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
            page.click("#navAgent")
            page.wait_for_timeout(800)

            # =============== a) 快捷选项不串填 ===============
            print("\n=== a) 快捷选项作用域定位 ===")
            page.evaluate(
                """(msgs) => {
                  window.AgentUI.state.messages = msgs.map(m => Object.assign(
                    {content:'', result:{}, jobs:[], assets:[], prompt:null,
                     steps:[], tools:[], streaming:false, error:''}, m));
                  window.AgentUI.renderMessages();
                }""",
                TWO_CLARIFY_MESSAGES)
            page.wait_for_timeout(400)
            before = page.eval_on_selector_all(
                ".ag-clarify-input", "els => els.map(e => e.value)")
            check("a1 页面存在两组快捷选项",
                  len(page.query_selector_all(".ag-clarify-opt")) == 5,
                  f"共 {len(page.query_selector_all('.ag-clarify-opt'))} 个")
            check("a2 两个澄清输入框初始为空",
                  before == ["", ""], str(before))

            opts = page.query_selector_all(".ag-clarify-opt")
            clicked = opts[-1].inner_text().strip()
            opts[-1].click()
            page.wait_for_timeout(300)
            after = page.eval_on_selector_all(
                ".ag-clarify-input", "els => els.map(e => e.value)")
            print("    点击的选项:", clicked)
            print("    点击后输入框:", after)
            check("a3 只填了第二条消息的输入框",
                  after[0] == "" and after[1] == clicked, str(after))
            focused = page.evaluate("""() => {
              const a = document.activeElement;
              const all = Array.from(document.querySelectorAll('.ag-clarify-input'));
              return all.indexOf(a);
            }""")
            check("a4 焦点落在第二条消息的输入框", focused == 1, f"index={focused}")
            picked = page.eval_on_selector_all(
                ".ag-clarify-opt-picked", "els => els.map(e => e.textContent)")
            check("a5 已选项被高亮且本组其余禁用",
                  len(picked) == 1 and picked[0] == clicked
                  and all(o.is_disabled() for o in
                          page.query_selector_all(".ag-clarify-options")[1]
                          .query_selector_all(".ag-clarify-opt")),
                  str(picked))
            # 【计算样式断言 —— 不能只看 class】
            # 踩过的坑：.ag-clarify-opt:disabled 的特异性 (0,2,0) 高于
            # .ag-clarify-opt-picked (0,1,0) -> 已选中的按钮反而被淡化，
            # 视觉上像「禁用」。class 断言抓不到这种问题，必须量计算样式。
            styles = page.evaluate("""() => {
              const grp = document.querySelectorAll('.ag-clarify-options')[1];
              const opts = Array.from(grp.querySelectorAll('.ag-clarify-opt'));
              const picked = opts.find(o => o.classList.contains('ag-clarify-opt-picked'));
              const other = opts.find(o => !o.classList.contains('ag-clarify-opt-picked'));
              const g = el => { const s = getComputedStyle(el); return {
                bg: s.backgroundColor, color: s.color, opacity: s.opacity,
                weight: s.fontWeight }; };
              return picked ? {picked: g(picked), other: g(other)} : null;
            }""")
            print("    计算样式:", json.dumps(styles, ensure_ascii=False))
            check("a5b 已选中的按钮计算样式与其它按钮明显不同",
                  bool(styles)
                  and styles["picked"]["bg"] != styles["other"]["bg"]
                  and float(styles["picked"]["opacity"]) == 1.0
                  and float(styles["other"]["opacity"]) < 1.0,
                  json.dumps(styles, ensure_ascii=False))
            page.screenshot(path=str(OUT / "a-clarify-scoped.png"))

            # ---- 跨会话后同样不串填 ----
            open_drawer(page)
            page.click('.ag-session-item[data-session-id="sess-bravo"]')
            page.wait_for_timeout(1000)
            page.evaluate(
                """(msgs) => {
                  window.AgentUI.state.messages = msgs.map(m => Object.assign(
                    {content:'', result:{}, jobs:[], assets:[], prompt:null,
                     steps:[], tools:[], streaming:false, error:''}, m));
                  window.AgentUI.renderMessages();
                }""",
                TWO_CLARIFY_MESSAGES)
            page.wait_for_timeout(400)
            b_before = page.eval_on_selector_all(
                ".ag-clarify-input", "els => els.map(e => e.value)")
            bopts = page.query_selector_all(".ag-clarify-opt")
            bclicked = bopts[-1].inner_text().strip()
            bopts[-1].click()
            page.wait_for_timeout(300)
            b_after = page.eval_on_selector_all(
                ".ag-clarify-input", "els => els.map(e => e.value)")
            print("    跨会话后点击:", bclicked, "->", b_after)
            check("a6 跨会话后仍不串填",
                  b_before == ["", ""] and b_after[0] == "" and b_after[1] == bclicked,
                  f"{b_before} -> {b_after}")
            page.screenshot(path=str(OUT / "a-cross-session.png"))

            # =============== b) 新建交互跳转 ===============
            print("\n=== b) 新建交互 -> 创建 + 跳转 ===")
            open_drawer(page)
            page.click('.ag-session-item[data-session-id="sess-alpha"]')
            page.wait_for_timeout(1000)
            url_before = page.url
            sid_before = page.evaluate("() => window.AgentUI.state.sessionId")
            msgs_before = page.evaluate(
                "() => window.AgentUI.state.messages.length")
            active_before = page.eval_on_selector_all(
                ".ag-session-item.ag-active", "els => els.map(e => e.dataset.sessionId)")

            # 【开抽屉必须等它真的打开】—— 只等固定时长会偶发撞上
            # 抽屉开合动画的中途态（按钮还没到可点位置），
            # 实测会偶发「点了没反应」—— 那是**测试脚本的时序问题**，
            # 不是产品缺陷。改成等 DOM 状态，不等时间。
            open_drawer(page)
            page.click("#agDrawerNew")
            page.wait_for_timeout(1500)
            url_after = page.url
            sid_after = page.evaluate("() => window.AgentUI.state.sessionId")
            msgs_after = page.evaluate(
                "() => window.AgentUI.state.messages.length")
            active_after = page.eval_on_selector_all(
                ".ag-session-item.ag-active", "els => els.map(e => e.dataset.sessionId)")
            print("    URL:", url_before, "->", url_after)
            print("    sid:", sid_before, "->", sid_after)
            print("    消息数:", msgs_before, "->", msgs_after)
            print("    选中:", active_before, "->", active_after)
            check("b1 URL 变了（路由跳转到新 session）",
                  url_before != url_after, url_after)
            check("b2 URL 指向新 session",
                  sid_after and sid_after in url_after, url_after)
            check("b3 AgentUI.sessionId 切到新 session",
                  bool(sid_after) and sid_after != sid_before, str(sid_after))
            check("b4 侧栏选中态切到新 session",
                  active_after == [sid_after], str(active_after))
            check("b5 消息区已清空（旧会话消息不再残留）",
                  msgs_after == 0, f"{msgs_before} -> {msgs_after}")
            page.screenshot(path=str(OUT / "b-new-session-jump.png"))

            # =============== c) 工具密度一致 ===============
            print("\n=== c) 工具密度一致性 ===")
            open_drawer(page)
            page.click('.ag-session-item[data-session-id="sess-alpha"]')
            page.wait_for_timeout(1200)
            base = page.evaluate(MEASURE)
            print("    会话A 基准:", json.dumps(base, ensure_ascii=False))

            # 展开 A 的第一个工具行（模拟用户点开看细节）
            page.evaluate("""() => {
              const d = document.querySelector('.ag-tool');
              if (d) d.open = true;
              window.AgentUI.renderMessages();
            }""")
            page.wait_for_timeout(300)
            after_open = page.evaluate(MEASURE)
            print("    展开一行后:", json.dumps(after_open, ensure_ascii=False))

            rounds = []
            for i in range(3):
                open_drawer(page)
                page.click('.ag-session-item[data-session-id="sess-bravo"]')
                page.wait_for_timeout(900)
                on_b = page.evaluate(MEASURE)
                open_drawer(page)
                page.click('.ag-session-item[data-session-id="sess-alpha"]')
                page.wait_for_timeout(900)
                back = page.evaluate(MEASURE)
                rounds.append({"round": i + 1, "on_bravo": on_b, "back_on_alpha": back})
                print(f"    第 {i+1} 轮切回A:", json.dumps(back, ensure_ascii=False))

            def geom(m):
                return {"toolBlocks": m["toolBlocks"], "toolRows": m["toolRows"],
                        "totalToolAreaH": m["totalToolAreaH"],
                        "blockH": m["blockH"], "rowH": m["rowH"]}

            check("c1 往返 3 次后工具区几何完全一致",
                  all(geom(r["back_on_alpha"]) == geom(after_open) for r in rounds),
                  f"基准={geom(after_open)} 实际={[geom(r['back_on_alpha']) for r in rounds]}")
            check("c2 展开态被保留（切回来还是展开的）",
                  all(r["back_on_alpha"]["rowOpen"] == after_open["rowOpen"]
                      for r in rounds),
                  f"基准 rowOpen={after_open['rowOpen']}")
            check("c3 恢复路径与实时路径密度一致（同为 1 个工具容器）",
                  base["toolBlocks"] == 1,
                  f"toolBlocks={base['toolBlocks']}（修复前是 2）")
            check("c4 折叠态 key 带会话作用域（跨会话不撞）",
                  all(r["on_bravo"]["rowKeys"] != r["back_on_alpha"]["rowKeys"]
                      for r in rounds),
                  f"B={rounds[0]['on_bravo']['rowKeys']} A={rounds[0]['back_on_alpha']['rowKeys']}")
            page.screenshot(path=str(OUT / "c-density-stable.png"))

            # =============== 附加：无 JS 报错 ===============
            check("d1 无 pageerror", not page_errors, "; ".join(page_errors[:3]))
            err_logs = [c for c in console if c.startswith("[error]")]
            check("d2 无 console error", not err_logs, "; ".join(err_logs[:3]))

            (OUT / "console.log").write_text("\n".join(console), encoding="utf-8")
            (OUT / "pageerrors.log").write_text("\n".join(page_errors), encoding="utf-8")
            browser.close()
    finally:
        server.stop()

    summary = {"results": results, "failed": failed,
               "all_passed": not failed}
    (OUT / "verify-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 总结 ===")
    print(f"通过 {len(results) - len(failed)}/{len(results)}")
    if failed:
        print("失败项:", failed)
    print("证据目录:", OUT)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
