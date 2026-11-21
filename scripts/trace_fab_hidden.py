# -*- coding: utf-8 -*-
"""追踪 FAB 的 hidden 属性在「开抽屉」过程中到底被谁改了。

【为什么需要】改完 updateResponsiveChrome 后实测 overlap 仍是 25×44。
可能的成因有三类，必须区分：
  (a) hidden 属性被设了，但 CSS 让它仍然显示（display:flex 覆盖 [hidden]）
  (b) hidden 属性根本没被设（顺序问题：谁在之后又把它放出来）
  (c) 设置的时机早于 .open 类被加上

做法：在页面里挂 MutationObserver 记录 fab.hidden 的每次变化，
再逐帧读最终值。

运行：
    python3 scripts/trace_fab_hidden.py --base http://127.0.0.1:8917 \
        --out evidence/mobile-nav/fab-trace
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fab-trace"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        logs = []
        page.on("console", lambda m: logs.append("%s: %s" % (m.type, m.text)))
        page.on("pageerror", lambda e: logs.append("pageerror: " + str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(1800)

        # 装 MutationObserver
        page.evaluate("""() => {
          window.__fabLog = [];
          const fab = document.querySelector('#panelFab');
          window.__fabLog.push({t: 'init', hidden: fab.hidden,
                                display: getComputedStyle(fab).display});
          new MutationObserver(muts => {
            muts.forEach(m => {
              window.__fabLog.push({t: 'mutate', hidden: fab.hidden,
                                    display: getComputedStyle(fab).display,
                                    attr: m.attributeName,
                                    stack: (new Error()).stack.split('\\n').slice(1,5).join(' | ')});
            });
          }).observe(fab, {attributes: true, attributeFilter: ['hidden', 'style', 'class']});
          // 记录 sidebar .open 的变化
          const sb = document.querySelector('#sidebar');
          new MutationObserver(muts => {
            muts.forEach(m => {
              window.__fabLog.push({t: 'sidebar', open: sb.classList.contains('open'),
                                    hidden: fab.hidden,
                                    display: getComputedStyle(fab).display});
            });
          }).observe(sb, {attributes: true, attributeFilter: ['class']});
        }""")

        page.click("#navToggle", timeout=6000)
        page.wait_for_timeout(1200)

        state = page.evaluate("""() => {
          const fab = document.querySelector('#panelFab');
          const sb = document.querySelector('#sidebar');
          const cs = getComputedStyle(fab);
          const r = fab.getBoundingClientRect();
          return {
            fabHiddenAttr: fab.hidden,
            fabDisplay: cs.display,
            fabVisibility: cs.visibility,
            fabOpacity: cs.opacity,
            fabRect: {top: Math.round(r.top), left: Math.round(r.left),
                      w: Math.round(r.width), h: Math.round(r.height)},
            sidebarOpen: sb.classList.contains('open'),
            log: window.__fabLog,
          };
        }""")
        page.screenshot(path=str(outdir / "FT1-after-hamburger.png"))
        browser.close()

    (outdir / "fab-trace.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print("fabHiddenAttr:", state["fabHiddenAttr"])
    print("fabDisplay   :", state["fabDisplay"])
    print("fabRect      :", state["fabRect"])
    print("sidebarOpen  :", state["sidebarOpen"])
    print("--- 变更日志 ---")
    for e in state["log"]:
        print(" ", json.dumps(e, ensure_ascii=False)[:260])
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
