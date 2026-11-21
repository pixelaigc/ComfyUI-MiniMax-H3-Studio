# -*- coding: utf-8 -*-
"""移动端「长按 → 右键菜单」是否可行（CDP 真实触摸）。

【为什么重测】上一轮探针把长按点打在了 y=1247（视口外，812 高度），
那个结果无效。本轮先把卡片滚进视口，再发**浏览器级真实触摸**
（CDP Input.dispatchTouchEvent），按 800ms 不松手。

判据：
  · contextmenu 是否触发
  · #mediaContext 是否打开
  · 菜单里有没有「添加到收藏」

运行：
    python3 scripts/probe_longpress.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/longpress-before --width 375 --height 812
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
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "longpress-before"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    r = {"base": base, "viewport": [args.width, args.height]}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(2000)

        page.evaluate("""() => {
          window.__ctxFired = 0; window.__ctxTarget = null;
          document.addEventListener('contextmenu', (e) => {
            window.__ctxFired++;
            window.__ctxTarget = e.target ? (e.target.tagName + '|' +
              String(e.target.className || '').slice(0, 40)) : null;
          }, true);
        }""")

        # 把第一张卡的预览图滚进视口
        page.evaluate("""() => {
          const el = document.querySelector('#jobsGrid .job-card[data-jid] .job-preview img')
            || document.querySelector('#jobsGrid .job-card[data-jid] .job-preview video');
          if (el) el.scrollIntoView({block: 'center'});
        }""")
        page.wait_for_timeout(600)

        target = page.query_selector("#jobsGrid .job-card[data-jid] .job-preview img")
        if target is None:
            target = page.query_selector("#jobsGrid .job-card[data-jid] .job-preview video")
        if target is None:
            target = page.query_selector("#jobsGrid .job-card[data-jid]")
        box = target.bounding_box()
        r["target_box"] = box
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        r["target_point"] = {"x": x, "y": y, "inViewport": 0 <= y <= args.height}

        cdp = ctx.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
        page.wait_for_timeout(850)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(700)

        r["contextmenu_fired"] = page.evaluate("() => window.__ctxFired")
        r["contextmenu_target"] = page.evaluate("() => window.__ctxTarget")
        r["ctx_menu_open"] = page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? m.classList.contains('open') : null; }")
        r["ctx_menu_items"] = page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? Array.from(m.querySelectorAll('button')).map(b => b.textContent.trim()) : []; }")
        page.screenshot(path=str(outdir / "L1-after-longpress.png"))

        # 对照：普通轻点会不会也触发（应该不会）
        page.evaluate("() => { const m = document.querySelector('#mediaContext');"
                      " if (m) m.classList.remove('open'); window.__ctxFired = 0; }")
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 2}]})
        page.wait_for_timeout(80)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(500)
        r["contextmenu_fired_on_tap"] = page.evaluate("() => window.__ctxFired")

        browser.close()

    (outdir / "longpress.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
