# -*- coding: utf-8 -*-
"""精确定位「顶栏汉堡 vs 抽屉导航项」的重叠。

【为什么要单独探】
上一轮全量复测在 375px/390px 的「文生音乐」类型上测到
#navToggle 与 #sidebar .nav-item 有 22×3.5 / 22×6.5 的重叠。
面积很小（77 / 143 px²），但验收判据是「遮挡面积 = 0」。

【必须分清两种可能】
  (a) 真缺陷：抽屉里的导航项真的伸到了顶栏底下，汉堡压着它
      ⇒ 手指点那个导航项时命中的是汉堡 ⇒ 点不动。
  (b) 量测假象：我用 JS 直接 add('open') 强制打开抽屉，
      而 CSS 有 `transition: transform .22s ease` ——
      强制加类后抽屉还在从 translateX(-100%) 往左滑的动画中间，
      量到的是**动画中途**的位置。
      ⇒ 必须等动画结束后再量，且必须用**真实 tap** 打开抽屉。

本探针两种都量，给出对照。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402


MEASURE_JS = """() => {
  const out = {viewport: {w: innerWidth, h: innerHeight}};
  const tog = document.querySelector('#navToggle');
  const sb  = document.querySelector('#sidebar');
  const tb  = document.querySelector('.topbar');
  if (!tog || !sb) return {error: 'missing'};
  const tbR = tb.getBoundingClientRect();
  const tR  = tog.getBoundingClientRect();
  const sR  = sb.getBoundingClientRect();
  out.topbar = {top: Math.round(tbR.top), h: Math.round(tbR.height),
                 bottom: Math.round(tbR.bottom)};
  out.navToggle = {top: Math.round(tR.top), left: Math.round(tR.left),
                   w: Math.round(tR.width), h: Math.round(tR.height),
                   bottom: Math.round(tR.bottom)};
  out.sidebar = {top: Math.round(sR.top), left: Math.round(sR.left),
                 w: Math.round(sR.width), h: Math.round(sR.height)};
  out.sidebarOpen = sb.classList.contains('open');
  out.sidebarTransform = getComputedStyle(sb).transform;
  out.topbarZ = getComputedStyle(tb).zIndex;
  out.navToggleZ = getComputedStyle(tog).zIndex;

  // 抽屉里所有与 navToggle 相交的导航项
  const hits = [];
  for (const el of sb.querySelectorAll('.nav-item, .nav-group, .nav-drawer-head')) {
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) continue;
    const w = Math.max(0, Math.min(b.right, tR.right) - Math.max(b.left, tR.left));
    const h = Math.max(0, Math.min(b.bottom, tR.bottom) - Math.max(b.top, tR.top));
    if (w > 0 && h > 0) {
      const cx = Math.round(Math.min(innerWidth-1, Math.max(0, b.left + b.width/2)));
      const cy = Math.round(Math.min(innerHeight-1, Math.max(0, b.top + b.height/2)));
      const t = document.elementFromPoint(cx, cy);
      hits.push({text: String(el.textContent||'').trim().slice(0,34),
                 cls: String(el.className||'').slice(0,46),
                 rect: {top: Math.round(b.top), left: Math.round(b.left),
                        w: Math.round(b.width), h: Math.round(b.height)},
                 overlap: {w: Math.round(w), h: Math.round(h),
                           area: Math.round(w*h)},
                 hitIsSelf: t ? (t === el || el.contains(t)) : false,
                 hitTag: t ? t.tagName : null,
                 hitId: t ? t.id : null,
                 hitCls: t ? String(t.className||'').slice(0,46) : null});
    }
  }
  out.overlaps = hits;
  out.overlapArea = hits.reduce((s,h)=>s+h.overlap.area, 0);

  // 抽屉自身是否伸到顶栏之下（top < 44 说明盖住了顶栏）
  out.drawerCoversTopbar = sR.top < (out.topbar.h || 44);
  return out;
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "navtoggle-overlap"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    r = {"base": base, "viewport": [args.width, args.height]}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
        page.wait_for_timeout(2500)

        # --- 方式 1：JS 强制加类（上一轮复测用的方式，可能量到动画中途）---
        page.evaluate("() => document.querySelector('#sidebar').classList.add('open')")
        page.wait_for_timeout(50)   # 动画中途
        r["forced_immediate"] = page.evaluate(MEASURE_JS)
        page.wait_for_timeout(600)  # 动画结束
        r["forced_settled"] = page.evaluate(MEASURE_JS)
        page.screenshot(path=str(outdir / f"forced-{args.width}.png"))

        # 复位
        page.evaluate("() => document.querySelector('#sidebar').classList.remove('open')")
        page.wait_for_timeout(500)

        # --- 方式 2：真实 tap 汉堡打开（用户路径），等动画结束再量 ---
        try:
            page.tap("#navToggle", timeout=8000)
            r["real_tap_hamburger"] = "OK"
        except Exception as e:
            r["real_tap_hamburger"] = "FAIL: " + str(e).split("\n")[0][:200]
        page.wait_for_timeout(900)
        r["real_settled"] = page.evaluate(MEASURE_JS)
        page.screenshot(path=str(outdir / f"real-{args.width}.png"))

        # 滚动抽屉到各个位置再量（导航项在滚动容器里，位置会变）
        r["scrolled"] = []
        for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
            page.evaluate(
                """(f) => { const s = document.querySelector('#navScroll');
                   if (s) s.scrollTop = (s.scrollHeight - s.clientHeight) * f; }""", frac)
            page.wait_for_timeout(350)
            m = page.evaluate(MEASURE_JS)
            m["scrollFrac"] = frac
            r["scrolled"].append(m)
        page.screenshot(path=str(outdir / f"real-scrolled-bottom-{args.width}.png"))

        browser.close()

    (outdir / f"navtoggle-{args.width}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:6000])
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
