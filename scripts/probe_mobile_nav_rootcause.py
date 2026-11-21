# -*- coding: utf-8 -*-
"""移动端导航死路 —— 逐项根因探针。

【为什么不用截图猜】用户反馈是「选择某个生成类型后，就无法回到生成类型选择，
也无法看到生成媒体库」。这句话可以对应到至少三种不同的机制：
  (a) 抽屉没关 -> 盖住媒体库
  (b) 抽屉关了但没有再打开的入口 -> 真的回不去
  (c) 遮罩层盖住顶栏 -> 汉堡按钮点不动 -> 回不去也看不到媒体库
必须用 elementFromPoint 判定「用户手指点到那儿命中的是谁」，
再用「媒体库卡片中心点命中谁」判定「媒体库是否真的够得着」。

【测什么】
  P1 静态 nav-item（t2i）点完后：抽屉开/关、汉堡命中、媒体库卡片命中
  P2 动态 nav-model-item（t2v）点完后：同上（这两条路径的绑定不同）
  P3 长片拼接点完后：同上
  P4 参数抽屉打开后：汉堡命中、媒体库卡片命中、遮罩命中
  P5 DOM 里是否存在任何「返回生成类型选择」语义的控件
  P6 收藏入口在抽屉里的命中情况

运行：
    python3 scripts/probe_mobile_nav_rootcause.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/probe
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

PROBE_JS = r"""
() => {
  const hitAt = (x, y) => {
    const t = document.elementFromPoint(x, y);
    if (!t) return null;
    const id = t.id || null;
    const cls = (t.className && t.className.baseVal !== undefined
                 ? t.className.baseVal : String(t.className || '')).slice(0, 80);
    return {x: x, y: y, tag: t.tagName, id: id, cls: cls,
            text: (t.textContent || '').trim().slice(0, 36)};
  };
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {top: Math.round(b.top), left: Math.round(b.left),
            w: Math.round(b.width), h: Math.round(b.height),
            bottom: Math.round(b.bottom), right: Math.round(b.right),
            visible: getComputedStyle(el).display !== 'none'};
  };
  const nav = document.querySelector('#navToggle');
  const nb = nav ? nav.getBoundingClientRect() : null;
  const sb = document.querySelector('#sidebar');
  const pm = document.querySelector('#panelMask');
  const pnl = document.querySelector('#panel');
  const card = document.querySelector('#jobsGrid .job-card[data-jid]');
  const cb = card ? card.getBoundingClientRect() : null;

  // 「返回类型选择」语义的控件全量搜
  const backish = [];
  document.querySelectorAll('button,a,[role=button]').forEach(el => {
    const t = ((el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')
               + ' ' + (el.getAttribute('title') || '') + ' ' + (el.id || '')
               + ' ' + (el.className || '')).toLowerCase();
    if (/返回|back|回到|prev|上一步/.test(t)) {
      const r = el.getBoundingClientRect();
      backish.push({id: el.id || null, tag: el.tagName,
                    cls: String(el.className || '').slice(0, 60),
                    text: (el.textContent || '').trim().slice(0, 30),
                    visible: getComputedStyle(el).display !== 'none'
                             && r.width > 0 && r.height > 0,
                    rect: {top: Math.round(r.top), left: Math.round(r.left),
                           w: Math.round(r.width), h: Math.round(r.height)}});
    }
  });

  return {
    vw: innerWidth, vh: innerHeight,
    navToggle: {box: box('#navToggle'),
                 hit: nb ? hitAt(Math.round(nb.left + nb.width / 2),
                                Math.round(nb.top + nb.height / 2)) : null},
    sidebar: {box: box('#sidebar'),
              open: sb ? sb.classList.contains('open') : null},
    sidebarMask: {box: box('#sidebarMask'),
                  open: pm ? pm.classList.contains('open') : null},
    panel: {box: box('#panel'),
            open: pnl ? pnl.classList.contains('open') : null},
    panelMask: {box: box('#panelMask'),
                open: pm ? pm.classList.contains('open') : null},
    jobsArea: box('#jobsArea'),
    cardCount: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
    cardHit: cb ? hitAt(Math.round(cb.left + cb.width / 2),
                        Math.round(cb.top + Math.min(cb.height / 2, 60))) : null,
    cardRect: cb ? {top: Math.round(cb.top), left: Math.round(cb.left),
                    w: Math.round(cb.width), h: Math.round(cb.height)} : null,
    navFavorites: box('#navFavorites'),
    navFavoritesHit: (() => {
      const el = document.querySelector('#navFavorites');
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return hitAt(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
    })(),
    genTypeTabs: box('#genTypeTabs'),
    statusTabs: box('.jobs-tabs'),
    pageSize: box('#pageSizeSelect') || box('.page-size-tools select'),
    backish: backish,
    stateMode: (window.state && window.state.mode) || null,
    statePage: (window.state && window.state.page) || null,
    stateTab: (window.state && window.state.tab) || null,
  };
}
"""


def snap(page, outdir, name):
    page.screenshot(path=str(Path(outdir) / (name + ".png")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "probe"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    report = {"base": base, "viewport": [args.width, args.height], "steps": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(1800)

        report["steps"]["P0_initial"] = page.evaluate(PROBE_JS)

        # ---- P1: 静态 nav-item (t2i) ----
        page.click("#navToggle")
        page.wait_for_timeout(500)
        page.click('#sidebar .nav-item[data-mode="t2i"]')
        page.wait_for_timeout(900)
        report["steps"]["P1_after_static_t2i"] = page.evaluate(PROBE_JS)
        snap(page, outdir, "P1-after-static-t2i")

        # ---- P2: 动态 nav-model-item (t2v) ----
        page.click("#navToggle")
        page.wait_for_timeout(500)
        t2v = page.query_selector('#sidebar .nav-model-item[data-mode="t2v"]')
        if t2v:
            t2v.click()
        page.wait_for_timeout(900)
        report["steps"]["P2_after_dynamic_t2v"] = page.evaluate(PROBE_JS)
        snap(page, outdir, "P2-after-dynamic-t2v")

        # ---- P3: 长片拼接 ----
        page.click("#navToggle")
        page.wait_for_timeout(500)
        # 长片拼接在抽屉里的折叠区底部，Playwright 的可见性/视口检查会卡住
        # （实测报 "element is outside of the viewport"）—— 用 JS 直接派发点击，
        # 走的是和用户手指完全相同的事件链。
        clicked_lf = page.evaluate(
            """() => { const el = document.querySelector(
                 '#sidebar .nav-model-item[data-mode="longfilm"]');
               if (!el) return false;
               el.click(); return true; }""")
        report["P3_longfilm_clicked"] = clicked_lf
        page.wait_for_timeout(900)
        report["steps"]["P3_after_longfilm"] = page.evaluate(PROBE_JS)
        snap(page, outdir, "P3-after-longfilm")

        # ---- P4: 参数抽屉打开后 ----
        page.click("#navToggle")
        page.wait_for_timeout(400)
        t2i2 = page.query_selector('#sidebar .nav-item[data-mode="t2i"]')
        if t2i2:
            t2i2.click()
        page.wait_for_timeout(700)
        fab = page.query_selector("#panelFab")
        if fab and fab.is_visible():
            fab.click()
            page.wait_for_timeout(700)
        report["steps"]["P4_panel_open"] = page.evaluate(PROBE_JS)
        snap(page, outdir, "P4-panel-open")

        # P4b: 参数抽屉开着时，汉堡按钮点得动吗？
        nav = page.query_selector("#navToggle")
        reopen_ok = False
        reopen_err = None
        try:
            if nav and nav.is_visible():
                nav.click(timeout=2500)
                page.wait_for_timeout(500)
                reopen_ok = page.evaluate(
                    "() => document.querySelector('#sidebar').classList.contains('open')")
            else:
                reopen_err = "navToggle 不可见"
        except Exception as e:
            reopen_err = str(e)[:160]
        report["P4b_reopen_sidebar_with_panel_open"] = reopen_ok
        report["P4b_error"] = reopen_err
        report["steps"]["P4b_after_reopen_attempt"] = page.evaluate(PROBE_JS)
        snap(page, outdir, "P4b-reopen-attempt")

        # ---- P5: 收藏入口在抽屉里命中吗 ----
        report["steps"]["P5_favorites_nav"] = page.evaluate(PROBE_JS)

        report["errors"] = errs[:10]
        browser.close()

    (outdir / "probe.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for k, v in report["steps"].items():
        print("=" * 68)
        print(k)
        print("  sidebar open :", v["sidebar"]["open"], "| mask open:", v["sidebarMask"]["open"])
        print("  panel   open :", v["panel"]["open"], "| panelMask:", v["panelMask"]["open"])
        print("  navToggle hit:", json.dumps(v["navToggle"]["hit"], ensure_ascii=False))
        print("  cardHit      :", json.dumps(v["cardHit"], ensure_ascii=False),
              "| cards:", v["cardCount"])
        print("  mode/page/tab:", v["stateMode"], v["statePage"], v["stateTab"])
        print("  backish      :", json.dumps(v["backish"], ensure_ascii=False)[:400])
        print("  navFavorites :", v["navFavorites"], json.dumps(v["navFavoritesHit"], ensure_ascii=False))
    print("P4b reopen with panel open:", report["P4b_reopen_sidebar_with_panel_open"],
          report["P4b_error"])
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
