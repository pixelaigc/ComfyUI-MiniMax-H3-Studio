#!/usr/bin/env python3
"""真机验证：模板选择收进增强菜单项内，面板里没有第二个模板下拉。

【验证什么】
  V1 面板里不存在平行的模板行 / 模板下拉（#enhanceSpecRow /
     #enhanceSpecSelect / #musicEnhanceSpecRow / #musicEnhanceSpecSelect）
  V2 增强菜单项内部有模板选择器（select[data-spec-method]），
     多档时是下拉、单档时是名字标签
  V3 模板切换真的生效（改值 -> 状态改变 -> 当前方式回显跟着变）
  V4 音乐面板同样只有一个增强入口 + 一个下拉 + 当前方式回显
  V5 整个过程零 console 报错 / 零非 ERR_ABORTED 的失败请求

用法：
  python3 tests/verify_spec_picker_in_menu.py --base http://127.0.0.1:8195
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

DEAD_IDS = ["#enhanceSpecRow", "#enhanceSpecSelect",
            "#musicEnhanceSpecRow", "#musicEnhanceSpecSelect"]


def wait_ready(page, timeout_ms=45000):
    page.wait_for_function(
        """() => {
          try {
            if (typeof provState === 'undefined' || !provState) return false;
            const mf = provState.modeFilters;
            if (!mf || typeof mf !== 'object') return false;
            if (!Object.keys(mf).length) return false;
            return !!document.querySelector('#musicPrompt');
          } catch (e) { return false; }
        }""", timeout=timeout_ms)
    page.wait_for_timeout(800)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8195")
    ap.add_argument("--out", default="/tmp/spec_picker_check")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    failures, checks = [], []

    def check(ok, label, detail=""):
        checks.append({"ok": bool(ok), "label": label, "detail": str(detail)[:300]})
        if not ok:
            failures.append(f"{label} :: {detail}")
        print(("PASS " if ok else "FAIL ") + label +
              ("" if ok else " :: " + str(detail)[:250]))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors, failed = [], []
        page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url} :: {r.failure}"))

        page.goto(args.base, wait_until="domcontentloaded", timeout=45000)
        wait_ready(page)

        # ============ 图像面板（t2i）============
        page.evaluate("() => { const el = document.querySelector"
                      "(`.nav-item[data-mode='t2i']`); if (el) el.click(); }")
        time.sleep(1.0)
        page.evaluate("() => { const b = document.querySelector('#enhanceMenuBtn');"
                      " if (b) b.click(); }")
        time.sleep(0.8)

        img = page.evaluate("""
            (dead) => {
              const deadFound = dead.filter(s => !!document.querySelector(s));
              const inMenu = Array.from(
                document.querySelectorAll('#enhanceMenu select[data-spec-method]'));
              const tags = Array.from(
                document.querySelectorAll('#enhanceMenu .enhance-menu-spec'));
              // 面板内所有 select（排除增强菜单内部）
              const panel = document.querySelector('#panel') || document;
              const panelSelects = Array.from(panel.querySelectorAll('select'))
                .filter(s => !s.closest('#enhanceMenu'))
                .map(s => s.id);
              return {
                dead_found: deadFound,
                in_menu_selects: inMenu.length,
                in_menu_options: inMenu.map(s => Array.from(s.options).map(o => o.value)),
                in_menu_values: inMenu.map(s => s.value),
                in_menu_methods: inMenu.map(s => s.dataset.specMethod),
                spec_tags: tags.length,
                panel_select_ids: panelSelects,
                menu_open: !document.querySelector('#enhanceMenu').hidden,
              };
            }
        """, DEAD_IDS)

        check(not img["dead_found"],
              "V1 图像面板无并行模板行/下拉", json.dumps(img["dead_found"], ensure_ascii=False))
        check(img["menu_open"], "V1 增强方式下拉已打开")
        check(img["in_menu_selects"] > 0 or img["spec_tags"] > 0,
              "V2 模板选择器在增强菜单项内",
              f"selects={img['in_menu_selects']} tags={img['spec_tags']}")

        # ============ V3 模板切换真的生效 ============
        before = page.evaluate("() => window.enhanceMethods.getSelectedSpecByMethod()")
        pick = page.evaluate("""
            () => {
              const sel = document.querySelector('#enhanceMenu select[data-spec-method]');
              if (!sel || sel.options.length < 2) return {skipped: true};
              const opt = Array.from(sel.options).find(o => !o.selected);
              if (!opt) return {skipped: true};
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return {value: opt.value, method: sel.dataset.specMethod};
            }
        """)
        if pick.get("skipped"):
            check(True, "V3 当前上下文只有一个模板，跳过切换验证")
        else:
            time.sleep(0.7)
            after = page.evaluate("() => window.enhanceMethods.getSelectedSpecByMethod()")
            check(after != before,
                  "V3 改模板后状态改变",
                  f"before={before} after={after} picked={pick}")
            check(any(pick["value"] in str(v) for v in after.values()),
                  "V3 新模板被记录到对应增强方式",
                  f"picked={pick['value']} state={after}")

        page.screenshot(path=os.path.join(args.out, "image-panel-menu.png"))

        # ============ 音乐面板（t2a）============
        page.evaluate("() => { const m = document.querySelector('#enhanceMenu');"
                      " if (m) m.hidden = true; }")
        page.evaluate("() => { const el = document.querySelector"
                      "(`.nav-item[data-mode='t2a']`); if (el) el.click(); }")
        time.sleep(1.2)
        page.evaluate("() => { const b = document.querySelector('#musicEnhanceMenuBtn');"
                      " if (b) b.click(); }")
        time.sleep(0.8)

        mus = page.evaluate("""
            (dead) => {
              const deadFound = dead.filter(s => !!document.querySelector(s));
              const panel = document.querySelector('#panel') || document;
              const panelSelects = Array.from(panel.querySelectorAll('select'))
                .filter(s => !s.closest('#musicEnhanceMenu'))
                .map(s => s.id);
              const menu = document.querySelector('#musicEnhanceMenu');
              const items = menu ? Array.from(
                menu.querySelectorAll('.enhance-menu-item')).map(i => ({
                  text: (i.textContent || '').trim().slice(0, 60),
                  unavailable: (i.className||'').includes('unavailable'),
                })) : [];
              const cur = document.querySelector('#musicEnhanceCurrent');
              const btn = document.querySelector('#musicEnhanceBtn');
              const caret = document.querySelector('#musicEnhanceMenuBtn');
              return {
                dead_found: deadFound,
                panel_select_ids: panelSelects,
                menu_items: items,
                current_text: cur ? (cur.textContent||'').trim() : null,
                current_visible: !!(cur && !cur.hidden && cur.offsetParent !== null),
                btn_text: btn ? (btn.textContent||'').trim() : null,
                has_caret: !!caret,
                has_mode_switch: !!document.querySelector('#musicModeChips, #musicModeNote'),
                has_duration: !!document.querySelector('#musicDuration'),
              };
            }
        """, DEAD_IDS)

        check(not mus["dead_found"],
              "V4 音乐面板无并行模板行/下拉",
              json.dumps(mus["dead_found"], ensure_ascii=False))
        # 音乐面板的参数区不应再有「模板」下拉
        check(not any("Spec" in i for i in mus["panel_select_ids"]),
              "V4 音乐面板参数区无模板下拉",
              json.dumps(mus["panel_select_ids"], ensure_ascii=False))
        check(mus["current_visible"] and mus["current_text"],
              "V4 音乐面板有「当前增强方式」回显",
              json.dumps(mus["current_text"], ensure_ascii=False))
        check(bool(mus["btn_text"]) and mus["has_caret"],
              "V4 音乐面板只有一个增强按钮 + 一个下拉",
              f"btn={mus['btn_text']!r} caret={mus['has_caret']}")
        check(not mus["has_mode_switch"], "V4 音乐面板无生成模式切换")
        check(not mus["has_duration"], "V4 音乐面板无最长时长控件")
        check(any("YuE2" in i["text"] for i in mus["menu_items"]),
              "V4 下拉里有 yue2 专用增强项",
              json.dumps(mus["menu_items"], ensure_ascii=False)[:300])

        page.screenshot(path=os.path.join(args.out, "music-panel-menu.png"))

        real_failures = [f for f in failed if "ERR_ABORTED" not in f]
        check(not real_failures, "V5 零非 ERR_ABORTED 的失败请求",
              json.dumps(real_failures[:5], ensure_ascii=False))
        check(not errors, "V5 零 console 报错",
              json.dumps(errors[:5], ensure_ascii=False))

        ctx.close()
        browser.close()

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"checks": checks, "image": img, "music": mus,
                   "console_errors": errors,
                   "failed_requests": failed}, f, ensure_ascii=False, indent=2)

    print(f"\n=== 断言 {len(checks)-len(failures)} 通过 / {len(failures)} 失败 ===")
    for f in failures:
        print("  FAIL:", f)
    print("证据:", os.path.join(args.out, "report.json"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
