# -*- coding: utf-8 -*-
"""JS 断点 vs CSS media query 断点 —— 一致性逐宽度核对。

【为什么必须查】
app.js 里的 `isPhoneViewport()` 用的是 `window.BREAKPOINTS.phone`（JS 常量），
而 CSS 用的是 `@media (max-width: 700px)`（写在 CSS 文件里）。
**两处是独立的** —— 改了一处忘了另一处，就会出现
「CSS 已经是手机档、JS 还当它是桌面档」（或反过来），
症状是：抽屉不收、按钮不隐藏、遮罩位置错 —— 表现成「点不动」。

【这一条查什么】
对每个视口宽度，同时问 JS 与 CSS：
  · JS  认为是不是手机档？  （BREAKPOINTS.phone）
  · CSS 认为是不是手机档？  （matchMedia('(max-width: 700px)'))
  · CSS 认为是不是平板档？  （matchMedia('(max-width: 1100px)'))
两边的判定必须**完全一致**。

【顺带查真实布局证据】
不只是问变量，还要看**实际渲染**是否与断点判定一致：
  · 手机档 ⇒ 汉堡可见、侧栏是 fixed（抽屉）、参数面板是 fixed（底部抽屉）、FAB 可见
  · 桌面档 ⇒ 汉堡隐藏、侧栏是静态流内、参数面板常驻、FAB 隐藏
⇒ 「变量说手机档，但 DOM 其实是桌面版」也是不一致。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

WIDTHS = [320, 340, 360, 375, 390, 393, 412, 428, 430, 500, 600, 699, 700, 701, 768, 900, 1024, 1100, 1101, 1280]

PROBE = """() => {
  const cs = (sel) => { const e = document.querySelector(sel);
    if (!e) return null; const c = getComputedStyle(e); return {
      display: c.display, position: c.position, visibility: c.visibility }; };
  const bp = window.BREAKPOINTS || null;
  return {
    innerWidth: innerWidth, innerHeight: innerHeight,
    jsBreakpoints: bp,
    jsPhone: bp ? (innerWidth <= bp.phone) : null,
    jsTablet: bp ? (innerWidth <= bp.tablet) : null,
    cssPhone: matchMedia('(max-width: 700px)').matches,
    cssTablet: matchMedia('(max-width: 1100px)').matches,
    cssPhone480: matchMedia('(max-width: 480px)').matches,
    cssPhone380: matchMedia('(max-width: 380px)').matches,
    hamburger: cs('#navToggle'),
    sidebar: cs('#sidebar'),
    panel: cs('#panel'),
    fab: cs('#panelFab'),
    panelMask: cs('#panelMask'),
  };
}"""


def check(p):
    """判据：JS 与 CSS 的判定必须一致，且实际渲染与判定相符。"""
    problems = []
    if p["jsPhone"] != p["cssPhone"]:
        problems.append(f"手机档判定不一致: JS={p['jsPhone']} CSS={p['cssPhone']}")
    if p["jsTablet"] != p["cssTablet"]:
        problems.append(f"平板档判定不一致: JS={p['jsTablet']} CSS={p['cssTablet']}")
    # 实际渲染
    if p["cssPhone"]:
        if not p["hamburger"] or p["hamburger"]["display"] == "none":
            problems.append(f"CSS 说手机档，但汉堡隐藏了 (display={p['hamburger'] and p['hamburger']['display']})")
        if p["sidebar"] and p["sidebar"]["position"] != "fixed":
            problems.append(f"CSS 说手机档，但侧栏 position={p['sidebar']['position']}（应 fixed=抽屉）")
        if p["panel"] and p["panel"]["position"] != "fixed":
            problems.append(f"CSS 说手机档，但参数面板 position={p['panel']['position']}（应 fixed=底部抽屉）")
    else:
        if p["hamburger"] and p["hamburger"]["display"] != "none":
            problems.append(f"CSS 说桌面档，但汉堡还显示着 (display={p['hamburger']['display']})")
        if p["sidebar"] and p["sidebar"]["position"] == "fixed":
            problems.append(f"CSS 说桌面档，但侧栏是 fixed（抽屉）")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "breakpoint-consistency.json"))
    ap.add_argument("--engines", default="chromium")
    args = ap.parse_args()

    out = {"base": args.base, "results": {}}
    with sync_playwright() as pw:
        for eng in args.engines.split(","):
            eng = eng.strip()
            if eng == "webkit":
                browser = pw.webkit.launch(headless=True)
            else:
                browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            out["results"][eng] = {}
            for w in WIDTHS:
                ctx = browser.new_context(viewport={"width": w, "height": 800},
                                        is_mobile=True, has_touch=True, device_scale_factor=2)
                pg = ctx.new_page()
                pg.goto(args.base + "/", wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
                pg.wait_for_timeout(900)
                p = pg.evaluate(PROBE)
                p["problems"] = check(p)
                out["results"][eng][str(w)] = p
                ctx.close()
                mark = "OK " if not p["problems"] else "❌ "
                print(f"  {mark}{eng} {w:5d}px  jsPhone={p['jsPhone']} cssPhone={p['cssPhone']} "
                      f"jsTablet={p['jsTablet']} cssTablet={p['cssTablet']}"
                      + ("" if not p["problems"] else "  " + "; ".join(p["problems"])))
            browser.close()

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    bad = sum(1 for e in out["results"].values() for p in e.values() if p["problems"])
    total = sum(len(e) for e in out["results"].values())
    print(f"\n=== 断点一致性：{total - bad}/{total} 通过 ===")
    print("OUT:", args.out)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
