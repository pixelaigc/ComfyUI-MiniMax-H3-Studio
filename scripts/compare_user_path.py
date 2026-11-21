# -*- coding: utf-8 -*-
"""用户原路径逐步复现 —— 修前 vs 修后对照（验收清单第 6 项）。

【用户原话】2026-11-21
「移动端的人机交互流程有问题：选择某个生成类型后，就无法回到生成类型选择，
  也无法看到生成媒体库。」

【本脚本做的事】
严格按用户描述的顺序走一遍，每一步都记录**真实可见状态**：
  步骤 1  选类型      —— 点左侧动态项「文生视频」
  步骤 2  找返回      —— 想回到「生成类型选择」，看有没有入口
  步骤 3  看媒体库    —— 媒体库卡片是否在视口内、是否可点
  步骤 4  切另一类型  —— 再选一个别的类型
  步骤 5  收藏一张卡  —— 收藏入口是否可用

【关键：全部用真实 tap，不用 JS .click()】
JS .click() 绕过命中测试，能点中任何元素 —— 用它测「能不能回到类型选择」
毫无意义。上一轮矩阵的假阳性就是这么来的。

【两个实例】
  · before = http://127.0.0.1:8921  （ba33586，无移动端修复）
  · after  = http://127.0.0.1:8190  （当前线上 HEAD）
两个实例**用同一个真实数据库**，所以数据一致，只有前端不同。
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
  const mask = document.querySelector('#sidebarMask');
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
  // 「返回 / 关闭」类控件全量扫描
  const backish = [];
  for (const el of document.querySelectorAll(
      'button, a, [role=button], .nav-drawer-close, .nav-toggle')) {
    const label = (el.getAttribute('aria-label') || el.title || el.textContent || '').trim();
    if (!label) continue;
    if (/返回|关闭|退出|取消|选择生成类型|菜单|back|close/i.test(label)) {
      const b = el.getBoundingClientRect();
      backish.push({label: label.slice(0, 30),
                    id: el.id || null,
                    cls: String(el.className || '').slice(0, 44),
                    visible: b.width > 0 && b.height > 0,
                    rect: {t: Math.round(b.top), l: Math.round(b.left),
                            w: Math.round(b.width), h: Math.round(b.height)}});
    }
  }
  return {
    sidebarOpen: sb.classList.contains('open'),
    sidebarMaskOpen: mask.classList.contains('open'),
    panelOpen: !!(panel && panel.classList.contains('open')),
    activeMode: (document.querySelector('.nav-item.active, .nav-model-item.active') || {}).dataset
                 ? document.querySelector('.nav-item.active, .nav-model-item.active').dataset.mode
                 : null,
    cardsTotal: cards.length,
    cardsInViewport: inView.length,
    cardsClickable: clickable.length,
    mediaLibraryVisible: inView.length > 0,
    backishControls: backish,
    drawerTitle: (document.querySelector('.nav-drawer-title') || {}).textContent || null,
    favButtonOnCard: !!document.querySelector(
      '#jobsGrid .job-card [data-act="favorite-toggle"]'),
  };
}"""

STEP_TAP = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok:false, reason:'not-found'};
  const b = el.getBoundingClientRect();
  if (b.width===0||b.height===0) return {ok:false, reason:'zero-size'};
  const x = Math.round(b.left+b.width/2), y = Math.round(b.top+b.height/2);
  if (x<0||y<0||x>innerWidth||y>innerHeight) return {ok:false, reason:'offscreen'};
  const t = document.elementFromPoint(x,y);
  return {ok: t ? (t===el||el.contains(t)) : false,
          hitTag: t?t.tagName:null, hitId: t?t.id:null,
          hitCls: t?String(t.className||'').slice(0,50):null};
}"""


def real_tap(page, selector, timeout=7000):
    try:
        page.tap(selector, timeout=timeout)
        return {"ok": True, "method": "tap"}
    except Exception as e:
        diag = page.evaluate(STEP_TAP, selector)
        try:
            page.click(selector, timeout=timeout)
            return {"ok": True, "method": "click-fallback", "diag": diag}
        except Exception as e2:
            return {"ok": False, "method": "tap+click-failed",
                    "tap_error": str(e).split("\n")[0][:180],
                    "diag": diag}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="http://127.0.0.1:8921")
    ap.add_argument("--after", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "userpath"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
        for label, base in [("before", args.before), ("after", args.after)]:
            ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                      is_mobile=True, has_touch=True, device_scale_factor=2)
            page = ctx.new_page()
            r = {"base": base, "viewport": [args.width, args.height]}
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
            page.wait_for_timeout(2500)

            # ---- 步骤 0：初始态 ----
            r["step0_initial"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-0-initial.png"))

            # ---- 步骤 1：打开类型选择，选「文生视频」（动态项）----
            r["step1_open_picker"] = real_tap(page, "#navToggle")
            page.wait_for_timeout(900)
            r["step1_after_open"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-1-picker-open.png"))

            r["step1_select_t2v"] = real_tap(
                page, ".nav-model-item[data-mode='t2v'], .nav-item[data-mode='t2v']")
            page.wait_for_timeout(1200)
            r["step1_after_select"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-2-after-select-t2v.png"))

            # ---- 步骤 2：想「回到生成类型选择」----
            #    先看有没有明示的返回控件；再看点汉堡能不能回
            r["step2_backish"] = r["step1_after_select"]["backishControls"]
            r["step2_tap_hamburger"] = real_tap(page, "#navToggle")
            page.wait_for_timeout(900)
            r["step2_after_hamburger"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-3-back-to-picker.png"))
            r["step2_reached_picker"] = r["step2_after_hamburger"]["sidebarOpen"]

            # ---- 步骤 3：看媒体库 ----
            #    若抽屉还开着（修前就是这个状态），关掉它再看
            if r["step2_after_hamburger"]["sidebarOpen"]:
                r["step3_forced_close"] = real_tap(page, "#navDrawerClose")
                if not r["step3_forced_close"]["ok"]:
                    r["step3_forced_close"] = real_tap(page, "#sidebarMask")
                page.wait_for_timeout(900)
            r["step3_media_library"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-4-media-library.png"))

            # ---- 步骤 4：切另一类型 ----
            r["step4_open_picker"] = real_tap(page, "#navToggle")
            page.wait_for_timeout(900)
            r["step4_select_t2i"] = real_tap(page, ".nav-item[data-mode='t2i']")
            page.wait_for_timeout(1200)
            r["step4_after_select"] = page.evaluate(STATE_JS)
            page.screenshot(path=str(outdir / f"{label}-{args.width}-5-after-select-t2i.png"))

            # ---- 步骤 5：收藏一张卡 ----
            r["step5_has_fav_button"] = page.evaluate(
                "() => !!document.querySelector('#jobsGrid .job-card [data-act=\"favorite-toggle\"]')")
            r["step5_fav_tap"] = real_tap(
                page, "#jobsGrid .job-card [data-act='favorite-toggle']")
            page.wait_for_timeout(1200)
            r["step5_after"] = page.evaluate(
                """() => ({
                    badgeCount: document.querySelectorAll('#jobsGrid .job-fav-badge').length,
                    toast: (document.querySelector('.fav-toast, #favToast, .toast') || {}).textContent || null,
                })""")
            page.screenshot(path=str(outdir / f"{label}-{args.width}-6-after-favorite.png"))

            ctx.close()
            results[label] = r
        browser.close()

    # ---- 汇总对照表 ----
    rows = []
    def g(lbl, key, field="", default=None):
        node = results[lbl].get(key, {})
        # 顶层就是标量（如 step2_reached_picker 是 bool）
        if not isinstance(node, dict):
            return node if node is not None else default
        return node.get(field, default)

    rows.append(("① 选类型后抽屉是否自动收起",
                 "是" if not g("before", "step1_after_select", "sidebarOpen")
                     else "**否（抽屉盖住媒体库）**",
                 "是" if not g("after", "step1_after_select", "sidebarOpen")
                     else "**否**"))
    rows.append(("② 明示的返回/关闭控件数",
                 len(g("before", "step1_after_select", "backishControls") or []),
                 len(g("after", "step1_after_select", "backishControls") or [])))
    rows.append(("③ 点汉堡能否回到类型选择",
                 "OK" if g("before", "step2_reached_picker") else "**FAIL**",
                 "OK" if g("after", "step2_reached_picker") else "**FAIL**"))
    rows.append(("④ 媒体库卡片在视口内",
                 g("before", "step3_media_library", "cardsInViewport"),
                 g("after", "step3_media_library", "cardsInViewport")))
    rows.append(("④ 媒体库卡片可点（命中测试）",
                 g("before", "step3_media_library", "cardsClickable"),
                 g("after", "step3_media_library", "cardsClickable")))
    rows.append(("⑤ 切换另一类型",
                 "OK" if g("before", "step4_after_select", "activeMode") == "t2i" else "**FAIL**",
                 "OK" if g("after", "step4_after_select", "activeMode") == "t2i" else "**FAIL**"))
    rows.append(("⑥ 卡片上有收藏按钮",
                 "无" if not g("before", "step5_has_fav_button") else "有",
                 "无" if not g("after", "step5_has_fav_button") else "有"))
    rows.append(("⑥ 点收藏后出现 ★",
                 g("before", "step5_after", "badgeCount"),
                 g("after", "step5_after", "badgeCount")))

    lines = ["| 步骤 | 修前(8921 / ba33586) | 修后(8190 / 当前HEAD) |",
             "|---|---|---|"]
    for a, b, c in rows:
        lines.append(f"| {a} | {b} | {c} |")
    table = "\n".join(lines)

    (outdir / f"userpath-{args.width}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / f"userpath-{args.width}.md").write_text(
        f"# 用户原路径对照（{args.width}x{args.height}）\n\n{table}\n",
        encoding="utf-8")
    print(table)
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
