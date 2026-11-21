# -*- coding: utf-8 -*-
"""FAB 与媒体库卡片的遮挡：**滚动全程扫描 + 可达性判定**。

【为什么要扫全程】
FAB 是 `position:fixed; right:14px; bottom:16px; z-index:960`
⇒ 它盖住的是**视口右下角**那一块 86×44px，
而媒体库网格在那个位置下面滚动 ⇒
**任何滚动位置**都可能有卡片内容伸到 FAB 底下。
只测 scrollTop=0 是不够的（那正好是缺陷最容易漏的情况）。

【两个判据，要分开看】
  ① 「瞬时遮挡」：某个滚动位置下，FAB 与卡片可交互元素的几何重叠面积。
     这**不等于**功能不可用 —— 用户可以再滚一下。
  ② 「可达性」：对**每一个**可交互元素（尤其「☆ 收藏」按钮），
     是否**存在某个滚动位置**让它的中心点可点。
     这才是「用户能不能用这个功能」的真判据。

【为什么 ② 是关键】
收藏按钮是**移动端唯一的收藏入口**（视频卡的长按右键被 <video> 吞掉）。
如果每个收藏按钮都有可点的滚动位置 ⇒ 功能可用，
① 只是「浮动按钮盖住滚动内容」的常规代价（Material FAB 的固有行为）。
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

SWEEP_JS = """(cfg) => {
  const area = document.querySelector('#jobsArea');
  const fab = document.querySelector('#panelFab');
  if (!area || !fab) return {error: 'missing'};
  const fcs = getComputedStyle(fab);
  if (fcs.display === 'none' || fab.hasAttribute('hidden'))
    return {fabVisible: false};
  const fb = fab.getBoundingClientRect();
  const maxScroll = Math.max(0, area.scrollHeight - area.clientHeight);
  const steps = cfg.steps;
  const overlapSamples = [];
  // 每个可交互元素 -> 有没有哪个滚动位置能让它中心可点
  const reach = new Map();

  for (let s = 0; s <= steps; s++) {
    const st = Math.round(maxScroll * s / steps);
    area.scrollTop = st;
    // 强制 reflow
    void area.offsetHeight;
    for (const el of document.querySelectorAll(
          '#jobsGrid .job-card button, #jobsGrid .job-card a')) {
      const b = el.getBoundingClientRect();
      if (b.width === 0 || b.height === 0) continue;
      const key = (el.dataset.act || el.className || '') + '|' +
                  String(el.textContent || '').trim().slice(0, 14);
      // 只有在视口内的元素才谈得上可达
      const inView = b.bottom > 0 && b.top < innerHeight &&
                     b.right > 0 && b.left < innerWidth;
      if (!inView) continue;
      const cx = Math.round(b.left + b.width / 2);
      const cy = Math.round(b.top + b.height / 2);
      const t = document.elementFromPoint(cx, cy);
      const centerSelf = t ? (t === el || el.contains(t)) : false;
      const rec = reach.get(key);
      if (rec) { rec.centerReachable = rec.centerReachable || centerSelf; }
      else {
        reach.set(key, {key: key, act: el.dataset.act || null,
                        text: String(el.textContent || '').trim().slice(0, 14),
                        centerReachable: centerSelf,
                        firstSeenAt: st});
      }
      // 瞬时几何重叠
      const ix = Math.max(0, Math.min(b.right, fb.right) - Math.max(b.left, fb.left));
      const iy = Math.max(0, Math.min(b.bottom, fb.bottom) - Math.max(b.top, fb.top));
      if (ix > 0 && iy > 0) {
        overlapSamples.push({scrollTop: st, act: el.dataset.act || null,
                              text: String(el.textContent || '').trim().slice(0, 12),
                              w: Math.round(ix), h: Math.round(iy),
                              area: Math.round(ix * iy),
                              centerSelf: centerSelf});
      }
    }
  }
  area.scrollTop = 0;
  const reachList = [...reach.values()];
  const unreachable = reachList.filter(r => !r.centerReachable);
  return {
    fabVisible: true,
    fabRect: {t: Math.round(fb.top), l: Math.round(fb.left),
               w: Math.round(fb.width), h: Math.round(fb.height)},
    maxScroll: maxScroll, steps: steps,
    overlapCount: overlapSamples.length,
    overlapMaxArea: overlapSamples.reduce((m, o) => Math.max(m, o.area), 0),
    overlapSamples: overlapSamples.slice(0, 12),
    elementsSeen: reachList.length,
    unreachableCount: unreachable.length,
    unreachable: unreachable.slice(0, 10),
  };
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "fab-sweep"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--steps", type=int, default=24)
    ap.add_argument("--tag", default="live")
    args = ap.parse_args()
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)

    r = {"base": args.base, "viewport": [args.width, args.height], "tag": args.tag}
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": args.width, "height": args.height},
                            is_mobile=True, has_touch=True, device_scale_factor=2)
        pg = ctx.new_page()
        pg.goto(args.base + "/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
        pg.wait_for_timeout(2500)
        r["sweep"] = pg.evaluate(SWEEP_JS, {"steps": args.steps})
        pg.screenshot(path=str(outdir / f"fab-{args.tag}-{args.width}.png"))
        b.close()

    (outdir / f"fab-{args.tag}-{args.width}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    s = r["sweep"]
    if not s.get("fabVisible"):
        print(f"{args.tag} {args.width}px: FAB 不可��")
    else:
        print(f"{args.tag} {args.width}px:")
        print(f"  瞬时重叠次数 = {s['overlapCount']}（最大 {s['overlapMaxArea']}px²）")
        print(f"  扫描到的可交互元素 = {s['elementsSeen']}")
        print(f"  ★ 不可达（任何滚动位置都点不到）= {s['unreachableCount']}")
        for u in s["unreachable"]:
            print(f"      {u['act']} '{u['text']}'")
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
