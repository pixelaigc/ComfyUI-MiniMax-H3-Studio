# -*- coding: utf-8 -*-
"""决定性证据：参数抽屉打开时，汉堡按钮用**真实命中测试**能不能点到。

【为什么必须用真实命中测试而不是 JS .click()】
`element.click()` 直接派发事件，**绕过命中测试** —— 它能点中任何元素，
哪怕那元素被一层遮罩盖住。所以上一轮矩阵里 S7 显示「抽屉打开了」，
那其实是 JS 直接 click 的结果，不等于用户手指点得到。

Playwright 的 `page.click()` 会先做 actionability 检查（可见 / 稳定 /
**命中测试**：元素中心点的 elementFromPoint 必须是它自己或它的后代）。
它报 "element is not receiving pointer events" 就是用户手指的真实体验。

判据：
  · 真实点击汉堡是否成功（打开侧栏）
  · 真实点击遮罩是否能关掉参数抽屉
  · 参数抽屉打开时，媒体库卡片是否够得着
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
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "decisive"))
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
        page.wait_for_timeout(1800)

        # --- 1. 打开参数抽屉（真实点击 FAB）---
        page.click("#panelFab", timeout=8000)
        page.wait_for_timeout(800)
        r["panel_open"] = page.evaluate(
            "() => document.querySelector('#panel').classList.contains('open')")
        r["fab_hidden_when_panel_open"] = page.evaluate(
            "() => { const f = document.querySelector('#panelFab');"
            " return !f || getComputedStyle(f).display === 'none'; }")
        page.screenshot(path=str(outdir / "D1-panel-open.png"))

        # --- 2. 真实点击汉堡（Playwright 命中测试）---
        try:
            page.click("#navToggle", timeout=4000)
            r["real_click_hamburger"] = "OK"
        except Exception as e:
            r["real_click_hamburger"] = "FAIL: " + str(e).split("\n")[0][:220]
        page.wait_for_timeout(600)
        r["sidebar_open_after_click"] = page.evaluate(
            "() => document.querySelector('#sidebar').classList.contains('open')")
        page.screenshot(path=str(outdir / "D2-after-real-hamburger-click.png"))

        # --- 3. 真实点击遮罩关闭参数抽屉 ---
        try:
            page.click("#panelMask", timeout=4000, force=False)
            r["real_click_mask"] = "OK"
        except Exception as e:
            r["real_click_mask"] = "FAIL: " + str(e).split("\n")[0][:220]
        page.wait_for_timeout(600)
        r["panel_open_after_mask_click"] = page.evaluate(
            "() => document.querySelector('#panel').classList.contains('open')")
        page.screenshot(path=str(outdir / "D3-after-mask-click.png"))

        # --- 4. 关掉抽屉后，真实点击媒体库卡片 ---
        page.evaluate("() => document.querySelector('#sidebarMask').click()")
        page.wait_for_timeout(500)
        try:
            card = page.query_selector("#jobsGrid .job-card[data-jid]")
            card.click(timeout=4000)
            r["real_click_card"] = "OK"
        except Exception as e:
            r["real_click_card"] = "FAIL: " + str(e).split("\n")[0][:220]
        page.screenshot(path=str(outdir / "D4-after-real-card-click.png"))

        # --- 5. 抽屉里滚到底看「收藏」是否可达 ---
        page.evaluate("() => { const s = document.querySelector('#navScroll');"
                      " s.scrollTop = s.scrollHeight; }")
        page.wait_for_timeout(500)
        r["favorites_in_view_after_scroll"] = page.evaluate(
            """() => { const el = document.querySelector('#navFavorites');
               if (!el) return null;
               const b = el.getBoundingClientRect();
               const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
               const t = document.elementFromPoint(x, y);
               return {rect: {top: Math.round(b.top), left: Math.round(b.left),
                              w: Math.round(b.width), h: Math.round(b.height)},
                       inViewport: b.top >= 0 && b.bottom <= innerHeight,
                       hitId: t ? t.id : null,
                       hitCls: t ? String(t.className||'').slice(0,60) : null,
                       hitIsSelf: t ? (t === el || el.contains(t)) : false}; }""")
        page.screenshot(path=str(outdir / "D5-drawer-scrolled-to-favorites.png"))

        browser.close()

    (outdir / "decisive.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
