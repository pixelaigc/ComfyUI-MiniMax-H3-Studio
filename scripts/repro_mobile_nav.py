# -*- coding: utf-8 -*-
"""移动端「生成类型 → 无法回到类型选择 / 看不到媒体库」死路 —— 真实浏览器复现。

【为什么必须真浏览器】静态读 CSS 只能猜「抽屉有没有被盖住」，
必须用真实布局几何（elementFromPoint / getBoundingClientRect）才能证明
「用户手指点到那儿命中的是谁」。

判据与用户反馈一一对应：
  A1 窄断点下汉堡按钮（navToggle）是否可见、可点
  A2 抽屉里的生成类型入口点完之后，抽屉是否还能再次打开
  A3 生成媒体库（#jobsArea）在窄断点是否可见 / 被遮住
  A4 DOM 里是否存在任何返回类型选择的控件
  A5 参数抽屉（.panel）打开时是否盖住顶栏 → 间接废掉汉堡按钮

运行：
    python3 scripts/repro_mobile_nav.py --base http://127.0.0.1:8190 --out evidence/mobile-nav
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

VIEWPORTS = [
    ("phone-375", 375, 812),
    ("phone-390", 390, 844),
    ("tablet-768", 768, 1024),
    ("desktop-1280", 1280, 900),
]

# 一次实测里要采的几何事实
FACT_JS = r"""
() => {
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none'
                     && el.getBoundingClientRect().width > 0
                     && el.getBoundingClientRect().height > 0;
  const rect = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return {top: Math.round(b.top), left: Math.round(b.left),
            w: Math.round(b.width), h: Math.round(b.height),
            bottom: Math.round(b.bottom), right: Math.round(b.right)};
  };
  const hit = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    const x = Math.round(b.left + b.width / 2);
    const y = Math.round(b.top + b.height / 2);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return null;
    const t = document.elementFromPoint(x, y);
    if (!t) return null;
    return {x: x, y: y, tag: t.tagName,
            id: t.id || null,
            cls: (t.className && t.className.baseVal !== undefined
                  ? t.className.baseVal : String(t.className || '')).slice(0, 90),
            text: (t.textContent || '').trim().slice(0, 40),
            isTarget: t === el || el.contains(t)};
  };
  const nav = document.querySelector('#navToggle');
  const sb = document.querySelector('#sidebar');
  const mask = document.querySelector('#sidebarMask');
  const panel = document.querySelector('#panel');
  const jobs = document.querySelector('#jobsArea');
  const fab = document.querySelector('#panelFab');
  const grid = document.querySelector('#jobsGrid');
  const cards = document.querySelectorAll('#jobsGrid .job-card[data-jid]');
  const tabs = document.querySelector('.jobs-tabs');
  const typeTabs = document.querySelector('#genTypeTabs');
  const pageSize = document.querySelector('#pageSizeSelect') || document.querySelector('.page-size-tools select');
  const star = document.querySelector('#navFavorites');
  return {
    vw: innerWidth, vh: innerHeight,
    scrollW: document.documentElement.scrollWidth,
    navToggle: {visible: vis(nav), rect: rect(nav), hit: hit(nav)},
    sidebar: {visible: vis(sb), open: sb ? sb.classList.contains('open') : null,
              rect: rect(sb)},
    sidebarMask: {visible: vis(mask)},
    panel: {visible: vis(panel), open: panel ? panel.classList.contains('open') : null,
            rect: rect(panel), hit: hit(panel)},
    jobsArea: {visible: vis(jobs), rect: rect(jobs)},
    panelFab: {visible: vis(fab), rect: rect(fab), hit: hit(fab)},
    jobsGrid: {visible: vis(grid), rect: rect(grid)},
    cardCount: cards.length,
    firstCard: cards.length ? rect(cards[0]) : null,
    statusTabs: {visible: vis(tabs), rect: rect(tabs)},
    typeTabs: {visible: vis(typeTabs), rect: rect(typeTabs),
               count: typeTabs ? typeTabs.querySelectorAll('button').length : 0},
    pageSize: {present: !!pageSize, visible: vis(pageSize), rect: rect(pageSize)},
    navFavorites: {present: !!star, visible: vis(star), rect: rect(star)},
    docScrollW: document.documentElement.scrollWidth,
    innerW: innerWidth,
  };
}
"""


def dump(outdir, name, obj):
    p = Path(outdir) / name
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav"))
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    base = args.base.rstrip("/")
    report = {"base": base, "viewports": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for label, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                      is_mobile=(w <= 700),
                                      has_touch=(w <= 1100))
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text)
                    if m.type == "error" else None)
            page.goto(base + "/", wait_until="domcontentloaded")
            page.wait_for_function(
                "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
                timeout=20000)
            page.wait_for_timeout(1500)

            vp = {"width": w, "height": h, "errors": errors[:10], "steps": {}}

            # --- S0: 初始态 ---
            vp["steps"]["S0_initial"] = page.evaluate(FACT_JS)
            page.screenshot(path=str(outdir / f"{label}-S0-initial.png"),
                            full_page=False)

            # --- S1: 打开左侧抽屉 ---
            nav = page.query_selector("#navToggle")
            if nav and nav.is_visible():
                nav.click()
                page.wait_for_timeout(500)
            vp["steps"]["S1_sidebar_open"] = page.evaluate(FACT_JS)
            page.screenshot(path=str(outdir / f"{label}-S1-sidebar-open.png"))

            # --- S2: 点一个生成类型（视频类，取第一个可见的 mode 项）---
            picked = None
            for sel in ['#sidebar .nav-item[data-mode="t2v"]',
                        '#sidebar .nav-item[data-mode="t2i"]',
                        '#sidebar .nav-item[data-mode="t2a"]']:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    picked = sel
                    el.click()
                    break
            vp["picked_type"] = picked
            page.wait_for_timeout(900)
            vp["steps"]["S2_after_type_pick"] = page.evaluate(FACT_JS)
            page.screenshot(path=str(outdir / f"{label}-S2-after-type.png"))

            # --- S3: 抽屉能否再次打开（汉堡是否可点）---
            nav2 = page.query_selector("#navToggle")
            can_reopen = False
            reopen_err = None
            try:
                if nav2 and nav2.is_visible():
                    nav2.click(timeout=3000)
                    page.wait_for_timeout(500)
                    can_reopen = page.evaluate(
                        "() => document.querySelector('#sidebar').classList.contains('open')")
                else:
                    reopen_err = "navToggle 不可见"
            except Exception as e:
                reopen_err = str(e)[:200]
            vp["steps"]["S3_reopen_sidebar"] = page.evaluate(FACT_JS)
            vp["can_reopen_sidebar"] = can_reopen
            vp["reopen_error"] = reopen_err
            page.screenshot(path=str(outdir / f"{label}-S3-reopen.png"))

            # --- S4: 打开参数抽屉，看它是否盖住顶栏/汉堡 ---
            fab = page.query_selector("#panelFab")
            if fab and fab.is_visible():
                fab.click()
                page.wait_for_timeout(600)
            vp["steps"]["S4_panel_open"] = page.evaluate(FACT_JS)
            page.screenshot(path=str(outdir / f"{label}-S4-panel-open.png"))

            # --- S5: 媒体库是否够得着（滚到底看分页/卡片）---
            vp["steps"]["S5_scroll_bottom"] = page.evaluate(
                """() => { const a = document.querySelector('#jobsArea');
                   a.scrollTop = a.scrollHeight; return {scrollTop: a.scrollTop,
                   scrollH: a.scrollHeight, clientH: a.clientHeight}; }""")
            page.wait_for_timeout(400)
            page.screenshot(path=str(outdir / f"{label}-S5-scroll-bottom.png"))

            report["viewports"][label] = vp
            ctx.close()
        browser.close()

    (outdir / "repro-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: {"can_reopen_sidebar": v["can_reopen_sidebar"],
                          "reopen_error": v["reopen_error"],
                          "picked": v["picked_type"],
                          "navToggle_visible": v["steps"]["S2_after_type_pick"]["navToggle"]["visible"],
                          "navToggle_hit": v["steps"]["S2_after_type_pick"]["navToggle"]["hit"],
                          "jobsArea_visible": v["steps"]["S2_after_type_pick"]["jobsArea"]["visible"],
                          "cardCount": v["steps"]["S2_after_type_pick"]["cardCount"]}
                      for k, v in report["viewports"].items()},
                     ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
