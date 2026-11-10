# -*- coding: utf-8 -*-
"""窄屏下拉菜单的**内容**是否被裁切的精确复验。

几何断言只量了菜单的 bounding box；还要确认：
  - 菜单自身没有 overflow 裁切（scrollHeight <= clientHeight）
  - **最后一项**的底边在视口内（不是「菜单框在视口内，但最后一项被裁掉」）
  - 每项的文字没被截断（scrollWidth <= clientWidth）
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8291/"

PANELS = [("t2i", "#enhanceMenuBtn", "#enhanceMenu"),
          ("t2a", "#musicEnhanceMenuBtn", "#musicEnhanceMenu")]
VIEWPORTS = [(375, 780), (390, 844), (1280, 900)]

PROBE = """(sel) => {
  const m = document.querySelector(sel);
  if (!m) return null;
  const r = m.getBoundingClientRect();
  const items = Array.from(m.querySelectorAll('.enhance-menu-item'));
  const last = items.length ? items[items.length - 1] : null;
  const lr = last ? last.getBoundingClientRect() : null;
  const cs = getComputedStyle(m);
  // 每一项的文字是否被截断（横向）
  const clippedText = items.map(it => {
    const lbl = it.querySelector('.enhance-menu-label');
    const hint = it.querySelector('.enhance-menu-hint');
    const bad = [];
    for (const e of [lbl, hint]) {
      if (!e) continue;
      if (e.scrollWidth > e.clientWidth + 1) bad.push((e.className||'') + ':' + e.textContent.trim().slice(0,30));
    }
    return bad;
  }).filter(x => x.length);
  return {
    menuTop: Math.round(r.top), menuBottom: Math.round(r.bottom),
    menuLeft: Math.round(r.left), menuRight: Math.round(r.right),
    innerH: window.innerHeight, innerW: window.innerWidth,
    scrollH: m.scrollHeight, clientH: m.clientHeight,
    scrollW: m.scrollWidth, clientW: m.clientWidth,
    overflowY: cs.overflowY, overflowX: cs.overflowX,
    verticallyClipped: m.scrollHeight > m.clientHeight + 1,
    horizontallyClipped: m.scrollWidth > m.clientWidth + 1,
    itemCount: items.length,
    lastItemBottom: lr ? Math.round(lr.bottom) : null,
    lastItemText: last ? (last.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80) : null,
    lastItemFullyVisible: lr ? (lr.top >= 0 && lr.bottom <= window.innerHeight) : null,
    clippedTextItems: clippedText,
    onScreen: (r.top >= 0 && r.bottom <= window.innerHeight
               && r.left >= 0 && r.right <= window.innerWidth + 1),
  };
}"""

out = {}
ok = True
with sync_playwright() as pw:
    b = pw.chromium.launch(args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof provState !== 'undefined' && provState.agent "
        "&& provState.agent.specs && provState.agent.specs.length > 0", timeout=60000)
    page.wait_for_timeout(1500)

    for mode, caret, menu in PANELS:
        for (w, h) in VIEWPORTS:
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(400)
            page.evaluate(
                "(m) => { const e = document.querySelector(`.nav-item[data-mode='${m}']`);"
                "if (e) e.click(); }", mode)
            page.wait_for_timeout(900)
            page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p && !p.classList.contains('open')) {
                const fab = document.querySelector('#panelFab');
                if (fab) fab.click();
              }
            }""")
            page.wait_for_timeout(600)
            page.evaluate("(s) => { const x = document.querySelector(s); if (x) x.click(); }", caret)
            page.wait_for_timeout(800)
            st = page.evaluate(PROBE, menu)
            key = "%s@%d" % (mode, w)
            out[key] = st
            if st:
                good = (st["onScreen"] and not st["verticallyClipped"]
                        and not st["horizontallyClipped"]
                        and st["lastItemFullyVisible"] is True
                        and not st["clippedTextItems"])
                ok = ok and good
                print("[%s] %s  menu(top=%d bottom=%d innerH=%d) scrollH=%d clientH=%d "
                      "lastItemBottom=%d clippedText=%d" % (
                          "PASS" if good else "FAIL", key, st["menuTop"], st["menuBottom"],
                          st["innerH"], st["scrollH"], st["clientH"], st["lastItemBottom"],
                          len(st["clippedTextItems"])))
                print("     lastItem = %s" % st["lastItemText"])
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
            page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p && p.classList.contains('open')) {
                p.classList.remove('open');
                const m = document.querySelector('#panelMask');
                if (m) m.classList.remove('open');
              }
            }""")
            page.wait_for_timeout(200)
    b.close()

print("\n=== 窄屏菜单内容裁切判定:", "PASS" if ok else "FAIL", "===")
sys.exit(0 if ok else 1)
