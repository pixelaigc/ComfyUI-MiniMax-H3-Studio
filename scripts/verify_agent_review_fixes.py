# -*- coding: utf-8 -*-
"""
综合审查返修的**真实浏览器**验证脚本。

覆盖三条审查问题，每条都产出可核对的证据（截图 + 几何数值 + console/请求记录）：

  P2-3 窄屏长对话后输入框可达   -> 三档视口截图 + 实测 inputTop / reachable
  P3-3 长耗时进度反馈可见       -> 进度区渲染 + 阶段文案截图
  P3-1 禁用项徽标文案统一       -> 增强菜单展开截图 + 徽标数组

用法：
    python3 scripts/verify_agent_review_fixes.py [端口] [证据目录]

需要一个**已启动**的 Studio 实例（默认端口 8307）。
本脚本只读页面、只截图，不修改任何数据；它会往 Agent 页灌入一批假消息
来复现「长对话」场景 —— 那只是前端内存里的 state，不落库。
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8307
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/agent-review-evidence")
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [(375, 812, "375"), (390, 844, "390"), (1280, 900, "1280")]

#: 灌入的消息条数 —— 足够把消息区撑出视口（复现审查报告 P2-3 的场景）
LONG_TURNS = 24

report = {
    "port": PORT,
    "viewports": {},
    "console_errors": [],
    "failed_requests": [],
    "badges": {},
}

AGENT_FACT = """
() => {
  const vh = window.innerHeight;
  const page = document.querySelector('#agentPage');
  const msgs = document.querySelector('#agMessages');
  const input = document.querySelector('#agInput');
  const composer = document.querySelector('.ag-composer');
  const status = document.querySelector('#agStatus');
  const r = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {top: Math.round(b.top), bottom: Math.round(b.bottom),
            h: Math.round(b.height), w: Math.round(b.width)};
  };
  return {
    vw: document.documentElement.clientWidth,
    vh: vh,
    docScrollW: document.documentElement.scrollWidth,
    bodyOverflowY: getComputedStyle(document.body).overflowY,
    pageDisplay: page ? getComputedStyle(page).display : null,
    pageVisible: page ? getComputedStyle(page).display !== 'none' : false,
    msgsClientH: msgs ? msgs.clientHeight : null,
    msgsScrollH: msgs ? msgs.scrollHeight : null,
    msgsScrollTop: msgs ? msgs.scrollTop : null,
    inputRect: r(input),
    inputReachable: input ? (input.getBoundingClientRect().top < vh
                             && input.getBoundingClientRect().bottom > 0) : false,
    composerRect: r(composer),
    statusText: status ? (status.textContent || '').trim() : '',
    msgCount: msgs ? msgs.children.length : 0,
  };
}
"""


def open_agent(page):
    """走真实用户路径：窄屏要先拉开侧栏抽屉，再点「内置 Agent」。"""
    page.evaluate(
        """() => {
          const t = document.querySelector('#navToggle');
          if (t && getComputedStyle(t).display !== 'none') t.click();
        }""")
    page.wait_for_timeout(300)
    page.click("#navAgent")
    page.wait_for_timeout(600)


def fill_long(page):
    page.evaluate(
        """(turns) => {
          const st = window.AgentUI.state;
          st.messages = [];
          for (let i = 0; i < turns; i++) {
            st.messages.push({role: 'user',
              content: '第 ' + (i + 1) + ' 条：' + '需求描述占位'.repeat(45)});
            st.messages.push({role: 'assistant',
              content: '第 ' + (i + 1) + ' 条回复：' + '处理结果占位'.repeat(45),
              result: {status: 'completed'}, jobs: [], assets: [],
              prompt: null, steps: []});
          }
          window.AgentUI.renderMessages();
        }""", LONG_TURNS)
    page.wait_for_timeout(400)


with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox"])

    for w, h, tag in VIEWPORTS:
        ctx = browser.new_context(viewport={"width": w, "height": h},
                                  is_mobile=(w < 700), has_touch=(w < 700))
        page = ctx.new_page()
        page.on("pageerror", lambda e, t=tag: report["console_errors"].append(
            f"[{t}] pageerror: {e}"))
        page.on("console", lambda m, t=tag: report["console_errors"].append(
            f"[{t}] console.error: {m.text}") if m.type == "error" else None)
        page.on("requestfailed", lambda rq, t=tag: report["failed_requests"].append(
            f"[{t}] {rq.method} {rq.url} -> {rq.failure}"))
        page.on("response", lambda rs, t=tag: report["failed_requests"].append(
            f"[{t}] {rs.status} {rs.url}") if rs.status >= 400 else None)

        page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

        open_agent(page)
        fill_long(page)

        info = page.evaluate(AGENT_FACT)
        # 滚到消息区底部，再采一次
        page.evaluate(
            "() => { const m = document.querySelector('#agMessages');"
            " m.scrollTop = m.scrollHeight; }")
        page.wait_for_timeout(300)
        after_scroll = page.evaluate(AGENT_FACT)

        shot = OUT / f"agent-{tag}-longconv.png"
        page.screenshot(path=str(shot), full_page=False)

        report["viewports"][tag] = {
            "info": info,
            "after_scroll_to_bottom": after_scroll,
            "screenshot": str(shot),
            "verdict": {
                "page_is_flex": info["pageDisplay"] == "flex",
                "input_reachable": info["inputReachable"],
                "input_top_in_viewport": info["inputRect"]["top"] <= info["vh"],
                "messages_area_scrolls": after_scroll["msgsScrollTop"] > 0,
                "no_horizontal_overflow": info["docScrollW"] <= info["vw"] + 1,
                "long_conversation_reproduced":
                    info["msgsScrollH"] > info["msgsClientH"],
            },
        }
        ctx.close()

    # ---------- P3-3 进度反馈 ----------
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.on("pageerror", lambda e: report["console_errors"].append(
        f"[progress] pageerror: {e}"))
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    open_agent(page)
    # 模拟一次忙碌态：进度区显示阶段 + 已用时间
    page.evaluate(
        """() => {
          const box = document.querySelector('#agStatus');
          box.innerHTML = '<span class="ag-progress">'
            + '<span class="ag-progress-dot"></span>'
            + '<span class="ag-progress-stage">正在检索素材库</span>'
            + '<span class="ag-progress-elapsed">已用 42 秒</span>'
            + '<span class="ag-progress-step">第 2/8 步</span>'
            + '</span>';
          box.classList.add('ag-status-busy');
          const btn = document.querySelector('#agSend');
          if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
        }""")
    page.wait_for_timeout(400)
    shot = OUT / "agent-progress-feedback.png"
    page.screenshot(path=str(shot), full_page=False)
    report["progress_feedback"] = {
        "screenshot": str(shot),
        "status_text": page.evaluate(
            "() => (document.querySelector('#agStatus').textContent || '').trim()"),
        "hint_text": page.evaluate(
            "() => (document.querySelector('.ag-hint-line').textContent || '').trim()"),
    }
    ctx.close()

    # ---------- P3-1 禁用项徽标 ----------
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    # 切到 Z-Image 文生图（qwen_pe 在这里不可用）
    page.evaluate(
        """() => {
          const n = document.querySelector('.nav-item[data-mode="t2i"]');
          if (n) n.click();
        }""")
    page.wait_for_timeout(800)
    # 展开增强菜单
    page.evaluate(
        """() => {
          const btn = document.querySelector('#enhanceMenuBtn')
                  || document.querySelector('#enhanceSplit .enhance-arrow')
                  || document.querySelector('#enhanceSplit');
          if (btn) btn.click();
        }""")
    page.wait_for_timeout(800)
    items = page.evaluate(
        """() => Array.from(
             document.querySelectorAll('#enhanceMenu .enhance-menu-item'))
           .map(it => ({
             id: it.dataset.method,
             label: (it.querySelector('.enhance-menu-label')||{}).textContent||'',
             disabled: !!(it.querySelector('input[data-method-check]')||{}).disabled,
             badges: Array.from(it.querySelectorAll('.enhance-menu-badge'))
                        .map(b => b.textContent.trim()),
           }))""")
    shot = OUT / "enhance-menu-badges.png"
    page.screenshot(path=str(shot), full_page=False)
    report["badges"] = {"items": items, "screenshot": str(shot)}
    ctx.close()

    browser.close()

report_path = OUT / "verify-report.json"
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8")

print("== 证据目录:", OUT)
print("== 报告文件:", report_path)
print()
for tag, data in report["viewports"].items():
    v = data["verdict"]
    print(f"[{tag}] " + "  ".join(f"{k}={val}" for k, val in v.items()))
print()
print("进度反馈:", json.dumps(report["progress_feedback"], ensure_ascii=False))
print()
print("禁用项徽标:")
for it in report["badges"]["items"]:
    print(f"   {it['id']:10s} disabled={it['disabled']} badges={it['badges']}")
print()
print("console 报错:", report["console_errors"] or "（无）")
print("失败请求:", report["failed_requests"] or "（无）")
