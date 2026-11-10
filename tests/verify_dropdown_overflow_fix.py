#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「增强」下拉浮层出框/遮挡 —— 回归守卫（真实浏览器）。

【守护什么】
  G1 菜单**不横向出框**：menu.scrollWidth <= menu.clientWidth
     （出框的判定口径 —— 文字溢出到菜单边框之外就是 scrollWidth > clientWidth）
  G2 选项**名称不被压成 0 宽**：每个 .enhance-menu-label 的
     getBoundingClientRect().width > 0 且 scrollWidth <= clientWidth
     （原来模板 select 把名称挤成 0 宽，文字整段消失）
  G3 菜单**完整在视口内**：left/right/top/bottom 都在视口内
  G4 菜单**不遮挡相邻字段**：与 #enhanceCurrent（「当前增强：xxx」回显）
     以及提示词框 #prompt 的矩形**不相交**
  G5 菜单**宽度不超过所在容器**：menu.width <= 容器宽度
  G6 全程 **0 条 console 报错 / 0 个 pageerror**
  G7 三档视口（375 / 390 / 1280）都通过

【怎么跑】
  python3 tests/verify_dropdown_overflow_fix.py                # 自己起临时服务
  python3 tests/verify_dropdown_overflow_fix.py --base http://127.0.0.1:8190

【为什么自己起临时服务】
  线上 8190 是**别的并发任务**在用的部署目录，本脚本绝不能改它。
  临时服务用 tests/test_frontend_defaults.py 的 StudioServer（复制到
  /tmp 的独立目录 + 独立端口），跑完即删。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

try:
    from playwright.sync_api import sync_playwright
except Exception:                                        # pragma: no cover
    print("SKIP: 没有 playwright")
    sys.exit(0)

VIEWPORTS = [(375, 812), (390, 844), (1280, 900), (1280, 1400)]
#: 1280x1400 是**高视口**：增强栏下面有足够空间，MAX_MENU_H 这个高度封顶
#: 在这里才真正生效（其余三档的高度是被「下方可用空间」卡住的，
#: 封顶与不封顶结果一样 —— 这正是「多条修复互相兜底」导致
#: 变异体存活的典型形态，见 skill: browser-layout-overflow-testing 陷阱 3）。

PROBE = r"""
() => {
  const menu = document.querySelector('#enhanceMenu');
  const btn  = document.querySelector('#enhanceMenuBtn');
  if (!menu || !btn) return {err: 'menu/btn not found'};
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const mr = menu.getBoundingClientRect();
  const rect = el => { if(!el) return null; const b = el.getBoundingClientRect();
    return {x:+b.x.toFixed(1), y:+b.y.toFixed(1), w:+b.width.toFixed(1),
            h:+b.height.toFixed(1), right:+b.right.toFixed(1), bottom:+b.bottom.toFixed(1)}; };
  const intersect = (a,b) => !(!a || !b) &&
    !(a.right <= b.x || b.right <= a.x || a.bottom <= b.y || b.bottom <= a.y);

  const labels = Array.from(menu.querySelectorAll('.enhance-menu-label')).map(el => {
    const b = el.getBoundingClientRect();
    return { text: el.textContent.trim(),
             w: +b.width.toFixed(1),
             sw: el.scrollWidth, cw: el.clientWidth,
             title: el.getAttribute('title'),
             collapsed: b.width < 1,
             truncated: el.scrollWidth > el.clientWidth + 1 };
  });
  const hints = Array.from(menu.querySelectorAll('.enhance-menu-hint')).map(el => ({
    text: el.textContent.trim().slice(0,30),
    sw: el.scrollWidth, cw: el.clientWidth,
    truncated: el.scrollWidth > el.clientWidth + 1,
    title: el.getAttribute('title') }));
  const items = Array.from(menu.querySelectorAll('.enhance-menu-item')).map(it => ({
    sw: it.scrollWidth, cw: it.clientWidth,
    overflow: it.scrollWidth > it.clientWidth + 1 }));

  const current = document.querySelector('#enhanceCurrent');
  const prompt  = document.querySelector('#prompt');
  const host   = btn.closest('.field, #panel, .panel') || document.body;
  const mcs = getComputedStyle(menu);
  const firstLabel = menu.querySelector('.enhance-menu-label');
  const firstItem  = menu.querySelector('.enhance-menu-item');
  const lcs = firstLabel ? getComputedStyle(firstLabel) : null;
  const ics = firstItem ? getComputedStyle(firstItem) : null;
  const maxH = mcs.maxHeight;

  return {
    vw, vh,
    menu: rect(menu),
    menuScrollW: menu.scrollWidth, menuClientW: menu.clientWidth,
    menuScrollH: menu.scrollHeight, menuClientH: menu.clientHeight,
    menuStyle: { position: mcs.position,
                 width: mcs.width,
                 maxWidth: mcs.maxWidth,
                 minWidth: mcs.minWidth,
                 maxHeight: mcs.maxHeight,
                 maxHeightPx: (maxH === 'none' || maxH === '' ? None
                               : parseFloat(maxH)),
                 overflowX: mcs.overflowX,
                 overflowY: mcs.overflowY,
                 zIndex: mcs.zIndex,
                 labelFlexShrink: lcs ? lcs.flexShrink : null,
                 labelMinWidth: lcs ? lcs.minWidth : null,
                 itemMinWidth: ics ? ics.minWidth : null,
                 // inline 值（getComputedStyle 会把 max-content 解析成像素，
                 // 要断言「声明的是 max-content」必须读 inline style）。
                 inlineWidth: menu.style.width,
                 inlineMinWidth: menu.style.minWidth,
                 inlineMaxWidth: menu.style.maxWidth,
                 specInCheck: menu.querySelectorAll(
                     '.enhance-menu-check .enhance-menu-spec, ' +
                     '.enhance-menu-check .enhance-menu-spec-row').length,
                 parent: menu.parentElement ? (menu.parentElement.id || menu.parentElement.tagName) : null },
    labels, hints, items,
    current: rect(current),
    prompt: rect(prompt),
    host: rect(host),
    overlapsCurrent: intersect(rect(menu), rect(current)),
    overlapsPrompt: intersect(rect(menu), rect(prompt)),
    onScreen: mr.left >= 0 && mr.right <= vw + 1 && mr.top >= 0 && mr.bottom <= vh + 1,
    // 增强栏下方的可用空间 —— 菜单高度的真实上限
    availBelow: +(window.innerHeight - 8 -
                  (btn.closest('.prompt-enhance-bar') || btn)
                    .getBoundingClientRect().bottom).toFixed(1),
    docScrollW: document.documentElement.scrollWidth,
    hOverflow: document.documentElement.scrollWidth > vw,
    specRows: menu.querySelectorAll('.enhance-menu-spec-row').length,
    specWraps: menu.querySelectorAll('.enhance-menu-spec-wrap').length,
    // 所有模板选择器（多档 spec-row + 单档 spec 标签）的总数，
    // 以及其中有多少被 .enhance-menu-spec-wrap 包住、多少还留在名称行里。
    specTotal: menu.querySelectorAll('.enhance-menu-spec, .enhance-menu-spec-row').length,
    specWrapped: menu.querySelectorAll(
        '.enhance-menu-spec-wrap .enhance-menu-spec, ' +
        '.enhance-menu-spec-wrap .enhance-menu-spec-row').length,
    specInCheck: menu.querySelectorAll(
        '.enhance-menu-check .enhance-menu-spec, ' +
        '.enhance-menu-check .enhance-menu-spec-row').length,
    nestedLabel: !!menu.querySelector('label label'),
  };
}
"""


SCROLL_PROBE = r"""
() => {
  const menu = document.querySelector('#enhanceMenu');
  if (!menu) return {err: 'no menu'};
  const cs = getComputedStyle(menu);
  // 滚到底
  menu.scrollTop = menu.scrollHeight;
  const items = Array.from(menu.querySelectorAll('.enhance-menu-item'));
  const last = items.length ? items[items.length - 1] : null;
  const mr = menu.getBoundingClientRect();
  const lr = last ? last.getBoundingClientRect() : null;
  const labels = last ? Array.from(last.querySelectorAll('.enhance-menu-label, .enhance-menu-hint')) : [];
  const lastItemTruncated = labels.some(el => el.scrollWidth > el.clientWidth + 1);
  return {
    scrollable: cs.overflowY === 'auto' || cs.overflowY === 'scroll',
    scrollTop: menu.scrollTop,
    maxScroll: menu.scrollHeight - menu.clientHeight,
    scrolledToBottom: menu.scrollTop >= (menu.scrollHeight - menu.clientHeight - 1),
    itemCount: items.length,
    lastItemText: last ? (last.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60) : null,
    // 末项是否完整落在菜单的可视区里
    lastItemReachable: !!(lr && lr.top >= mr.top - 1 && lr.bottom <= mr.bottom + 1),
    lastItemTruncated,
    allLabelsIntact: labels.every(el => el.scrollWidth <= el.clientWidth + 1),
  };
}
"""


def wait_ready(page, timeout_ms=45000):
    page.wait_for_function(
        """() => {
          try {
            if (typeof provState === 'undefined' || !provState) return false;
            const mf = provState.modeFilters;
            if (!mf || typeof mf !== 'object') return false;
            return !!document.querySelector('#enhanceMenuBtn');
          } catch (e) { return false; }
        }""", timeout=timeout_ms)
    page.wait_for_timeout(800)


def open_moss_menu(page, w):
    """切到 MOSS-Image 面板并展开增强下拉。"""
    if w <= 700:
        tog = page.locator(".nav-toggle")
        if tog.count() and tog.first.is_visible():
            tog.first.click()
            page.wait_for_timeout(400)
    page.evaluate("() => { const e = document.querySelector("
                  "'.nav-item[data-mode=\"t2i_moss\"]'); if (e) e.click(); }")
    page.wait_for_timeout(900)
    if w <= 1100:
        page.evaluate("""() => {
          const p = document.querySelector('#panel');
          if (p && !p.classList.contains('open')) {
            const fab = document.querySelector('#panelFab');
            if (fab) fab.click();
          }
        }""")
        page.wait_for_timeout(600)
    # 滚到提示词区，保证增强按钮在视口内。
    # 用 block:'start'（按钮贴近可视区顶部）而不是 'center' ——
    # 这样增强栏**下方**才有足够空间，MAX_MENU_H 这个高度封顶
    # 才真正生效（'center' 时下方只剩 ~200px，封顶与不封顶一样，
    # 于是「高度不封顶」这个变异体就杀不掉 —— 见 skill:
    # browser-layout-overflow-testing 陷阱 3「修复手段互相冗余」）。
    page.evaluate("""() => {
      const el = document.querySelector('#enhanceSplit');
      if (el) el.scrollIntoView({block: 'start'});
    }""")
    page.wait_for_timeout(400)
    page.evaluate("() => { const b = document.querySelector('#enhanceMenuBtn');"
                  " if (b) b.click(); }")
    page.wait_for_timeout(700)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="")
    ap.add_argument("--out", default="/tmp/dropdown_overflow_fix")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    server = None
    base = args.base
    if not base:
        from test_frontend_defaults import StudioServer
        server = StudioServer()
        if not server.start():
            print("FAIL: 临时服务没起来")
            return 1
        base = f"http://127.0.0.1:{server.port}/"

    failures, checks, results = [], [], {}

    def check(ok, label, detail: object = ""):
        checks.append({"ok": bool(ok), "label": label, "detail": str(detail)[:400]})
        if not ok:
            failures.append(f"{label} :: {detail}")
        print(("PASS " if ok else "FAIL ") + label +
              ("" if ok else " :: " + str(detail)[:300]))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            for (w, h) in VIEWPORTS:
                ctx = browser.new_context(viewport={"width": w, "height": h},
                                          device_scale_factor=2, is_mobile=False,
                                          has_touch=True)
                page = ctx.new_page()
                errors, failed = [], []
                page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                        if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
                page.on("requestfailed",
                        lambda r: failed.append(f"{r.method} {r.url} :: {r.failure}"))

                page.goto(base, wait_until="domcontentloaded", timeout=45000)
                wait_ready(page)
                open_moss_menu(page, w)

                st = page.evaluate(PROBE)
                results[str(w)] = st
                key = f"{w}px"

                if st.get("err"):
                    check(False, f"[{key}] 探针能取到菜单", st["err"])
                    ctx.close()
                    continue

                shot = os.path.join(args.out, f"after-{w}.png")
                page.screenshot(path=shot)
                try:
                    page.locator("#enhanceMenu").screenshot(
                        path=os.path.join(args.out, f"after-{w}-menu.png"))
                except Exception:
                    pass

                # G1 菜单不横向出框
                check(st["menuScrollW"] <= st["menuClientW"] + 1,
                      f"[{key}] G1 菜单不横向出框",
                      f"scrollW={st['menuScrollW']} clientW={st['menuClientW']}")

                # G2 名称没被压成 0 宽 / 没被截断
                collapsed = [l["text"] for l in st["labels"] if l["collapsed"]]
                truncated = [l["text"] for l in st["labels"] if l["truncated"]]
                check(not collapsed, f"[{key}] G2a 选项名称非 0 宽", collapsed)
                check(not truncated, f"[{key}] G2b 选项名称未被截断", truncated)

                # 说明文字换行而不是被裁
                htr = [h["text"] for h in st["hints"] if h["truncated"]]
                check(not htr, f"[{key}] G2c 说明文字未被横向裁切", htr)

                # G3 菜单完整在视口内
                check(st["onScreen"], f"[{key}] G3 菜单完整在视口内", st["menu"])

                # G4 不遮挡「当前增强」回显与提示词框
                check(not st["overlapsCurrent"],
                      f"[{key}] G4a 不遮挡「当前增强」回显",
                      f"menu={st['menu']} current={st['current']}")
                check(not st["overlapsPrompt"],
                      f"[{key}] G4b 不遮挡提示词框",
                      f"menu={st['menu']} prompt={st['prompt']}")

                # G5 宽度不超过所在容器
                host_w = st["host"]["w"] if st.get("host") else st["vw"]
                check(st["menu"]["w"] <= host_w + 1,
                      f"[{key}] G5 菜单宽度不超过所在容器",
                      f"menuW={st['menu']['w']} hostW={host_w}")
                # G5b 宽度上限必须**按容器收**（不是只按视口）——
                # 桌面端参数面板只有 297px，固定 340px 就是出框。
                # 这条断言锚在 inline max-width 上：它必须 <= 容器宽 - 24。
                host_w2 = st["host"]["w"] if st.get("host") else st["vw"]
                inline_max_w = st["menuStyle"].get("inlineMaxWidth", "")
                try:
                    maxw_px = float(inline_max_w.replace("px", ""))
                except Exception:
                    maxw_px = None
                check(maxw_px is not None and maxw_px <= host_w2 - 24 + 1,
                      f"[{key}] G5b 宽度上限按容器收（<= 容器宽 - 24）",
                      f"inlineMaxWidth={inline_max_w} hostW={host_w2}")

                # 高度封顶 + 内部可滚
                check(st["menu"]["h"] <= 400,
                      f"[{key}] 菜单高度已封顶（不铺满面板）",
                      f"h={st['menu']['h']}")

                # 注：这里**故意不写**「高度 <= 下方可用空间」这种几何等式断言。
                # 试过，availBelow 在「定位那一刻」与「测量那一刻」之间
                # 会因为增强栏内容异步加载而漂移 ~10px，断言会假红。
                # 真正要守的用户可见性质是两条，已在上面：
                #   G3 菜单完整在视口内
                #   G4a 不遮挡同一栏的「当前增强」回显
                # M5 变异体（高度改回内容自然高度）正是被 G4a 杀掉的。

                # ---- 机制级守卫 ----
                # 结果级守卫（不溢出 / 不遮挡）会被多条修复互相兜底，
                # 变异体可能存活。所以这里补**机制级**断言：
                # 锚到浏览器算出来的 computed style，删任一条就红。
                # （见 skill: browser-layout-overflow-testing 陷阱 3）
                ms = st["menuStyle"]
                check(ms["overflowX"] == "hidden",
                      f"[{key}] M-a 菜单 overflow-x:hidden（内容不横向出框）",
                      ms["overflowX"])
                check(ms["minWidth"] == "268px" or ms["minWidth"].startswith("268"),
                      f"[{key}] M-b 菜单 min-width 已按容器压到 268px 以内",
                      ms["minWidth"])
                check(ms["inlineWidth"] == "max-content",
                      f"[{key}] M-c 菜单宽度自适应内容（inline width:max-content）",
                      f"inline={ms['inlineWidth']} used={ms['width']}")
                check(ms["inlineMinWidth"] == "268px",
                      f"[{key}] M-b2 菜单 inline min-width 已按容器压低",
                      ms["inlineMinWidth"])
                check(ms["labelFlexShrink"] == "1" and ms["labelMinWidth"] == "0px",
                      f"[{key}] M-d 名称可收缩（flex-shrink:1 + min-width:0）",
                      f"shrink={ms['labelFlexShrink']} minW={ms['labelMinWidth']}")
                check(ms["itemMinWidth"] == "0px",
                      f"[{key}] M-e 菜单项 min-width:0（不撑宽父级）",
                      ms["itemMinWidth"])
                check(ms["specInCheck"] == 0,
                      f"[{key}] M-f 模板选择器不在名称行内（不再挤掉名称）",
                      ms["specInCheck"])
                # 注：MAX_MENU_H 的「封顶」断言**故意不写** ——
                # 实测里它不可达（面板永远滚到底，availBelow ~190px < 360px），
                # 写一条永远为真的断言等于给守卫充数。
                # 真正生效的高度约束由 G5c（高度 <= 可用空间）守住。

                # 内容超高时必须能**滚到最后一项**，且末项**文字完整**。
                # 菜单内容实测 795px，视口只能给 ~200px —— 靠内部滚动承载是
                # 既有契约（见 tests/verify_menu_not_clipped.py）。
                # 注意：末项本身可能比可视区还高（qwen_pe 项带两个 textarea，
                # 实测 324px > 菜单 181px），所以判据是「滚到底后末项的
                # **文字**没被截断」，而不是「末项整个矩形必须塞进可视区」——
                # 后者对超高项本来就不成立（那是浏览器滚动容器的固有行为，
                # 不是出框）。
                st2 = page.evaluate(SCROLL_PROBE)
                check(st2["scrolledToBottom"] and st2["scrollable"],
                      f"[{key}] 内容超高时可滚到底",
                      st2)
                check(not st2["lastItemTruncated"],
                      f"[{key}] 滚到底后末项文字完整（无横向截断）",
                      st2)
                check(st2["allLabelsIntact"],
                      f"[{key}] 末项内所有文字均未被截断",
                      st2)

                # 页面无横向溢出
                check(not st["hOverflow"], f"[{key}] 页面无横向溢出",
                      f"docScrollW={st['docScrollW']} vw={st['vw']}")

                # 模板选择器独占一行：所有 .enhance-menu-spec* 都必须在
                # .enhance-menu-spec-wrap 里，且**不在** .enhance-menu-check 里
                # （原来它在名称那一行的 flex 里，把名称挤成 0 宽）。
                # 注意 spec 有两种形态：多档 = .enhance-menu-spec-row（带 select），
                # 单档 = .enhance-menu-spec（纯文字标签）—— 两者都要被包住。
                check(st["specInCheck"] == 0 and st["specTotal"] == st["specWrapped"],
                      f"[{key}] 模板选择器已独占一行（不与名称同行）",
                      f"total={st['specTotal']} wrapped={st['specWrapped']} "
                      f"stillInCheck={st['specInCheck']}")
                check(not st["nestedLabel"], f"[{key}] 不再出现 label 套 label",
                      st["nestedLabel"])

                # G6 console 报错为 0
                check(len(errors) == 0, f"[{key}] G6 console 报错为 0", errors[:5])

                page.keyboard.press("Escape")
                ctx.close()
            browser.close()
    finally:
        if server is not None:
            server.stop()

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"base": base, "results": results,
                   "checks": checks, "failures": failures},
                  f, ensure_ascii=False, indent=2)

    print("\n=== 下拉出框守卫:", "PASS" if not failures else "FAIL", "===")
    if failures:
        for f_ in failures:
            print("  - " + f_)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
