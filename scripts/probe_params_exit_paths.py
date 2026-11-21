# -*- coding: utf-8 -*-
"""参数抽屉的**唯一退路**验证 + 媒体库可达性。

【上一轮 probe 的结论要修正】
probe_params_drawer_flow.py 在 s4 报「点遮罩失败」，但 diag 显示
`zero-size` —— 因为 s3 已经点了汉堡，抽屉顺带被关掉了，
所以那一刻 #panelMask 根本没有 .open（display:none ⇒ 零尺寸）。
**那是脚本顺序问题，不是产品缺陷。**

本脚本把两条退路分开测：
  路径 A：参数抽屉开着 → **直接点遮罩**（不碰汉堡）
  路径 B：参数抽屉开着 → **点汉堡** → 抽屉开了 → 关抽屉 → 看媒体库

【关键问题】
参数抽屉本体**没有任何关闭按钮**（只有 CSS 画的装饰把手 ::before，不可点）。
⇒ 移动端关掉参数抽屉只有两条路：
   ① 点遮罩（遮罩只露顶栏以下、抽屉以上那一条 —— 到底有多少像素？）
   ② 点汉堡（但那是「开侧栏抽屉」，顺带把参数抽屉关了）

【要量的】
  遮罩可点区域的实际像素高度（手指能有多准）
  关掉抽屉后媒体库卡片是否真的可点
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

MASK_GEOM = """() => {
  const mask = document.querySelector('#panelMask');
  const panel = document.querySelector('#panel');
  const topbar = document.querySelector('.topbar');
  if (!mask || !panel) return {error:'missing'};
  const mb = mask.getBoundingClientRect();
  const pb = panel.getBoundingClientRect();
  const tb = topbar.getBoundingClientRect();
  const cs = getComputedStyle(mask);
  const visible = cs.display !== 'none' && cs.visibility !== 'hidden';
  // 遮罩真正露出来、能被点到的区域 = 遮罩 rect ∩ 视口 − 抽屉 rect − 顶栏
  const x = Math.round(mb.left + mb.width/2);
  let topY = Math.round(Math.max(mb.top, tb.bottom));
  const bottomLimit = Math.round(pb.top);
  const clickableH = Math.max(0, bottomLimit - topY);
  // 沿中线从顶栏底部往下扫，找第一个命中遮罩的 y
  let firstHit = null;
  for (let y = Math.round(tb.bottom); y < innerHeight; y += 2) {
    const t = document.elementFromPoint(x, y);
    if (t && (t === mask || mask.contains(t))) { firstHit = y; break; }
  }
  return {
    maskVisible: visible,
    maskRect: {top: Math.round(mb.top), h: Math.round(mb.height),
               left: Math.round(mb.left), w: Math.round(mb.width)},
    panelTop: Math.round(pb.top), panelBottom: Math.round(pb.bottom),
    topbarBottom: Math.round(tb.bottom),
    maskStripHeight: clickableH,
    firstMaskHitY: firstHit,
    maskZ: cs.zIndex,
  };
}"""

CARDS = """() => {
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
  const inView = cards.filter(c => { const b = c.getBoundingClientRect();
    return b.bottom>0 && b.top<innerHeight && b.right>0 && b.left<innerWidth; });
  const clickable = inView.filter(c => {
    const b = c.getBoundingClientRect();
    const x = Math.round(Math.min(innerWidth-1, Math.max(0, b.left+b.width/2)));
    const y = Math.round(Math.min(innerHeight-1, Math.max(0, b.top+b.height/2)));
    const t = document.elementFromPoint(x,y);
    return t && (t===c || c.contains(t));
  });
  return {total: cards.length, inViewport: inView.length, clickable: clickable.length,
          sidebarOpen: document.querySelector('#sidebar').classList.contains('open'),
          panelOpen: !!document.querySelector('#panel.open')};
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "params-exit"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out); outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    r = {"base": base, "viewport": [args.width, args.height]}

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = b.new_context(viewport={"width": args.width, "height": args.height},
                            is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
        page.wait_for_timeout(2500)
        r["cards_initial"] = page.evaluate(CARDS)

        # --- 路径 A：开参数抽屉 → 直接点遮罩 ---
        page.tap("#panelFab", timeout=8000)
        page.wait_for_timeout(900)
        r["A_geom_when_open"] = page.evaluate(MASK_GEOM)
        page.screenshot(path=str(outdir / f"exit-{args.width}-A1-panel-open.png"))

        # 真实点遮罩**露出来的那一条**（顶栏下沿 + 4px）
        geom = r["A_geom_when_open"]
        if geom.get("firstMaskHitY") is not None:
            y = geom["firstMaskHitY"] + 2
            x = args.width // 2
            try:
                page.touchscreen.tap(x, y)
                r["A_tap_mask_strip"] = {"ok": True, "x": x, "y": y}
            except Exception as e:
                r["A_tap_mask_strip"] = {"ok": False, "err": str(e).split("\n")[0][:180],
                                         "x": x, "y": y}
            page.wait_for_timeout(900)
            r["A_panel_closed_after_mask"] = not page.evaluate(
                "() => !!document.querySelector('#panel.open')")
            r["A_cards_after_mask"] = page.evaluate(CARDS)
        page.screenshot(path=str(outdir / f"exit-{args.width}-A2-after-mask.png"))

        # --- 路径 B：开参数抽屉 → 点汉堡 → 关侧栏 → 看媒体库 ---
        page.tap("#panelFab", timeout=8000)
        page.wait_for_timeout(900)
        try:
            page.tap("#navToggle", timeout=8000)
            r["B_tap_hamburger"] = "OK"
        except Exception as e:
            r["B_tap_hamburger"] = "FAIL: " + str(e).split("\n")[0][:180]
        page.wait_for_timeout(900)
        r["B_state_after_hamburger"] = page.evaluate(CARDS)
        page.screenshot(path=str(outdir / f"exit-{args.width}-B1-sidebar-open.png"))
        # 关侧栏（× 按钮）
        try:
            page.tap("#navDrawerClose", timeout=8000)
            r["B_close_sidebar"] = "OK"
        except Exception as e:
            r["B_close_sidebar"] = "FAIL: " + str(e).split("\n")[0][:180]
        page.wait_for_timeout(900)
        r["B_cards_after_close"] = page.evaluate(CARDS)
        page.screenshot(path=str(outdir / f"exit-{args.width}-B2-closed.png"))
        # 媒体库能开详情吗
        try:
            page.tap("#jobsGrid .job-card[data-jid]", timeout=8000)
            page.wait_for_timeout(900)
            r["B_detail_open"] = page.evaluate(
                """() => { const m = document.querySelector('#detailModal');
                   if (!m || m.hasAttribute('hidden')) return false;
                   const b = m.getBoundingClientRect();
                   return b.width>0 && b.height>0; }""")
            page.screenshot(path=str(outdir / f"exit-{args.width}-B3-detail.png"))
        except Exception as e:
            r["B_detail_open"] = "FAIL: " + str(e).split("\n")[0][:180]
        b.close()

    (outdir / f"exit-{args.width}x{args.height}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
