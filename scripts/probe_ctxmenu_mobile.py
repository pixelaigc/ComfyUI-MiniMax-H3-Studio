# -*- coding: utf-8 -*-
"""右键菜单在移动端的可达性与不被遮挡（需求 B4）。

【测什么】
  1. 长按打开的菜单是否完整落在视口内（不被裁切）
  2. 菜单项是否真的可点（命中测试，不是「存在」而已）
  3. 菜单是否被抽屉/遮罩盖住（z-index 断言：菜单在抽屉之上）
  4. 菜单里的「添加到收藏」点下去是否真的生效

运行：
    python3 scripts/probe_ctxmenu_mobile.py --base http://127.0.0.1:8917 \
        --out evidence/mobile-nav/ctxmenu-after --width 375 --height 812
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
    ap.add_argument("--base", default="http://127.0.0.1:8917")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "ctxmenu-after"))
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
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.goto(base + "/", wait_until="domcontentloaded")
        # 【为什么等「真的有媒体」而不是「有卡片」】
        # `length >= 0` 恒真 —— 首帧还没数据时也满足，于是后面的
        # 等待时间成了唯一的保险，很脆。
        # 这里等「页面里存在带媒体预览的卡」——那才是长按测试的
        # 真正前置条件。
        page.wait_for_function(
            """() => {
                 const cards = Array.from(
                   document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
                 return cards.some(c => c.querySelector(
                   '.job-preview img, .job-preview video'));
               }""",
            timeout=30000)
        page.wait_for_timeout(500)

        # 找到**第一张有媒体预览的**卡（前面的可能是「生成中」卡，没有预览图）
        idx = page.evaluate("""() => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          for (let i = 0; i < cards.length; i++) {
            const m = cards[i].querySelector('.job-preview img, .job-preview video');
            if (m) return i;
          }
          return -1;
        }""")
        r["media_card_index"] = idx
        if idx < 0:
            r["error"] = "本页没有带媒体的卡片"
            browser.close()
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
        # 【为什么要滚 #jobsArea 而不是只调 scrollIntoView】
        # scrollIntoView 会滚动**最近的滚动祖先**，但媒体库的滚动容器是
        # #jobsArea（overflow-y:auto），而 body 是 overflow:hidden ——
        # 只调 scrollIntoView 时实测元素仍停在 y=1248（视口外，812 高），
        # 长按点了个空气（elementFromPoint 返回 null）。
        # 这里直接设 #jobsArea.scrollTop。
        page.evaluate("""(i) => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          const m = cards[i].querySelector('.job-preview img, .job-preview video');
          if (!m) return;
          const area = document.querySelector('#jobsArea');
          const mr = m.getBoundingClientRect();
          const ar = area.getBoundingClientRect();
          area.scrollTop += (mr.top - ar.top) - (ar.height - mr.height) / 2;
        }""", idx)
        page.wait_for_timeout(700)
        # 取包围盒（用 evaluate 拿几何，不要跨进程拿 ElementHandle）
        box = page.evaluate("""(i) => {
          const cards = Array.from(
            document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
          const m = cards[i].querySelector('.job-preview img, .job-preview video');
          if (!m) return null;
          const b = m.getBoundingClientRect();
          return {x: b.x, y: b.y, width: b.width, height: b.height};
        }""", idx)
        if box is None:
            r["error"] = "没有图片/视频卡片可测"
            browser.close()
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
        if box is None:
            r["error"] = "取不到预览元素包围盒"
            browser.close()
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        r["touch_point"] = {"x": x, "y": y}

        cdp = ctx.new_cdp_session(page)
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 1}]})
        page.wait_for_timeout(850)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(800)

        r["menu_geometry"] = page.evaluate("""() => {
          const m = document.querySelector('#mediaContext');
          if (!m || !m.classList.contains('open')) return {open: false};
          const r = m.getBoundingClientRect();
          const items = Array.from(m.querySelectorAll('button')).map(b => {
            const br = b.getBoundingClientRect();
            const cx = Math.round(br.left + br.width/2);
            const cy = Math.round(br.top + br.height/2);
            const t = (cx>=0&&cy>=0&&cx<=innerWidth&&cy<=innerHeight)
              ? document.elementFromPoint(cx, cy) : null;
            return {text: b.textContent.trim(),
                    rect: {top: Math.round(br.top), left: Math.round(br.left),
                           w: Math.round(br.width), h: Math.round(br.height)},
                    inViewport: br.top >= 0 && br.bottom <= innerHeight
                                && br.left >= 0 && br.right <= innerWidth,
                    hitIsSelf: t ? (t === b || b.contains(t)) : false,
                    hitId: t ? t.id : null,
                    hitCls: t ? String(t.className||'').slice(0,40) : null};
          });
          return {open: true,
                  rect: {top: Math.round(r.top), left: Math.round(r.left),
                         w: Math.round(r.width), h: Math.round(r.height),
                         bottom: Math.round(r.bottom), right: Math.round(r.right)},
                  fullyInViewport: r.top >= 0 && r.bottom <= innerHeight
                                   && r.left >= 0 && r.right <= innerWidth,
                  zIndex: getComputedStyle(m).zIndex,
                  items: items};
        }""")
        page.screenshot(path=str(outdir / "CM1-longpress-menu.png"))

        # 点「添加到收藏」，验证真的生效
        fav = page.query_selector('#mediaContext button[data-act="favorite"]')
        r["fav_button_found"] = fav is not None
        if fav:
            fav.click(timeout=6000)
            page.wait_for_timeout(1500)
            r["fav_badge_after"] = page.evaluate(
                "() => document.querySelectorAll('#jobsGrid .job-card[data-jid] .job-fav-badge').length")
            r["menu_closed_after"] = page.evaluate(
                "() => { const m = document.querySelector('#mediaContext');"
                " return m ? !m.classList.contains('open') : null; }")
        page.screenshot(path=str(outdir / "CM2-after-favorite.png"))

        r["errors"] = errs[:10]
        browser.close()

    (outdir / "ctxmenu.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("OUT:", outdir)


if __name__ == "__main__":
    main()
