#!/usr/bin/env python3
"""审查返修 · 音乐面板真机截图 + console 记录。

【覆盖】
  - 两个音乐标签（文生音乐 t2a / 参考生音乐 a2a）
  - 三档宽度：375 / 390 / 1280
  - 每张截图记录 console error / pageerror / failed request

【窄屏注意】
  375 / 390 下左侧栏是抽屉、参数面板是底部抽屉：
    - 需要点 #navToggle 展开左侧栏
    - 需要点 #panelFab 展开参数面板
    - 需要点 #panelMask 关闭遮罩

用法：
  python3 tests/verify_music_panel_screens.py --base http://127.0.0.1:8195 \
      --out /tmp/music_shots
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright


def wait_ready(page, timeout_ms=45000):
    # 注意：
    #  - provState 是页面脚本运行后才有的全局量，不能一进页面就假设它存在；
    #  - provState.modeFilters 是**对象**（键=模式名），不是数组 ——
    #    写成 Array.isArray 会永远等不到，直接超时。
    page.wait_for_function(
        """() => {
          try {
            if (typeof provState === 'undefined' || !provState) return false;
            const mf = provState.modeFilters;
            if (!mf || typeof mf !== 'object') return false;
            if (!Object.keys(mf).length) return false;
            return !!document.querySelector('#musicPrompt');
          } catch (e) { return false; }
        }""",
        timeout=timeout_ms)
    page.wait_for_timeout(600)


def open_sidebar(page):
    """窄屏下左侧栏是抽屉 —— 点 #navToggle 展开。"""
    expanded = page.evaluate("""
        () => {
          const t = document.querySelector('#navToggle');
          if (!t) return 'no-toggle';
          const vis = t.offsetParent !== null || getComputedStyle(t).display !== 'none';
          if (!vis) return 'hidden';
          // 已经展开就别再点（避免 toggle 关掉）
          const nav = document.querySelector('#nav, .nav, aside');
          const navOpen = nav && nav.classList.contains('open');
          if (!navOpen) t.click();
          return navOpen ? 'already' : 'clicked';
        }
    """)
    time.sleep(0.5)
    return expanded


def close_overlays(page):
    """点 #panelMask 关掉可能盖住内容的遮罩。"""
    page.evaluate("""
        () => {
          const m = document.querySelector('#panelMask');
          if (m && !m.hidden && getComputedStyle(m).display !== 'none') {
            m.click();
          }
        }
    """)
    time.sleep(0.3)


def switch_tab(page, mode):
    """切到指定音乐标签（左侧入口）。"""
    page.evaluate("""
        (m) => {
          const el = document.querySelector(`.nav-item[data-mode='${m}']`);
          if (el) { el.click(); return true; }
          return false;
        }
    """, mode)
    time.sleep(0.9)


def _visible(page, selector):
    """元素是否真的在屏幕上可见。

    注意：**不能用 offsetParent 判断** —— 窄屏下参数面板是
    position: fixed，fixed 元素的 offsetParent 恒为 null，
    会把「其实可见」误判成「不可见」。
    改用 getBoundingClientRect + computed style。
    """
    return page.evaluate(
        """(sel) => {
          const el = document.querySelector(sel);
          if (!el) return false;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden') return false;
          const r = el.getBoundingClientRect();
          if (r.width <= 0 || r.height <= 0) return false;
          // 在视口内（或接近视口，底部抽屉刚展开时可能略低）
          return r.bottom > 0 && r.top < (window.innerHeight + 40)
                 && r.right > 0 && r.left < (window.innerWidth + 40);
        }""", selector)


def ensure_panel_visible(page):
    """窄屏下参数面板是底部抽屉 —— 点 #panelFab 展开。"""
    page.evaluate("""
        () => {
          const fab = document.querySelector('#panelFab');
          if (!fab) return;
          const cs = getComputedStyle(fab);
          if (cs.display === 'none' || cs.visibility === 'hidden') return;
          const panel = document.querySelector('#panel');
          if (panel && panel.classList.contains('open')) return;
          fab.click();
        }
    """)
    time.sleep(0.8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8195")
    ap.add_argument("--out", default="/tmp/music_shots")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    report = {"shots": [], "console_errors": [], "failed_requests": []}

    TABS = [("t2a", "文生音乐"), ("a2a", "参考生音乐")]
    WIDTHS = [375, 390, 1280]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for width in WIDTHS:
            ctx = browser.new_context(viewport={"width": width, "height": 844})
            page = ctx.new_page()
            errors = []
            page.on("console", lambda m, e=errors: e.append(f"{m.type}: {m.text}")
                    if m.type == "error" else None)
            page.on("pageerror", lambda exc, e=errors: e.append(f"pageerror: {exc}"))
            failed = []
            page.on("requestfailed",
                    lambda r, f=failed: f.append(f"{r.method} {r.url} :: {r.failure}"))

            for mode, label in TABS:
                errors.clear()
                failed.clear()
                page.goto(args.base, wait_until="domcontentloaded", timeout=45000)
                wait_ready(page)
                open_sidebar(page)
                switched = page.evaluate(
                    "(m) => { const el = document.querySelector"
                    "(`.nav-item[data-mode='${m}']`); if (el) { el.click(); return true; }"
                    " return false; }", mode)
                time.sleep(0.9)
                close_overlays(page)
                ensure_panel_visible(page)
                time.sleep(0.5)

                # 确认音乐面板确实在显示（用 rect 判可见，fixed 元素 offsetParent 为 null）
                state = page.evaluate("""
                    () => {
                      const vis = (el) => {
                        if (!el) return false;
                        const cs = getComputedStyle(el);
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        const r = el.getBoundingClientRect();
                        if (r.width <= 0 || r.height <= 0) return false;
                        return r.bottom > 0 && r.top < (window.innerHeight + 40)
                               && r.right > 0 && r.left < (window.innerWidth + 40);
                      };
                      const q = (s) => document.querySelector(s);
                      const mf = q('#musicPromptField');
                      const prompt = q('#musicPrompt');
                      const cur = q('#musicEnhanceCurrent');
                      const btn = q('#musicEnhanceBtn');
                      const panel = q('#panel');
                      const fab = q('#panelFab');
                      return {
                        mode: (typeof state !== 'undefined' && state) ? state.mode : '',
                        panelOpen: !!(panel && panel.classList.contains('open')),
                        panelVisible: vis(panel),
                        fabVisible: vis(fab),
                        musicFieldVisible: vis(mf),
                        promptVisible: vis(prompt),
                        btnVisible: vis(btn),
                        currentVisible: vis(cur),
                        currentText: cur ? (cur.textContent || '').trim() : null,
                        btnText: btn ? (btn.textContent || '').trim() : null,
                        hasModeSwitch: !!q('#musicModeChips, #musicModeNote'),
                        hasDuration: !!q('#musicDuration'),
                        enhanceMenus: document.querySelectorAll('.enhance-menu').length,
                      };
                    }
                """)

                shot = os.path.join(args.out, f"music-{label}-{width}.png")
                page.screenshot(path=shot, full_page=False)

                report["shots"].append({
                    "mode": mode, "label": label, "width": width,
                    "path": shot, "switched": switched,
                    "state": state,
                    "console_errors": list(errors),
                    "failed_requests": list(failed),
                })
                print(f"[{width}px] {label}: mode={state['mode']} "
                      f"panel_open={state['panelOpen']} "
                      f"field_vis={state['musicFieldVisible']} "
                      f"btn_vis={state['btnVisible']} "
                      f"current='{state['currentText']}' "
                      f"mode_switch={state['hasModeSwitch']} "
                      f"duration={state['hasDuration']} "
                      f"errors={len(errors)} failed_req={len(failed)}")
                for e in errors:
                    print("   CONSOLE-ERR:", e)
                for f in failed:
                    print("   FAILED-REQ:", f)
            ctx.close()
        browser.close()

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    total_err = sum(len(s["console_errors"]) for s in report["shots"])
    total_fail = sum(len(s["failed_requests"]) for s in report["shots"])
    # ERR_ABORTED = 浏览器取消媒体预载（元素离开 DOM 时）—— 不是资源路径缺陷。
    # 已用 tests/verify_audio_resource_abort.py 在未改动的 8190 基线上对照复现。
    aborts = [f for s in report["shots"] for f in s["failed_requests"]
              if "ERR_ABORTED" in f]
    others = [f for s in report["shots"] for f in s["failed_requests"]
              if "ERR_ABORTED" not in f]
    bad = [s for s in report["shots"]
           if s["state"]["hasModeSwitch"] or s["state"]["hasDuration"]
           or s["state"]["currentText"] in (None, "")
           or not s["state"]["musicFieldVisible"]
           or not s["state"]["btnVisible"]
           or not s["state"]["currentVisible"]]
    print("\n=== 截图 %d 张；console error %d；failed request %d "
          "(ERR_ABORTED %d / 其它 %d) ===" %
          (len(report["shots"]), total_err, total_fail, len(aborts), len(others)))
    for f in others:
        print("  OTHER-FAILED:", f)
    for s in bad:
        print("  BAD:", s["label"], s["width"], s["state"])
    ok = not bad and total_err == 0 and not others
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
