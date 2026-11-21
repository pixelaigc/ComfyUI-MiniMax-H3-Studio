# -*- coding: utf-8 -*-
"""验收清单 2 的补充：媒体库与「收藏」栏入口的可见性 / 滚动距离。

【为什么要单独做】
验收 2 要求「任何状态下都能看到生成媒体库（含左侧『收藏』栏入口），
不被抽屉、遮罩、FAB 或顶栏遮挡或截断；**需要滚动才能到达的项要如实记录
滚动距离与是否在视口内**」。

⇒ 「在视口内」不等于「在抽屉里」——
侧栏抽屉里的「收藏」入口在 375×812 这种矮视口下**必然要滚动**
（#navScroll 是独立滚动容器）。这不是缺陷，但必须**如实记录**：
滚多远、滚完之后在不在视口内、滚完之后能不能真点到。

【测法】
  1) 真实 tap 汉堡打开抽屉
  2) 记录 #navFavorites 的初始位置 / 是否在视口内 / 命中测试
  3) 记录到它需要的滚动距离（scrollTop 增量）
  4) 滚过去之后再测：是否在视口内 / 命中测试 / 真实 tap 能否点中
  5) 点进去后：收藏视图是否显示收藏内容
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

FAV_PROBE = """() => {
  const el = document.querySelector('#navFavorites');
  if (!el) return {present: false};
  const b = el.getBoundingClientRect();
  const inView = b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth;
  let hit = null, hitIsSelf = false;
  if (inView) {
    const x = Math.round(Math.min(innerWidth-1, Math.max(0, b.left + b.width/2)));
    const y = Math.round(Math.min(innerHeight-1, Math.max(0, b.top + b.height/2)));
    const t = document.elementFromPoint(x, y);
    hit = t ? {tag: t.tagName, id: t.id || null,
               cls: String(t.className||'').slice(0,60),
               text: String(t.textContent||'').trim().slice(0,30)} : null;
    hitIsSelf = t ? (t === el || el.contains(t)) : false;
  }
  const sc = document.querySelector('#navScroll');
  return {
    present: true,
    text: String(el.textContent||'').trim().slice(0,30),
    rect: {top: Math.round(b.top), left: Math.round(b.left),
           w: Math.round(b.width), h: Math.round(b.height),
           bottom: Math.round(b.bottom)},
    inViewport: inView,
    hit: hit, hitIsSelf: hitIsSelf,
    navScroll: sc ? {scrollTop: Math.round(sc.scrollTop),
                     scrollHeight: Math.round(sc.scrollHeight),
                     clientHeight: Math.round(sc.clientHeight),
                     maxScroll: Math.round(sc.scrollHeight - sc.clientHeight)} : null,
    sidebarOpen: document.querySelector('#sidebar').classList.contains('open'),
  };
}"""

SCROLL_TO = """(sel) => {
  const el = document.querySelector(sel);
  const sc = document.querySelector('#navScroll');
  if (!el || !sc) return {ok: false};
  const before = sc.scrollTop;
  const er = el.getBoundingClientRect();
  const sr = sc.getBoundingClientRect();
  // 需要滚的距离：让元素底边进入滚动容器可视底边
  const need = Math.max(0, (er.bottom - sr.bottom) + 8);
  sc.scrollTop = before + need;
  return {ok: true, before: Math.round(before),
          scrolledBy: Math.round(sc.scrollTop - before),
          needed: Math.round(need)};
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fav-entry-reach"))
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

        # 1) 打开抽屉
        try:
            page.tap("#navToggle", timeout=8000)
            r["tap_hamburger"] = "OK"
        except Exception as e:
            r["tap_hamburger"] = "FAIL: " + str(e).split("\n")[0][:180]
        page.wait_for_timeout(900)

        # 2) 初始状态
        r["before_scroll"] = page.evaluate(FAV_PROBE)
        page.screenshot(path=str(outdir / f"fav-{args.width}-1-drawer-open.png"))

        # 3) 滚到它
        r["scroll"] = page.evaluate(SCROLL_TO, "#navFavorites")
        page.wait_for_timeout(600)
        r["after_scroll"] = page.evaluate(FAV_PROBE)
        page.screenshot(path=str(outdir / f"fav-{args.width}-2-scrolled.png"))

        # 4) 真实 tap 点它
        try:
            page.tap("#navFavorites", timeout=8000)
            r["tap_favorites"] = "OK"
        except Exception as e:
            r["tap_favorites"] = "FAIL: " + str(e).split("\n")[0][:200]
        page.wait_for_timeout(1200)

        # 5) 收藏视图
        r["favorites_view"] = page.evaluate(
            """() => ({
                sidebarOpen: document.querySelector('#sidebar').classList.contains('open'),
                favTabActive: !!document.querySelector('.jobs-tabs button[data-st="favorites"].active'),
                cards: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
                favBadges: document.querySelectorAll('#jobsGrid .job-fav-badge').length,
                favToolsVisible: (() => { const t = document.querySelector('#favTools');
                   if (!t) return false;
                   if (t.hasAttribute('hidden')) return false;
                   const b = t.getBoundingClientRect();
                   return b.width > 0 && b.height > 0; })(),
                pageSize: (document.querySelector('#pageSizeSelect')||{}).value || null,
            })""")
        page.screenshot(path=str(outdir / f"fav-{args.width}-3-favorites-view.png"))

        # 6) 收藏视图里能开卡片详情吗
        try:
            page.tap("#jobsGrid .job-card[data-jid]", timeout=8000)
            page.wait_for_timeout(900)
            r["detail_in_fav_view"] = page.evaluate(
                """() => { const m = document.querySelector('#detailModal');
                   if (!m || m.hasAttribute('hidden')) return false;
                   const b = m.getBoundingClientRect();
                   return b.width > 0 && b.height > 0; }""")
            page.screenshot(path=str(outdir / f"fav-{args.width}-4-detail.png"))
            page.tap("#detailModalClose", timeout=8000)
        except Exception as e:
            r["detail_in_fav_view"] = "FAIL: " + str(e).split("\n")[0][:180]

        # 7) 收藏视图里能切状态标签吗
        try:
            page.tap('.jobs-tabs button[data-st="all"]', timeout=8000)
            page.wait_for_timeout(900)
            r["tab_all_in_fav_view"] = page.evaluate(
                """() => !!document.querySelector('.jobs-tabs button[data-st="all"].active')""")
        except Exception as e:
            r["tab_all_in_fav_view"] = "FAIL: " + str(e).split("\n")[0][:180]

        browser.close()

    (outdir / f"fav-entry-{args.width}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
