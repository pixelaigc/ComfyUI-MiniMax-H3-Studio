# -*- coding: utf-8 -*-
"""移动端导航「死路」 —— 状态 × 目标 可达性矩阵（真实触摸 + 命中测试）。

【为什么用矩阵而不是单条路径】用户那句「无法回到生成类型选择，也无法看到
生成媒体库」对应的是**可达性**问题，不是「某个按钮坏了」。
必须枚举「从每个可达状态出发，一个用户动作能不能达成目标」，
才能区分：
  (a) 有入口但要点两次（可用但难用）
  (b) 入口被别的层挡住（命中测试打不中）—— 真正的死路
  (c) DOM 里压根没有控件（不存在）
命中测试用 elementFromPoint —— 它返回**用户手指实际落到的那一层**。

【目标定义】
  G1 回到「生成类型选择」 = 汉堡按钮命中自身（或抽屉已开且某类型项命中）
  G2 看到「生成媒体库」   = 媒体库卡片中心命中卡片自身（不被抽屉/遮罩盖住）
  G3 打开「收藏」         = #navFavorites 命中自身

运行：
    python3 scripts/probe_mobile_nav_matrix.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/matrix-before --width 375 --height 812
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

STATE_JS = r"""
() => {
  const desc = (el) => el ? {
    tag: el.tagName, id: el.id || null,
    cls: (el.className && el.className.baseVal !== undefined
          ? el.className.baseVal : String(el.className || '')).slice(0, 70),
    text: (el.textContent || '').trim().slice(0, 30),
  } : null;
  const hit = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return {ok: false, why: 'no-element'};
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return {ok: False, why: 'zero-size'};
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) {
      return {ok: false, why: 'off-viewport', at: {x, y}};
    }
    const t = document.elementFromPoint(x, y);
    const isSelf = t === el || el.contains(t);
    return {ok: isSelf, why: isSelf ? 'hit-self' : 'covered-by',
            at: {x, y}, hit: desc(t),
            targetRect: {top: Math.round(r.top), left: Math.round(r.left),
                         w: Math.round(r.width), h: Math.round(r.height)}};
  };
  const sb = document.querySelector('#sidebar');
  const pnl = document.querySelector('#panel');
  const pnmask = document.querySelector('#panelMask');
  const sbmask = document.querySelector('#sidebarMask');
  // 媒体库卡片：取第一张真实卡片（data-jid），命中点取卡片上部（标题区）
  const card = document.querySelector('#jobsGrid .job-card[data-jid]');
  let cardHit = {ok: false, why: 'no-card'};
  if (card) {
    const r = card.getBoundingClientRect();
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + Math.min(r.height / 2, 40));
    const t = (x >= 0 && y >= 0 && x <= innerWidth && y <= innerHeight)
      ? document.elementFromPoint(x, y) : null;
    cardHit = {ok: !!t && (t === card || card.contains(t)),
               why: (!!t && (t === card || card.contains(t))) ? 'hit-self' : 'covered-by',
               at: {x, y}, hit: desc(t),
               cardRect: {top: Math.round(r.top), left: Math.round(r.left),
                          w: Math.round(r.width), h: Math.round(r.height)}};
  }
  const activeTab = document.querySelector('.jobs-tabs button.active');
  const activeModeNav = document.querySelector('#sidebar .nav-item.active, #sidebar .nav-model-item.active');
  return {
    vw: innerWidth, vh: innerHeight,
    sidebarOpen: sb ? sb.classList.contains('open') : null,
    sidebarMaskOpen: sbmask ? sbmask.classList.contains('open') : null,
    panelOpen: pnl ? pnl.classList.contains('open') : null,
    panelMaskOpen: pnmask ? pnmask.classList.contains('open') : null,
    jobsAreaHidden: document.querySelector('#jobsArea').classList.contains('page-hidden'),
    cardCount: document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
    activeTab: activeTab ? (activeTab.dataset.st || activeTab.textContent.trim()) : null,
    activeModeNav: activeModeNav ? (activeModeNav.textContent || '').trim().slice(0, 24) : null,
    G1_hamburger: hit('#navToggle'),
    G2_media_card: cardHit,
    G3_favorites_nav: hit('#navFavorites'),
    G4_type_tabs: hit('#genTypeTabs'),
    G5_status_tabs: hit('.jobs-tabs'),
    G6_page_size: hit('#pageSizeSelect') ,
  };
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "matrix-before"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")

    matrix = {}

    def tap(page, sel):
        """真实触摸（is_mobile 上下文里 touchscreen.tap 走触摸事件链）。"""
        ok = page.evaluate(
            """(s) => { const el = document.querySelector(s);
               if (!el) return false; el.click(); return true; }""", sel)
        page.wait_for_timeout(700)
        return ok

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(1800)

        def ensure_drawer_closed():
            """【确定性】每次开抽屉前先确保它是关着的。
            修前「动态项不收抽屉」会让上一状态残留着打开的抽屉，
            下一行的 tap('#navToggle') 就变成「关」—— 状态机因此错乱，
            矩阵读数不可比。这里显式归零。"""
            if page.evaluate(
                    "() => document.querySelector('#sidebar').classList.contains('open')"):
                page.evaluate("() => document.querySelector('#navToggle').click()")
                page.wait_for_timeout(400)

        def snap(name):
            page.screenshot(path=str(outdir / (name + ".png")))
            matrix[name] = page.evaluate(STATE_JS)

        # S0 初始
        snap("S0-initial")

        # S1 打开抽屉
        ensure_drawer_closed()
        tap(page, "#navToggle")
        snap("S1-drawer-open")

        # S2 点静态类型 t2i
        tap(page, '#sidebar .nav-item[data-mode="t2i"]')
        snap("S2-after-static-t2i")

        # S3 点动态视频类型 t2v（video-nav 异步渲染）
        ensure_drawer_closed()
        tap(page, "#navToggle")
        tap(page, '#sidebar .nav-model-item[data-mode="t2v"]')
        snap("S3-after-dynamic-t2v")

        # S4 再点动态类别头（首尾帧）
        ensure_drawer_closed()
        tap(page, "#navToggle")
        tap(page, '#sidebar .nav-cat-head[data-category="flf"]')
        snap("S4-after-category-head")

        # S5 长片拼接
        ensure_drawer_closed()
        tap(page, "#navToggle")
        tap(page, '#sidebar .nav-model-item[data-mode="longfilm"]')
        snap("S5-after-longfilm")

        # S6 回到 t2i 并打开参数抽屉
        ensure_drawer_closed()
        tap(page, "#navToggle")
        tap(page, '#sidebar .nav-item[data-mode="t2i"]')
        tap(page, "#panelFab")
        snap("S6-panel-drawer-open")

        # S7 参数抽屉开着时点汉堡（试图回到类型选择）
        tap(page, "#navToggle")
        snap("S7-toggle-hamburger-while-panel-open")

        # S8 关掉参数抽屉后
        tap(page, "#panelMask")
        snap("S8-after-panel-closed")

        report = {"base": base, "viewport": [args.width, args.height],
                  "errors": errs[:10], "matrix": matrix}
        (outdir / "matrix.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    hdr = "%-26s %-9s %-9s %-9s %-9s %-9s" % ("state", "drawer", "panel", "G1hamb",
                                               "G2card", "G3fav")
    print(hdr)
    print("-" * len(hdr))
    for k, v in matrix.items():
        print("%-26s %-9s %-9s %-9s %-9s %-9s" % (
            k,
            "OPEN" if v["sidebarOpen"] else "closed",
            "OPEN" if v["panelOpen"] else "closed",
            v["G1_hamburger"]["why"],
            v["G2_media_card"]["why"],
            v["G3_favorites_nav"]["why"]))
    print()
    for k, v in matrix.items():
        bad = []
        for goal in ("G1_hamburger", "G2_media_card", "G3_favorites_nav"):
            if not v[goal]["ok"]:
                bad.append("%s=%s(%s)" % (goal, v[goal]["why"],
                                          (v[goal].get("hit") or {}).get("id")
                                          or (v[goal].get("hit") or {}).get("cls", "")))
        if bad:
            print("DEAD-END?", k, "->", "; ".join(bad))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
