# -*- coding: utf-8 -*-
"""量化：FAB（position:fixed 右下角）在窄屏下到底压住了卡片的什么。

【为什么必须量化而不是目测】需求 B4 要求「导航控件不得遮挡卡片正文」。
「正文」= 卡片的文字行（提示词 / 尺寸 / 模型），不是缩略图。
所以要逐个滚动位置、逐张卡片，列出 FAB 矩形与卡片内部**文本元素**
矩形的交集，而不是只报「压住了卡片」。

运行：
    python3 scripts/probe_fab_overlap.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/fab-before --width 375 --height 812
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

OVERLAP_JS = r"""
() => {
  const fab = document.querySelector('#panelFab');
  if (!fab || getComputedStyle(fab).display === 'none') return {fabHidden: true};
  const fr = fab.getBoundingClientRect();
  const rows = [];
  document.querySelectorAll('#jobsGrid .job-card[data-jid]').forEach(card => {
    const cr = card.getBoundingClientRect();
    // 卡片内部的所有「文字元素」
    const texts = card.querySelectorAll('.job-meta, .job-meta *, .job-row, .job-row *, '
      + '.job-title, .job-title *, .job-fav-badge, .reso-note, .row');
    texts.forEach(t => {
      const r = t.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      if (r.bottom < 0 || r.top > innerHeight) return;      // 不在视口内，不算遮挡
      const ovw = Math.min(fr.right, r.right) - Math.max(fr.left, r.left);
      const ovh = Math.min(fr.bottom, r.bottom) - Math.max(fr.top, r.top);
      if (ovw > 0 && ovh > 0) {
        rows.push({text: (t.textContent || '').trim().slice(0, 24),
                   cls: String(t.className || '').slice(0, 40),
                   ovW: Math.round(ovw), ovH: Math.round(ovh)});
      }
    });
  });
  // 也报一下压住了缩略图（img/video）多少
  const media = [];
  document.querySelectorAll('#jobsGrid .job-card[data-jid] .job-preview img, '
    + '#jobsGrid .job-card[data-jid] .job-preview video').forEach(m => {
    const r = m.getBoundingClientRect();
    const ovw = Math.min(fr.right, r.right) - Math.max(fr.left, r.left);
    const ovh = Math.min(fr.bottom, r.bottom) - Math.max(fr.top, r.top);
    if (ovw > 0 && ovh > 0) media.push({ovW: Math.round(ovw), ovH: Math.round(ovh)});
  });
  return {fabHidden: false,
          fabRect: {top: Math.round(fr.top), left: Math.round(fr.left),
                    w: Math.round(fr.width), h: Math.round(fr.height)},
          textOverlaps: rows, mediaOverlaps: media};
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fab-before"))
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

        positions = []
        # 顶部 / 中部 / 底部 三个滚动位置
        for i, frac in enumerate([0.0, 0.5, 1.0]):
            page.evaluate(
                """(f) => { const a = document.querySelector('#jobsArea');
                   a.scrollTop = (a.scrollHeight - a.clientHeight) * f; }""", frac)
            page.wait_for_timeout(500)
            res = page.evaluate(OVERLAP_JS)
            res["scrollFrac"] = frac
            positions.append(res)
            page.screenshot(path=str(outdir / f"F{i}-scroll{int(frac*100)}.png"))

        report = {"base": base, "viewport": [args.width, args.height], "positions": positions}
        (outdir / "fab-overlap.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    for p in positions:
        print("scroll %s%%  fab=%s" % (int(p["scrollFrac"] * 100), p.get("fabRect")))
        print("   压住文字元素 %d 个: %s" % (len(p.get("textOverlaps", [])),
                                           json.dumps(p.get("textOverlaps", [])[:4], ensure_ascii=False)))
        print("   压住缩略图 %d 个" % len(p.get("mediaOverlaps", [])))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
