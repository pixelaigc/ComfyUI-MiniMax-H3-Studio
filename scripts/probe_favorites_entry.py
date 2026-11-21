# -*- coding: utf-8 -*-
"""专项定位：#navFavorites（左侧「收藏」入口）在移动端抽屉里到底在哪。

【为什么要专项】矩阵探针只报 "off-viewport"，没说是**横向**出界还是
**纵向**出界，也没说它是不是真的在 #sidebar 里。
三种可能：
  (a) 它在 #sidebar 之外（结构错位）
  (b) 它在 #sidebar 里但横向被 translateX 挪出去了（CSS 错）
  (c) 它在 #sidebar 里、在滚动区下方（纵向要滚）—— 这是**正常**的

判据必须区分这三种，否则会为了修 (c) 去破坏 (c) 之外的正常行为。

运行：
    python3 scripts/probe_favorites_entry.py --base http://127.0.0.1:8917 \
        --out evidence/mobile-nav/fav-entry-after --width 375 --height 812
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

JS = r"""
() => {
  const el = document.querySelector('#navFavorites');
  if (!el) return {found: false};
  const sb = document.querySelector('#sidebar');
  const scroll = document.querySelector('#navScroll');
  const inSidebar = !!sb && sb.contains(el);
  const r = el.getBoundingClientRect();
  const sr = sb ? sb.getBoundingClientRect() : null;
  const scr = scroll ? scroll.getBoundingClientRect() : null;
  // 命中测试（在滚动区可见部分内）
  let hit = null;
  if (r.width && r.height) {
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    const inVP = x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight;
    const t = inVP ? document.elementFromPoint(x, y) : null;
    hit = {x, y, inViewport: inVP,
           hitId: t ? t.id : null,
           hitCls: t ? String(t.className || '').slice(0, 50) : null,
           hitText: t ? (t.textContent || '').trim().slice(0, 20) : null,
           isSelf: t ? (t === el || el.contains(t)) : false};
  }
  return {
    found: true,
    inSidebar: inSidebar,
    parentChain: (() => {
      const chain = [];
      let n = el;
      while (n && chain.length < 6) {
        chain.push(n.tagName + (n.id ? '#' + n.id : '') +
                   (n.className ? '.' + String(n.className).split(' ')[0] : ''));
        n = n.parentElement;
      }
      return chain;
    })(),
    rect: {top: Math.round(r.top), left: Math.round(r.left),
           w: Math.round(r.width), h: Math.round(r.height),
           bottom: Math.round(r.bottom), right: Math.round(r.right)},
    sidebarRect: sr ? {top: Math.round(sr.top), left: Math.round(sr.left),
                        w: Math.round(sr.width), h: Math.round(sr.height)} : null,
    navScrollRect: scr ? {top: Math.round(scr.top), left: Math.round(scr.left),
                           w: Math.round(scr.width), h: Math.round(scr.height),
                           scrollTop: scroll.scrollTop,
                           scrollHeight: scroll.scrollHeight,
                           clientHeight: scroll.clientHeight} : null,
    sidebarTransform: sb ? getComputedStyle(sb).transform : null,
    sidebarOpen: sb ? sb.classList.contains('open') : null,
    hit: hit,
    vw: innerWidth, vh: innerHeight,
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fav-entry-after"))
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
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(1800)

        r = {}
        r["drawer_closed"] = page.evaluate(JS)
        page.click("#navToggle", timeout=6000)
        page.wait_for_timeout(700)
        r["drawer_open"] = page.evaluate(JS)
        page.screenshot(path=str(outdir / "FE1-drawer-open.png"))

        # 滚动到底再测
        page.evaluate("() => { const s = document.querySelector('#navScroll');"
                      " s.scrollTop = s.scrollHeight; }")
        page.wait_for_timeout(600)
        r["drawer_open_scrolled_bottom"] = page.evaluate(JS)
        page.screenshot(path=str(outdir / "FE2-scrolled-bottom.png"))

        # 真实点击
        ok, err = False, None
        try:
            page.click("#navFavorites", timeout=5000)
            ok = True
        except Exception as e:
            err = str(e).split("\n")[0][:200]
        r["real_click_ok"] = ok
        r["real_click_error"] = err
        r["fav_tab_active"] = page.evaluate(
            "() => { const b = document.querySelector(\".jobs-tabs button[data-st='favorites']\");"
            " return b ? b.classList.contains('active') : null; }")
        page.screenshot(path=str(outdir / "FE3-after-fav-click.png"))

        browser.close()

    (outdir / "fav-entry.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in r.items():
        if isinstance(v, dict):
            print("=" * 60)
            print(k)
            print("  inSidebar:", v.get("inSidebar"), "| chain:", v.get("parentChain"))
            print("  rect:", v.get("rect"))
            print("  sidebarRect:", v.get("sidebarRect"))
            print("  navScroll:", v.get("navScrollRect"))
            print("  sidebarOpen:", v.get("sidebarOpen"), "| transform:", v.get("sidebarTransform"))
            print("  hit:", json.dumps(v.get("hit"), ensure_ascii=False))
        else:
            print(k, "=", v)
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
