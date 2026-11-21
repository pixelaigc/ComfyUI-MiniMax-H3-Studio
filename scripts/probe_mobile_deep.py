# -*- coding: utf-8 -*-
"""两项精确定位：
  (1) 动态视频类型项点完为什么抽屉不收（静态项会收）
  (2) 移动端**真·长按**能不能唤出右键菜单（CDP 派发真实 touch 事件）

【为什么 (1) 要单独测】
app.js 里「点导航项后自动收起侧栏」是
    $$(".nav-item").forEach(item => item.addEventListener("click", ...))
这一行在 initResponsive() 启动时执行 —— 它只给**当时已存在**的 .nav-item 绑。
video-nav.js 的模型项是 /api/video-categories 回来之后才动态创建的
（item.className = "nav-item nav-model-item"），**绑不上**。
所以静态项会收抽屉、动态项不会 —— 需要实测确认，不能推断。

【为什么 (2) 要用 CDP】
上一轮用 JS 合成 PointerEvent 测长按，contextmenu 没触发 ——
但那可能只是合成事件不被浏览器当成真实长按。
CDP 的 Input.dispatchTouchEvent 是**浏览器级真实触摸**，才是用户手指的等价物。

运行：
    python3 scripts/probe_mobile_deep.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/deep-before --width 375 --height 812
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
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "deep-before"))
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

        # ---------- (1) 绑定情况对比 ----------
        r["binding_audit"] = page.evaluate("""() => {
          // 找出所有 .nav-item，看哪些能触发「自动收抽屉」
          const items = Array.from(document.querySelectorAll('.nav-item'));
          return {
            total: items.length,
            static: items.filter(e => e.tagName === 'A').length,
            dynamicVideoModel: items.filter(e => e.classList.contains('nav-model-item')).length,
            dynamicOther: items.filter(e => !e.tagName === 'A' && !e.classList.contains('nav-model-item')).length,
            sample: items.slice(0, 6).map(e => ({tag: e.tagName, mode: e.dataset.mode || null,
                                                cls: e.className.slice(0, 40)})),
          };
        }""")

        # 实测：静态项 vs 动态项，点完抽屉是否收起
        def drawer_open():
            return page.evaluate(
                "() => document.querySelector('#sidebar').classList.contains('open')")

        page.evaluate("() => document.querySelector('#navToggle').click()")
        page.wait_for_timeout(500)
        page.evaluate("""() => { const el = document.querySelector(
            '#sidebar .nav-item[data-mode="t2i"]'); el.click(); }""")
        page.wait_for_timeout(800)
        r["static_item_closes_drawer"] = not drawer_open()

        page.evaluate("() => document.querySelector('#navToggle').click()")
        page.wait_for_timeout(500)
        page.evaluate("""() => { const el = document.querySelector(
            '#sidebar .nav-model-item[data-mode="t2v"]'); el.click(); }""")
        page.wait_for_timeout(800)
        r["dynamic_video_item_closes_drawer"] = not drawer_open()
        r["drawer_open_after_dynamic"] = drawer_open()
        page.screenshot(path=str(outdir / "D-drawer-stays-open-after-dynamic.png"))

        # 动态项点完后，能不能通过再点汉堡回到类型选择？
        page.evaluate("() => document.querySelector('#navToggle').click()")
        page.wait_for_timeout(500)
        r["can_reopen_after_dynamic"] = drawer_open()
        r["favorites_reachable_after_scroll"] = page.evaluate("""() => {
          const s = document.querySelector('#navScroll');
          s.scrollTop = s.scrollHeight;
          const el = document.querySelector('#navFavorites');
          const b = el.getBoundingClientRect();
          const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
          const t = document.elementFromPoint(x, y);
          return {inViewport: b.top >= 0 && b.bottom <= innerHeight,
                  hitIsSelf: t ? (t === el || el.contains(t)) : false,
                  rect: {top: Math.round(b.top), left: Math.round(b.left)}};
        }""")

        # ---------- (2) 真·长按（CDP 真实 touch）----------
        page.evaluate("() => document.querySelector('#sidebarMask').click()")
        page.wait_for_timeout(600)
        page.evaluate("""() => {
          window.__ctxFired = 0; window.__ctxTarget = null;
          document.addEventListener('contextmenu', (e) => {
            window.__ctxFired++;
            window.__ctxTarget = e.target ? (e.target.tagName + '.' +
              String(e.target.className || '').slice(0, 40)) : null;
          }, true);
        }""")
        card = page.query_selector("#jobsGrid .job-card[data-jid] .job-preview img")
        if card is None:
            card = page.query_selector("#jobsGrid .job-card[data-jid] .job-preview video")
        if card is None:
            card = page.query_selector("#jobsGrid .job-card[data-jid]")
        if card:
            box = card.bounding_box()
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            cdp = ctx.new_cdp_session(page)
            # 真·长按：touchStart -> 等 800ms -> touchEnd
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": x, "y": y, "id": 1}]})
            page.wait_for_timeout(800)
            cdp.send("Input.dispatchTouchEvent", {
                "type": "touchEnd", "touchPoints": []})
            page.wait_for_timeout(600)
            r["cdp_longpress"] = {"x": x, "y": y}
        r["contextmenu_fired_after_real_longpress"] = page.evaluate("() => window.__ctxFired")
        r["contextmenu_target"] = page.evaluate("() => window.__ctxTarget")
        r["ctx_menu_open_after_longpress"] = page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? m.classList.contains('open') : null; }")
        r["ctx_menu_items"] = page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? Array.from(m.querySelectorAll('button')).map(b => b.textContent.trim()) : []; }")
        page.screenshot(path=str(outdir / "D-after-real-longpress.png"))

        browser.close()

    (outdir / "deep.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
