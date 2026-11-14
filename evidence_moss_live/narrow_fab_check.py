# -*- coding: utf-8 -*-
"""窄屏 FAB 遮挡权威判定（打线上 8190，375px / 390px 两种视口）。

背景（缺陷根因）：
  .panel-fab 的 z-index 原来是 930，而抽屉本体 .panel = 950、遮罩 .panel-mask = 949
  —— FAB 比抽屉**低 20 层**。抽屉打开后 FAB 落在抽屉覆盖范围内，用户手指点 FAB
  中心，命中的是面板里的控件而不是 FAB 自己 -> 按钮点不动 -> 表现为
  「MOSS-Image 参数面板完全不可用」。

本脚本对每个视口宽度检查**两个状态**：
  A. 抽屉关闭：FAB 必须**可见且可点**（用户靠它打开参数面板）
  B. 抽屉打开：FAB 要么干净地隐藏（display:none / hidden），
     要么可见但 elementFromPoint 命中的是它自己（仍可点）

判定用 elementFromPoint（权威命中判定），不靠肉眼。
"""
import json
import os
import sys

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = os.path.dirname(os.path.abspath(__file__))

from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = """() => {
  const fab = document.getElementById('panelFab');
  const panel = document.querySelector('#panel');
  if (!fab) return {exists: false};
  const cs = getComputedStyle(fab);
  const r = fab.getBoundingClientRect();
  const visible = !!(fab.offsetWidth || fab.offsetHeight)
                  && cs.display !== 'none' && !fab.hidden;
  let hitTag = null, hitId = null, hitIsFab = null, hitRect = null;
  if (visible && r.width > 0 && r.height > 0) {
    const h = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    hitTag = h ? h.tagName : null;
    hitId = h ? (h.id || '') : null;
    hitIsFab = !!h && (h === fab || fab.contains(h));
    if (h) { const hr = h.getBoundingClientRect();
             hitRect = {x: Math.round(hr.left), y: Math.round(hr.top),
                        w: Math.round(hr.width), h: Math.round(hr.height)}; }
  }
  const pcs = panel ? getComputedStyle(panel) : null;
  const pr = panel ? panel.getBoundingClientRect() : null;
  return {
    exists: true,
    visible: visible,
    fabZIndex: cs.zIndex,
    fabDisplay: cs.display,
    fabHiddenAttr: !!fab.hidden,
    fabRect: {x: Math.round(r.left), y: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height)},
    panelZIndex: pcs ? pcs.zIndex : null,
    panelOpen: panel ? panel.classList.contains('open') : null,
    panelRect: pr ? {x: Math.round(pr.left), y: Math.round(pr.top),
                      w: Math.round(pr.width), h: Math.round(pr.height)} : null,
    hitTag: hitTag, hitId: hitId, hitIsFab: hitIsFab, hitRect: hitRect,
    viewport: {w: window.innerWidth, h: window.innerHeight},
  };
}"""


def main():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for width in (375, 390):
            page = browser.new_page(viewport={"width": width, "height": 720})
            page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # ---- A. 抽屉关闭状态 ----
            page.evaluate("""() => {
              const p = document.querySelector('#panel');
              if (p) p.classList.remove('open');
            }""")
            page.wait_for_timeout(600)
            closed = page.evaluate(PROBE)
            closed_ok = (closed.get("exists") and closed.get("visible")
                         and closed.get("hitIsFab"))
            results.append({"viewport": width, "state": "drawer-closed",
                            "ok": closed_ok, "probe": closed})
            print(f"[{'PASS' if closed_ok else 'FAIL'}] {width}px 抽屉关闭: "
                  f"FAB visible={closed.get('visible')} z={closed.get('fabZIndex')} "
                  f"hit={closed.get('hitTag')}#{closed.get('hitId')} "
                  f"hitIsFab={closed.get('hitIsFab')}")
            page.screenshot(path=os.path.join(OUT, f"06-narrow-{width}-closed.png"))

            # ---- A2. 真实点击 FAB 必须能打开抽屉 ----
            clicked_ok = False
            try:
                page.click("#panelFab")
                page.wait_for_timeout(900)
                st = page.evaluate("""() => {
                  const p = document.querySelector('#panel');
                  return p ? p.classList.contains('open') : false;
                }""")
                clicked_ok = bool(st)
            except Exception as exc:  # noqa: BLE001
                print("   click error:", exc)
            results.append({"viewport": width, "state": "click-fab-opens-drawer",
                            "ok": clicked_ok})
            print(f"[{'PASS' if clicked_ok else 'FAIL'}] {width}px 点 FAB 能打开抽屉: {clicked_ok}")

            # ---- B. 抽屉打开状态 ----
            open_probe = page.evaluate(PROBE)
            open_ok = bool(open_probe.get("exists")) and (
                (not open_probe.get("visible")) or open_probe.get("hitIsFab"))
            results.append({"viewport": width, "state": "drawer-open",
                            "ok": open_ok, "probe": open_probe})
            print(f"[{'PASS' if open_ok else 'FAIL'}] {width}px 抽屉打开: "
                  f"FAB visible={open_probe.get('visible')} "
                  f"hiddenAttr={open_probe.get('fabHiddenAttr')} "
                  f"display={open_probe.get('fabDisplay')} z={open_probe.get('fabZIndex')} "
                  f"drawerZ={open_probe.get('panelZIndex')} "
                  f"hit={open_probe.get('hitTag')}#{open_probe.get('hitId')} "
                  f"hitIsFab={open_probe.get('hitIsFab')}")
            page.screenshot(path=os.path.join(OUT, f"03-narrow-{width}-fab.png"))
            page.close()
        browser.close()

    out = {"base": BASE, "results": results,
           "all_pass": all(r["ok"] for r in results)}
    with open(os.path.join(OUT, "narrow_fab_check.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print()
    print("ALL_PASS =", out["all_pass"])
    return 0 if out["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
