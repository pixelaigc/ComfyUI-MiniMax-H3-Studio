# -*- coding: utf-8 -*-
"""
BUG 复现脚本（修复前基线取证）。

三个待复现的问题：
  A. 快捷选项填充到**错误**的消息输入框（全局首个 .ag-clarify-input）
  B. 「新建交互」点击后不跳转（URL / 选中态不更新）
  C. 工具密度：切走再切回 session，工具区密度漂移

跑法：
  python3 repro_before.py <repo_root> <out_dir>
"""
from __future__ import annotations

import json
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


# 两条带快捷选项的 Agent 消息（模拟用户截图里的两组选项）
TWO_CLARIFY_MESSAGES = [
    {
        "role": "assistant",
        "content": "第一组：我先确认一下分镜政策。",
        "clarify": {
            "question": "先处理哪个？",
            "options": ["帮我压缩分镜政策到 3 条", "先完成一段 15 秒的开场试戏"],
            "hint": "",
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
            "hint": "",
        },
        "tools": [{"name": "search_assets", "ok": True, "ms": 30,
                   "arguments": "{\"q\":\"雨夜\"}", "result": "命中 12 条"}],
    },
]


def main() -> int:
    server = StudioServer()
    if not server.start():
        print("!! Studio 实例启动失败")
        return 2
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
            page.wait_for_timeout(600)

            # ---------- A. 快捷选项串填 ----------
            page.evaluate(
                """(msgs) => {
                  window.AgentUI.state.messages = msgs.map(m => Object.assign(
                    {content:'', result:{}, jobs:[], assets:[], prompt:null,
                     steps:[], tools:[], streaming:false, error:''}, m));
                  window.AgentUI.renderMessages();
                }""",
                TWO_CLARIFY_MESSAGES,
            )
            page.wait_for_timeout(400)
            before = page.eval_on_selector_all(
                ".ag-clarify-input",
                "els => els.map(e => e.value)",
            )
            # 点**第二条**消息的第三个选项
            opts = page.query_selector_all(".ag-clarify-opt")
            print(f"[A] 页面上的快捷选项数 = {len(opts)}")
            target_text = opts[len(opts) - 1].inner_text().strip()
            opts[len(opts) - 1].click()
            page.wait_for_timeout(300)
            after = page.eval_on_selector_all(
                ".ag-clarify-input",
                "els => els.map(e => e.value)",
            )
            a_result = {
                "option_count": len(opts),
                "clicked_option": target_text,
                "inputs_before": before,
                "inputs_after": after,
                "bug_reproduced": bool(after and after[0]),
            }
            print("[A] before =", before)
            print("[A] after  =", after)
            print("[A] 复现 =", a_result["bug_reproduced"])
            page.screenshot(path=str(OUT / "A-before-fix-crossfill.png"))
            (OUT / "A-crossfill.json").write_text(
                json.dumps(a_result, ensure_ascii=False, indent=2), encoding="utf-8")

            # ---------- B. 新建交互不跳转 ----------
            url_before = page.url
            hash_before = page.evaluate("() => location.hash")
            sid_before = page.evaluate("() => window.AgentUI.state.sessionId")
            page.click("#agMenu")
            page.wait_for_timeout(300)
            page.click("#agDrawerNew")
            page.wait_for_timeout(1200)
            url_after = page.url
            hash_after = page.evaluate("() => location.hash")
            sid_after = page.evaluate("() => window.AgentUI.state.sessionId")
            active_after = page.eval_on_selector_all(
                ".ag-session-item.ag-active", "els => els.map(e => e.dataset.sessionId)")
            b_result = {
                "url_before": url_before, "url_after": url_after,
                "hash_before": hash_before, "hash_after": hash_after,
                "sid_before": sid_before, "sid_after": sid_after,
                "active_session_after": active_after,
                "url_changed": url_before != url_after,
                "sid_changed": bool(sid_after) and sid_after != sid_before,
            }
            print("[B] url  ", url_before, "->", url_after)
            print("[B] sid  ", sid_before, "->", sid_after, "active =", active_after)
            page.screenshot(path=str(OUT / "B-before-fix-new-session.png"))
            (OUT / "B-new-session.json").write_text(
                json.dumps(b_result, ensure_ascii=False, indent=2), encoding="utf-8")

            # ---------- C. 工具密度漂移 ----------
            # 建第二个会话，塞同样内容，量工具区几何，再往返切 3 次。
            def measure():
                return page.evaluate("""() => {
                  const tools = Array.from(document.querySelectorAll('.ag-tools'));
                  const rows  = Array.from(document.querySelectorAll('.ag-tool'));
                  const list  = document.querySelector('.ag-tool-list');
                  const r = el => { if(!el) return null;
                    const b = el.getBoundingClientRect();
                    return {h: Math.round(b.height), w: Math.round(b.width),
                            top: Math.round(b.top)}; };
                  return {
                    toolsBlocks: tools.length,
                    toolRows: rows.length,
                    blockH: tools.map(t => Math.round(t.getBoundingClientRect().height)),
                    rowH: rows.map(t => Math.round(t.getBoundingClientRect().height)),
                    listH: r(list),
                    gap: list ? getComputedStyle(list).rowGap || getComputedStyle(list).gap : null,
                  };
                }""")

            # 用真实会话：新建 -> 发消息（模拟）不现实；直接用 restoreMessages 造数据
            page.evaluate("""() => {
              window.AgentUI.state.messages = [
                {role:'user', content:'hi'},
                {role:'assistant', content:'ok',
                 result:{status:'completed'}, jobs:[], assets:[], prompt:null,
                 steps:[], streaming:false, error:'',
                 tools:[{name:'list_modes', ok:true, ms:12, arguments:'{}', result:'t2v'}]},
              ];
              window.AgentUI.renderMessages();
            }""")
            page.wait_for_timeout(300)
            m0 = measure()
            print("[C] 首测 =", json.dumps(m0, ensure_ascii=False))

            # 模拟「切到别的 session 再切回来」：
            # 真实链路是 agent_sessions.loadSession -> restoreMessages -> renderMessages。
            # 这里走同一入口（restoreMessages），复现「重新渲染后布局漂移」。
            drift = []
            for i in range(3):
                # 切走：清空 + 渲染另一个会话（空）
                page.evaluate("""() => {
                  window.AgentUI.restoreMessages(
                    [{role:'user', content:'别的会话'}], {merge:false});
                }""")
                page.wait_for_timeout(200)
                # 切回：同样的消息重新恢复
                page.evaluate("""() => {
                  window.AgentUI.restoreMessages([
                    {role:'user', content:'hi'},
                    {role:'assistant', content:'ok',
                     result:{status:'completed'}, jobs:[], assets:[], prompt:null,
                     steps:[], streaming:false, error:'',
                     tools:[{name:'list_modes', ok:true, ms:12, arguments:'{}', result:'t2v'}]},
                  ], {merge:false});
                }""")
                page.wait_for_timeout(250)
                mi = measure()
                drift.append(mi)
                print(f"[C] 第 {i+1} 次切回 =", json.dumps(mi, ensure_ascii=False))

            c_result = {
                "baseline": m0,
                "after_roundtrips": drift,
                "stable": all(d == m0 for d in drift),
            }
            print("[C] 密度稳定 =", c_result["stable"])
            page.screenshot(path=str(OUT / "C-before-fix-density.png"))
            (OUT / "C-density.json").write_text(
                json.dumps(c_result, ensure_ascii=False, indent=2), encoding="utf-8")

            (OUT / "console.log").write_text(
                "\n".join(console), encoding="utf-8")
            (OUT / "pageerrors.log").write_text(
                "\n".join(page_errors), encoding="utf-8")
            browser.close()
    finally:
        server.stop()

    print("\n=== 证据目录:", OUT, "===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
