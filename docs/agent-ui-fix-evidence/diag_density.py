# -*- coding: utf-8 -*-
"""
工具密度诊断 —— 对比「实时渲染路径」与「会话恢复渲染路径」。

假设（待验证）：
  实时路径（流式 tool_call）：一个 assistant 消息里的 N 个工具
    -> 渲染成**一个** .ag-tools 容器 + N 个 .ag-tool 行（紧凑）
  恢复路径（agent_sessions.messagesFromServer）：每条 role=tool 消息
    -> 各自变成**一个独立的 assistant 气泡**，每个气泡各带一个 .ag-tools 容器
    -> N 个容器 = N 份 margin/padding/border -> 密度大幅变化

跑法：python3 diag_density.py <repo_root>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3studio-agent-ui")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from playwright.sync_api import sync_playwright   # noqa: E402
from test_frontend_defaults import StudioServer  # noqa: E402

MEASURE = """() => {
  const blocks = Array.from(document.querySelectorAll('.ag-tools'));
  const rows = Array.from(document.querySelectorAll('.ag-tool'));
  const msgBodies = Array.from(document.querySelectorAll('.ag-msg-bot'));
  const blocksBox = document.querySelectorAll('.ag-tools');
  let totalToolAreaH = 0;
  blocks.forEach(b => { totalToolAreaH += b.getBoundingClientRect().height; });
  const outer = blocks[0];
  const outerStyle = outer ? getComputedStyle(outer) : null;
  return {
    toolBlocks: blocks.length,
    toolRows: rows.length,
    botMessages: msgBodies.length,
    totalToolAreaH: Math.round(totalToolAreaH),
    blockH: blocks.map(b => Math.round(b.getBoundingClientRect().height)),
    rowH: rows.map(x => Math.round(x.getBoundingClientRect().height)),
    rowOpen: rows.map(x => x.open),
    blockOpen: blocks.map(b => b.open),
    blockMarginTop: outerStyle ? outerStyle.marginTop : null,
    blockPadding: outerStyle ? outerStyle.padding : null,
    detailKeys: blocks.map(b => b.getAttribute('data-detail-key')),
    rowKeys: rows.map(x => x.getAttribute('data-detail-key')),
  };
}"""


def main() -> int:
    server = StudioServer()
    if not server.start():
        print("!! Studio 实例启动失败")
        return 2
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))
            page.goto(f"http://127.0.0.1:{server.port}/", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            page.click("#navAgent")
            page.wait_for_timeout(600)

            # ---------- 实时路径（流式 tool_call） ----------
            page.evaluate("""() => {
              window.AgentUI.state.messages = [];
              const msg = {role:'assistant', content:'实时', result:{}, jobs:[],
                           assets:[], prompt:null, steps:[], tools:[],
                           streaming:false, error:''};
              window.AgentUI.state.messages.push(msg);
              // 两个工具调用（模拟 SSE tool_call 事件）
              window.AgentUI._applyStreamEvent(msg, 'tool_call',
                {name:'list_modes', arguments:'{}', call_id:'c1'});
              window.AgentUI._applyStreamEvent(msg, 'tool_call',
                {name:'search_assets', arguments:'{\"q\":\"雨夜\"}', call_id:'c2'});
              window.AgentUI.renderMessages();
            }""")
            page.wait_for_timeout(300)
            live = page.evaluate(MEASURE)
            print("[LIVE  ]", json.dumps(live, ensure_ascii=False, indent=2))

            # ---------- 恢复路径（会话层还原） ----------
            page.evaluate("""() => {
              // agent_sessions.messagesFromServer 的等价形状：
              // 每条 role=tool 消息 -> 独立的 assistant 气泡，各带一个 tools 数组
              window.AgentUI.restoreMessages([
                {role:'user', content:'hi'},
                {role:'assistant', content:'已完成一轮。', result:{status:'completed'},
                 jobs:[], assets:[], prompt:null, steps:[], tools:[], streaming:false, error:''},
                {role:'assistant', content:'', tools:[
                  {name:'list_modes', ok:true, ms:null, arguments:null, result:'list_modes: ok'}]},
                {role:'assistant', content:'', tools:[
                  {name:'search_assets', ok:true, ms:null, arguments:null, result:'search_assets: ok'}]},
              ], {merge:false});
            }""")
            page.wait_for_timeout(300)
            restored = page.evaluate(MEASURE)
            print("[RESTORE]", json.dumps(restored, ensure_ascii=False, indent=2))

            print("\n=== 差异 ===")
            print("live   toolBlocks =", live["toolBlocks"], " botMessages =", live["botMessages"],
                  " totalToolAreaH =", live["totalToolAreaH"])
            print("restore toolBlocks =", restored["toolBlocks"], " botMessages =", restored["botMessages"],
                  " totalToolAreaH =", restored["totalToolAreaH"])
            print("密度一致 =", live["toolBlocks"] == restored["toolBlocks"]
                  and live["totalToolAreaH"] == restored["totalToolAreaH"])

            # ---------- 跨会话 details 态泄漏 ----------
            page.evaluate("""() => {
              window.AgentUI.restoreMessages([
                {role:'user', content:'hi'},
                {role:'assistant', content:'A', tools:[
                  {name:'list_modes', ok:true, ms:12, arguments:'{}', result:'ok'}]},
              ], {merge:false});
            }""")
            page.wait_for_timeout(250)
            # 展开 A 的第一个工具行（模拟用户点开看细节）
            page.evaluate("""() => {
              const d = document.querySelector('.ag-tool');
              if (d) d.open = true;
            }""")
            a_state = page.evaluate("""() => ({
              rowOpen: Array.from(document.querySelectorAll('.ag-tool')).map(x => x.open),
              keys: Array.from(document.querySelectorAll('.ag-tool')).map(x => x.getAttribute('data-detail-key')),
            })""")
            print("\n[A 的工具行折叠态]", json.dumps(a_state, ensure_ascii=False))

            # 切到「另一个会话」：同样 1 个工具，但 key 也是 m0-tool-0
            page.evaluate("""() => {
              window.AgentUI.restoreMessages([
                {role:'user', content:'别的会话'},
                {role:'assistant', content:'B', tools:[
                  {name:'other_tool', ok:true, ms:99, arguments:'{}', result:'ok'}]},
              ], {merge:false});
            }""")
            page.wait_for_timeout(250)
            b_state = page.evaluate("""() => ({
              rowOpen: Array.from(document.querySelectorAll('.ag-tool')).map(x => x.open),
              keys: Array.from(document.querySelectorAll('.ag-tool')).map(x => x.getAttribute('data-detail-key')),
            })""")
            print("[切到 B 后 ]", json.dumps(b_state, ensure_ascii=False))
            leak = b_state["rowOpen"] != [False]
            print("跨会话折叠态泄漏 =", leak)

            print("\npageerrors =", errs)
            browser.close()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
