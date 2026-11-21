# -*- coding: utf-8 -*-
"""补两项实测：移动端长按能否唤出右键菜单 + FAB 是否压住卡片。

【为什么必须测】本任务要求「卡片右键加入收藏」在移动端也要能用。
移动端没有右键键，只有长按 —— 如果 contextmenu 在触摸设备上不触发，
收藏入口在手机上就是死的。同理 FAB（position:fixed 右下角）
可能压住最后一行卡片正文。

运行：
    python3 scripts/probe_mobile_extra.py --base http://127.0.0.1:8190 \
        --out evidence/mobile-nav/extra-before --width 375 --height 812
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "extra-before"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    r = {"base": base, "viewport": [args.width, args.height]}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        fired = []
        page.on("console", lambda m: fired.append(m.text))
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length >= 0",
            timeout=20000)
        page.wait_for_timeout(1800)

        # ---------- 1. 长按卡片能否唤出右键菜单 ----------
        page.evaluate("""() => {
          window.__ctxFired = 0;
          document.addEventListener('contextmenu', () => { window.__ctxFired++; }, true);
        }""")
        card = page.query_selector("#jobsGrid .job-card[data-jid] .job-preview img, "
                                   "#jobsGrid .job-card[data-jid] .job-preview video")
        if card is None:
            card = page.query_selector("#jobsGrid .job-card[data-jid]")
        r["card_preview_found"] = card is not None
        if card:
            box = card.bounding_box()
            if box:
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                # 真实触摸长按
                page.touchscreen.tap(x, y)
                page.wait_for_timeout(300)
                page.evaluate("""([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return;
                  const mk = (type) => new PointerEvent(type, {
                    bubbles: true, cancelable: true, clientX: x, clientY: y,
                    pointerType: 'touch', isPrimary: true, pointerId: 1});
                  el.dispatchEvent(mk('pointerdown'));
                  el.dispatchEvent(mk('pointerup'));
                }""", [x, y])
                page.wait_for_timeout(200)
                # 真·长按：按住 700ms 不松手
                page.evaluate("""([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return;
                  const mk = (type) => new PointerEvent(type, {
                    bubbles: true, cancelable: true, clientX: x, clientY: y,
                    pointerType: 'touch', isPrimary: true, pointerId: 1});
                  el.dispatchEvent(mk('pointerdown'));
                }""", [x, y])
                page.wait_for_timeout(750)
                page.evaluate("""([x, y]) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return;
                  const mk = (type) => new PointerEvent(type, {
                    bubbles: true, cancelable: true, clientX: x, clientY: y,
                    pointerType: 'touch', isPrimary: true, pointerId: 1});
                  el.dispatchEvent(mk('pointerup'));
                }""", [x, y])
                page.wait_for_timeout(500)
        r["contextmenu_fired_after_touch"] = page.evaluate("() => window.__ctxFired")
        r["ctx_menu_open"] = page.evaluate(
            "() => { const m = document.querySelector('#mediaContext');"
            " return m ? m.classList.contains('open') : null; }")
        page.screenshot(path=str(outdir / "E1-after-longpress.png"))

        # ---------- 2. FAB 是否压住卡片 ----------
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        r["fab_vs_card"] = page.evaluate("""() => {
          const fab = document.querySelector('#panelFab');
          if (!fab || getComputedStyle(fab).display === 'none') return {fabHidden: true};
          const fr = fab.getBoundingClientRect();
          const hits = [];
          document.querySelectorAll('#jobsGrid .job-card[data-jid]').forEach(card => {
            const cr = card.getBoundingClientRect();
            const ovw = Math.min(fr.right, cr.right) - Math.max(fr.left, cr.left);
            const ovh = Math.min(fr.bottom, cr.bottom) - Math.max(fr.top, cr.top);
            if (ovw > 0 && ovh > 0) {
              hits.push({ovW: Math.round(ovw), ovH: Math.round(ovh),
                         cardRect: {top: Math.round(cr.top), left: Math.round(cr.left),
                                    w: Math.round(cr.width), h: Math.round(cr.height)}});
            }
          });
          return {fabHidden: false,
                  fabRect: {top: Math.round(fr.top), left: Math.round(fr.left),
                            w: Math.round(fr.width), h: Math.round(fr.height)},
                  overlappingCards: hits};
        }""")

        # ---------- 3. 抽屉滚到底后「收藏」能不能真实点到 ----------
        page.click("#navToggle", timeout=6000)
        page.wait_for_timeout(500)
        page.evaluate("() => { const s = document.querySelector('#navScroll');"
                      " s.scrollTop = s.scrollHeight; }")
        page.wait_for_timeout(500)
        fav_ok, fav_err = False, None
        try:
            page.click("#navFavorites", timeout=6000)
            fav_ok = True
        except Exception as e:
            fav_err = str(e).split("\n")[0][:200]
        r["real_click_favorites"] = fav_ok
        r["real_click_favorites_error"] = fav_err
        r["favorites_tab_active_after"] = page.evaluate(
            """() => { const b = document.querySelector(".jobs-tabs button[data-st='favorites']");
               return b ? b.classList.contains('active') : null; }""")
        r["fav_cards"] = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length")
        page.screenshot(path=str(outdir / "E2-favorites-after-click.png"))

        browser.close()

    (outdir / "extra.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
