# -*- coding: utf-8 -*-
"""量化 FAB（position:fixed 右下角）与**抽屉内容**、**卡片**的重叠。

【为什么单独测 FAB vs 抽屉】FAB 的 z-index 是 960，参数抽屉本体是 950、
侧栏抽屉是 940 —— FAB 在**两层抽屉之上**。
需求 B4 要求「导航控件不得遮挡卡片正文、右键菜单与抽屉内容」，
所以必须逐状态量：FAB 开着时到底压在谁身上。

运行：
    python3 scripts/probe_fab_vs_drawers.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/fabdrawers-before --width 375 --height 812
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
  const fab = document.querySelector('#panelFab');
  const vis = (el) => !!el && getComputedStyle(el).display !== 'none'
                     && el.getBoundingClientRect().width > 0;
  const out = {fabVisible: vis(fab), fabRect: null, sidebarOverlap: null,
               panelOverlap: null, cardTextOverlap: [], cardMediaOverlap: []};
  if (!out.fabVisible) return out;
  const fr = fab.getBoundingClientRect();
  out.fabRect = {top: Math.round(fr.top), left: Math.round(fr.left),
                 w: Math.round(fr.width), h: Math.round(fr.height)};
  const ov = (a, b) => {
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return (w > 0 && h > 0) ? {w: Math.round(w), h: Math.round(h)} : null;
  };
  const sb = document.querySelector('#sidebar');
  if (sb && getComputedStyle(sb).display !== 'none') {
    const sr = sb.getBoundingClientRect();
    const o = ov(fr, sr);
    if (o) out.sidebarOverlap = Object.assign(o, {
      sidebarRect: {top: Math.round(sr.top), left: Math.round(sr.left),
                    w: Math.round(sr.width), h: Math.round(sr.height)},
      // 抽屉里被压住的具体控件
      covered: Array.from(sb.querySelectorAll('.nav-item, .nav-group, .nav-cat-head, .nav-drawer-close'))
        .filter(el => { const r = el.getBoundingClientRect();
                        if (r.width === 0) return false;
                        const c = ov(fr, r);
                        return c && c.w > 0 && c.h > 0; })
        .map(el => ({text: (el.textContent || '').trim().slice(0, 20),
                     cls: String(el.className || '').slice(0, 40)})),
    });
  }
  const pn = document.querySelector('#panel');
  if (pn && getComputedStyle(pn).display !== 'none') {
    const pr = pn.getBoundingClientRect();
    const o = ov(fr, pr);
    if (o) out.panelOverlap = Object.assign(o, {
      panelRect: {top: Math.round(pr.top), left: Math.round(pr.left),
                  w: Math.round(pr.width), h: Math.round(pr.height)}});
  }
  document.querySelectorAll('#jobsGrid .job-card[data-jid]').forEach(card => {
    card.querySelectorAll('.job-meta, .job-meta *').forEach(t => {
      const tr = t.getBoundingClientRect();
      if (!tr.width || !tr.height) return;
      if (tr.bottom < 0 || tr.top > innerHeight) return;
      const o = ov(fr, tr);
      if (o) out.cardTextOverlap.push({text: (t.textContent||'').trim().slice(0,20),
                                       cls: String(t.className||'').slice(0,30), ...o});
    });
    card.querySelectorAll('.job-preview img, .job-preview video').forEach(m => {
      const mr = m.getBoundingClientRect();
      if (!mr.width || !mr.height) return;
      if (mr.bottom < 0 || mr.top > innerHeight) return;
      const o = ov(fr, mr);
      if (o) out.cardMediaOverlap.push(o);
    });
  });
  return out;
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fabdrawers-before"))
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
        # 状态 1：初始
        r["S1_initial"] = page.evaluate(PROBE_JS)
        # 状态 2：侧栏抽屉打开
        page.evaluate("() => document.querySelector('#navToggle').click()")
        page.wait_for_timeout(600)
        r["S2_sidebar_open"] = page.evaluate(PROBE_JS)
        page.screenshot(path=str(outdir / "FD1-sidebar-open.png"))
        # 状态 3：关抽屉 + 开参数抽屉
        page.evaluate("() => document.querySelector('#navDrawerClose')"
                      " ? document.querySelector('#navDrawerClose').click()"
                      " : document.querySelector('#navToggle').click()")
        page.wait_for_timeout(600)
        page.evaluate("() => document.querySelector('#panelFab').click()")
        page.wait_for_timeout(800)
        r["S3_panel_open"] = page.evaluate(PROBE_JS)
        page.screenshot(path=str(outdir / "FD2-panel-open.png"))

        browser.close()

    (outdir / "fab-vs-drawers.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in r.items():
        print("=" * 60)
        print(k, "fabVisible=", v["fabVisible"], v["fabRect"])
        print("  sidebarOverlap:", json.dumps(v["sidebarOverlap"], ensure_ascii=False))
        print("  panelOverlap  :", json.dumps(v["panelOverlap"], ensure_ascii=False))
        print("  cardTextOverlap:", len(v["cardTextOverlap"]), v["cardTextOverlap"][:3])
        print("  cardMediaOverlap:", len(v["cardMediaOverlap"]))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
