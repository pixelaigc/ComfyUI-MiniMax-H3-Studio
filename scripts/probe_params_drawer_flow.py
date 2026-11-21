# -*- coding: utf-8 -*-
"""真实用户全流程复测：选类型 → 开参数面板 → 回类型选择 → 看媒体库。

【为什么必须单独做】
上一轮复测只覆盖了「选类型 → 看媒体库」，漏掉了用户**必然要走**的一步：
在移动端，参数区收进了**底部抽屉**（#panel），打开它的入口是 FAB。
用户选完生成类型 → 想调参数 → 点 FAB → 参数抽屉升起（max-height:88vh）
→ 这时候：
  · 媒体库被参数抽屉盖住
  · 抽屉本体**没有任何关闭按钮**（只有 CSS 画的装饰把手 ::before，不可点）
  · 唯一出路是点遮罩
⇒ 「无法回到生成类型选择，也无法看到生成媒体库」很可能发生在这个状态。

【同时覆盖矮视口】
iOS Safari 的地址栏/工具栏会吃掉 ~120px，真实可用高度常常只有
667 / 664 甚至更少 —— 而我上一轮测的是 812 / 844（完整视口）。
⇒ 追加 375x667 / 390x664 两档。

【判据】
  · 参数抽屉打开时，汉堡按钮必须真能点到（命中测试）
  · 参数抽屉打开时，媒体库卡片是否可点（抽屉上方那 12vh 之外应该可点）
  · 点遮罩能否关掉参数抽屉
  · 关掉后能否看到媒体库
"""
import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

STATE_JS = """() => {
  const sb = document.querySelector('#sidebar');
  const panel = document.querySelector('#panel');
  const mask = document.querySelector('#panelMask');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
  const inView = cards.filter(c => {
    const b = c.getBoundingClientRect();
    return b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth;
  });
  const clickable = inView.filter(c => {
    const b = c.getBoundingClientRect();
    const x = Math.round(Math.min(innerWidth-1, Math.max(0, b.left+b.width/2)));
    const y = Math.round(Math.min(innerHeight-1, Math.max(0, b.top+b.height/2)));
    const t = document.elementFromPoint(x, y);
    return t && (t === c || c.contains(t));
  });
  const tog = document.querySelector('#navToggle');
  const togR = tog.getBoundingClientRect();
  const togHit = (() => {
    if (togR.width === 0) return {visible: false};
    const x = Math.round(togR.left + togR.width/2);
    const y = Math.round(togR.top + togR.height/2);
    const t = document.elementFromPoint(x, y);
    return {visible: true, hitIsSelf: t ? (t === tog || tog.contains(t)) : false,
            hitTag: t ? t.tagName : null, hitId: t ? t.id : null,
            hitCls: t ? String(t.className||'').slice(0,60) : null};
  })();
  return {
    sidebarOpen: sb.classList.contains('open'),
    panelOpen: !!(panel && panel.classList.contains('open')),
    panelMaskOpen: !!(mask && mask.classList.contains('open')),
    panelRect: panel ? (() => { const b = panel.getBoundingClientRect();
       return {top: Math.round(b.top), h: Math.round(b.height),
               bottom: Math.round(b.bottom)}; })() : null,
    navToggleHit: togHit,
    cardsTotal: cards.length,
    cardsInViewport: inView.length,
    cardsClickable: clickable.length,
    fabHidden: (() => { const f = document.querySelector('#panelFab');
       if (!f) return null;
       return f.hasAttribute('hidden') || getComputedStyle(f).display === 'none'; })(),
    bodyScrollLocked: document.body.classList.contains('modal-open'),
  };
}"""


def real_tap(page, selector, timeout=7000):
    try:
        page.tap(selector, timeout=timeout)
        return {"ok": True, "method": "tap"}
    except Exception as e:
        diag = page.evaluate(
            """(s) => { const el = document.querySelector(s);
               if (!el) return {reason:'not-found'};
               const b = el.getBoundingClientRect();
               if (!b.width||!b.height) return {reason:'zero-size'};
               const x=Math.round(b.left+b.width/2), y=Math.round(b.top+b.height/2);
               if (x<0||y<0||x>innerWidth||y>innerHeight) return {reason:'offscreen',x,y};
               const t = document.elementFromPoint(x,y);
               return {reason:'hit-other', hitTag:t?t.tagName:null, hitId:t?t.id:null,
                       hitCls:t?String(t.className||'').slice(0,60):null}; }""", selector)
        try:
            page.click(selector, timeout=timeout)
            return {"ok": True, "method": "click-fallback", "diag": diag}
        except Exception:
            return {"ok": False, "method": "failed",
                    "err": str(e).split("\n")[0][:180], "diag": diag}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "params-drawer"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--tag", default="live")
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
        r["s0_initial"] = page.evaluate(STATE_JS)
        page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s0.png"))

        # 1) 选一个生成类型（动态视频项，触发最复杂路径）
        r["s1_open_picker"] = real_tap(page, "#navToggle")
        page.wait_for_timeout(900)
        r["s1_select_t2v"] = real_tap(
            page, ".nav-model-item[data-mode='t2v'], .nav-item[data-mode='t2v']")
        page.wait_for_timeout(1200)
        r["s1_after_type"] = page.evaluate(STATE_JS)
        page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s1-type.png"))

        # 2) 打开参数抽屉（FAB）
        r["s2_tap_fab"] = real_tap(page, "#panelFab")
        page.wait_for_timeout(900)
        r["s2_panel_open"] = page.evaluate(STATE_JS)
        page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s2-panel-open.png"))

        # 3) 参数抽屉开着时，汉堡能不能点（回类型选择）
        r["s3_tap_hamburger"] = real_tap(page, "#navToggle")
        page.wait_for_timeout(900)
        r["s3_after_hamburger"] = page.evaluate(STATE_JS)
        page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s3-hamburger.png"))

        # 4) 点遮罩关参数抽屉
        r["s4_tap_mask"] = real_tap(page, "#panelMask")
        page.wait_for_timeout(900)
        r["s4_after_mask"] = page.evaluate(STATE_JS)
        page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s4-mask.png"))

        # 5) 关掉之后能不能看到媒体库 + 开详情
        r["s5_media_library"] = page.evaluate(STATE_JS)
        try:
            page.tap("#jobsGrid .job-card[data-jid]", timeout=7000)
            page.wait_for_timeout(900)
            r["s5_detail_open"] = page.evaluate(
                """() => { const m = document.querySelector('#detailModal');
                   if (!m || m.hasAttribute('hidden')) return false;
                   const b = m.getBoundingClientRect();
                   return b.width>0 && b.height>0; }""")
            page.screenshot(path=str(outdir / f"{args.tag}-{args.width}-{args.height}-s5-detail.png"))
            page.tap("#detailModalClose", timeout=7000)
        except Exception as e:
            r["s5_detail_open"] = "FAIL: " + str(e).split("\n")[0][:180]

        browser.close()

    (outdir / f"params-{args.tag}-{args.width}x{args.height}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
