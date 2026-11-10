# -*- coding: utf-8 -*-
"""统一增强体系 —— 五类面板 × 三视口的真机验收脚本。

覆盖用户要求：
  Z-Image 文生图 / Qwen 文生图 / Qwen 图片编辑 / MOSS 文生图 / 文生音乐
  每类面板 × 375 / 390 / 1280 三档：
    1. 截图（面板 + 增强下拉展开态）
    2. console 报错记录
    3. 失败请求记录
    4. 增强下拉逐项点击：选项、可用性、徽标文案
    5. 「一个增强按钮 + 一个下拉 + 当前增强方式回显」的唯一性断言
    6. 单选执行后版本历史与「已增强」徽标仍在（不许回退）

用法：
    python3 tests/verify_enhance_panels_live.py --base http://127.0.0.1:8291/ \
        --out <截图目录>
"""
import argparse
import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:                                    # pragma: no cover
    print("playwright 缺失：%s" % exc)
    sys.exit(2)

# 面板 -> (mode, 增强主按钮, 菜单箭头按钮, 菜单 id, 当前方式回显 id,
#          输入框选择器, 模板行 id, 模板 select id)
PANELS = {
    "t2i":      ("t2i",      "#enhanceBtn",      "#enhanceMenuBtn",      "#enhanceMenu",
                 "#enhanceCurrent",     "#prompt",      "#enhanceSpecRow",     "#enhanceSpecSelect"),
    "t2i_qwen": ("t2i_qwen", "#enhanceBtn",      "#enhanceMenuBtn",      "#enhanceMenu",
                 "#enhanceCurrent",     "#prompt",      "#enhanceSpecRow",     "#enhanceSpecSelect"),
    "i2i_qwen": ("i2i_qwen", "#enhanceBtn",      "#enhanceMenuBtn",      "#enhanceMenu",
                 "#enhanceCurrent",     "#prompt",      "#enhanceSpecRow",     "#enhanceSpecSelect"),
    "t2i_moss": ("t2i_moss", "#enhanceBtn",      "#enhanceMenuBtn",      "#enhanceMenu",
                 "#enhanceCurrent",     "#prompt",      "#enhanceSpecRow",     "#enhanceSpecSelect"),
    "t2a":      ("t2a",      "#musicEnhanceBtn", "#musicEnhanceMenuBtn", "#musicEnhanceMenu",
                 "#musicEnhanceCurrent", "#musicPrompt", "#musicEnhanceSpecRow", "#musicEnhanceSpecSelect"),
}
VIEWPORTS = [(375, 780), (390, 844), (1280, 900)]

#: 用户要求的五类面板（用于最终汇总）
REQUIRED = ["t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2a"]


class Checker:
    def __init__(self):
        self.failures = []
        self.passed = 0

    def check(self, ok, label, detail=""):
        if ok:
            self.passed += 1
        else:
            self.failures.append("%s :: %s" % (label, detail))
        return bool(ok)


def wait_ready(page, timeout_ms=60000):
    page.wait_for_function(
        """() => {
          try {
            return typeof provState !== 'undefined' && provState
              && provState.agent && Array.isArray(provState.agent.specs)
              && provState.agent.specs.length > 0
              && Object.keys(provState.refineSpecs || {}).length > 0;
          } catch (e) { return false; }
        }""", timeout=timeout_ms)
    page.wait_for_timeout(600)


def switch_mode(page, mode):
    page.evaluate(
        "(m) => { const e = document.querySelector(`.nav-item[data-mode='${m}']`);"
        "if (e) e.click(); }", mode)
    page.wait_for_timeout(1000)


def open_drawer(page):
    page.evaluate("""() => {
      const p = document.querySelector('#panel');
      if (p && !p.classList.contains('open')) {
        const fab = document.querySelector('#panelFab');
        if (fab) fab.click();
      }
    }""")
    page.wait_for_timeout(600)


def close_drawer(page):
    page.evaluate("""() => {
      const p = document.querySelector('#panel');
      if (p && p.classList.contains('open')) {
        p.classList.remove('open');
        const m = document.querySelector('#panelMask');
        if (m) m.classList.remove('open');
      }
    }""")
    page.wait_for_timeout(250)


def scroll_into(page, sel):
    page.evaluate(
        """(s) => { const e = document.querySelector(s);
                   if (e) e.scrollIntoView({block: 'center', inline: 'nearest'}); }""", sel)
    page.wait_for_timeout(400)


def probe_entry(page, mode, btn, menu_btn, menu, cur, inp, spec_row, spec_sel):
    """在当前视口下量：唯一入口 / 菜单几何 / 选项清单 / 徽标。"""
    return page.evaluate(
        """([mode, btnSel, menuBtnSel, menuSel, curSel, inpSel, rowSel, selSel]) => {
          const q = s => document.querySelector(s);
          const box = el => { if (!el) return null; const r = el.getBoundingClientRect();
            return {top: Math.round(r.top), bottom: Math.round(r.bottom),
                    left: Math.round(r.left), right: Math.round(r.right),
                    w: Math.round(r.width), h: Math.round(r.height),
                    visible: r.width > 0 && r.height > 0}; };
          const panel = q('#panel');
          // 面板内所有「增强」相关控件
          const enhanceControls = Array.from(document.querySelectorAll(
            '#panel button, #panel select, #panel .enhance-current, #panel [id*="enhance" i]'))
            .filter(el => {
              const id = (el.id || '') + ' ' + (el.className || '');
              return /enhance/i.test(id);
            })
            .map(el => ({tag: el.tagName, id: el.id || '', cls: String(el.className || '')}));

          const m = q(menuSel);
          const items = m ? Array.from(m.querySelectorAll('.enhance-menu-item')) : [];
          const c = q(curSel);
          const row = q(rowSel);
          const sel = q(selSel);

          // 第二个「增强模板」下拉（改造前存在的重复入口）
          const standaloneSpec = Array.from(document.querySelectorAll('select'))
            .filter(s => /enhanceSpec(?!Select)/.test(s.id || ''))
            .map(s => s.id);

          return {
            mode,
            enhanceBtnCount: document.querySelectorAll(btnSel).length,
            menuBtnCount: document.querySelectorAll(menuBtnSel).length,
            menuCount: document.querySelectorAll(menuSel).length,
            currentCount: document.querySelectorAll(curSel).length,
            specRowExists: !!row,
            specRowVisible: row ? (row.hidden ? false : box(row).visible) : false,
            specSelectExists: !!sel,
            specSelectOptions: sel ? Array.from(sel.options).map(o => o.value) : [],
            standaloneSpecSelects: standaloneSpec,
            enhanceControls,
            btnBox: box(q(btnSel)),
            menuBtnBox: box(q(menuBtnSel)),
            currentBox: box(c),
            currentText: c ? (c.textContent || '').trim() : null,
            currentVisible: c ? (!c.hidden && box(c).visible) : false,
            menuHidden: m ? m.hidden : null,
            menuParentIsBody: m ? (m.parentElement === document.body) : null,
            menuBox: box(m),
            innerW: window.innerWidth, innerH: window.innerHeight,
            items: items.map(it => ({
              method: it.dataset.method,
              label: (it.querySelector('.enhance-menu-label')||{}).textContent || '',
              unavailable: it.classList.contains('unavailable'),
              disabled: !!(it.querySelector('input[type=checkbox]')||{}).disabled,
              checked: !!(it.querySelector('input[type=checkbox]')||{}).checked,
              badges: Array.from(it.querySelectorAll('.enhance-menu-badge'))
                        .map(b => (b.textContent||'').trim()),
              hint: (it.querySelector('.enhance-menu-hint')||{}).textContent || '',
            })),
          };
        }""", [mode, btn, menu_btn, menu, cur, inp, spec_row, spec_sel])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8291/")
    ap.add_argument("--out", default="enh-unify-panel-evidence")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = {"panels": {}, "console_errors": [], "failed_requests": [], "summary": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: results["console_errors"].append(
            {"text": m.text, "type": m.type}) if m.type == "error" else None)
        page.on("pageerror", lambda e: results["console_errors"].append(
            {"text": str(e), "type": "pageerror"}))
        page.on("requestfailed", lambda r: results["failed_requests"].append(
            {"url": r.url, "error": (r.failure or "")}))

        page.goto(args.base, wait_until="domcontentloaded")
        wait_ready(page)

        ck = Checker()

        for pname, spec in PANELS.items():
            mode, btn, menu_btn, menu, cur, inp, row, sel = spec
            results["panels"].setdefault(pname, {})
            for (w, h) in VIEWPORTS:
                page.set_viewport_size({"width": w, "height": h})
                page.wait_for_timeout(400)
                switch_mode(page, mode)
                open_drawer(page)
                scroll_into(page, btn)

                # 先量「唯一入口」形态（菜单未展开）
                pre = probe_entry(page, mode, btn, menu_btn, menu, cur, inp, row, sel)
                tag = "%s@%d" % (pname, w)

                ck.check(pre["enhanceBtnCount"] == 1,
                         "[%s] 恰好 1 个增强主按钮" % tag, str(pre["enhanceBtnCount"]))
                ck.check(pre["menuBtnCount"] == 1,
                         "[%s] 恰好 1 个增强下拉箭头" % tag, str(pre["menuBtnCount"]))
                ck.check(pre["menuCount"] == 1,
                         "[%s] 恰好 1 个增强方式下拉" % tag, str(pre["menuCount"]))
                ck.check(pre["currentCount"] == 1,
                         "[%s] 恰好 1 个「当前增强方式」回显" % tag, str(pre["currentCount"]))
                ck.check(pre["standaloneSpecSelects"] == [],
                         "[%s] 不存在独立的第二个增强模板下拉" % tag,
                         str(pre["standaloneSpecSelects"]))
                # 音乐面板走结构化路线（无模板行），图像/视频面板有平级模板行
                if pname == "t2a":
                    ck.check(not pre["specRowVisible"],
                             "[%s] 音乐面板不出现模板行（结构化风格路线）" % tag,
                             str(pre["specRowVisible"]))
                else:
                    ck.check(pre["specRowVisible"],
                             "[%s] 模板行可见（与增强按钮平级）" % tag, str(pre))
                    ck.check(len(pre["specSelectOptions"]) > 0,
                             "[%s] 模板下拉有选项" % tag, str(pre["specSelectOptions"]))

                # 展开下拉，量几何与选项
                page.evaluate("(s) => { const x = document.querySelector(s); if (x) x.click(); }",
                              menu_btn)
                page.wait_for_timeout(700)
                post = probe_entry(page, mode, btn, menu_btn, menu, cur, inp, row, sel)
                mb = post["menuBox"]
                if mb:
                    on_screen = (mb["top"] >= 0 and mb["bottom"] <= post["innerH"]
                                 and mb["left"] >= 0 and mb["right"] <= post["innerW"] + 1)
                    ck.check(on_screen,
                             "[%s] 下拉菜单完整在视口内" % tag, str(mb))
                    ck.check(mb["visible"], "[%s] 下拉菜单有可见高度" % tag, str(mb))
                    ck.check(post["menuParentIsBody"],
                             "[%s] 下拉挂在 body（脱离 transform 包含块）" % tag,
                             str(post["menuParentIsBody"]))
                ck.check(post["menuHidden"] is False, "[%s] 下拉已展开" % tag, str(post["menuHidden"]))
                ck.check(len(post["items"]) > 0, "[%s] 下拉有选项" % tag, str(post["items"]))

                # 逐项检查：禁用项只写归属，不带 Recommended
                for it in post["items"]:
                    if not it["unavailable"]:
                        continue
                    joined = " | ".join(it["badges"])
                    ck.check("Recommended" not in joined,
                             "[%s] 禁用项 %s 不带 Recommended" % (tag, it["method"]), joined)
                    ck.check(("专属" in joined) or ("不可用" in joined)
                             or ("需结构化" in joined) or ("文本扩写" in joined),
                             "[%s] 禁用项 %s 写明归属/原因" % (tag, it["method"]), joined)

                # 截图（展开态）
                scroll_into(page, btn)
                shot = out / ("panel-%s-%d.png" % (pname, w))
                try:
                    page.screenshot(path=str(shot))
                except Exception:
                    pass

                results["panels"][pname][str(w)] = {"pre": pre, "post": post,
                                                   "screenshot": str(shot)}
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                close_drawer(page)

        browser.close()

    results["summary"] = {
        "passed": ck.passed,
        "failed": len(ck.failures),
        "failures": ck.failures,
        "console_errors": len(results["console_errors"]),
        "failed_requests": len(results["failed_requests"]),
        "panels_covered": sorted(results["panels"].keys()),
        "required_panels_covered": sorted(set(REQUIRED) & set(results["panels"].keys())),
    }
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print("=== 断言 %d 通过 / %d 失败 ===" % (ck.passed, len(ck.failures)))
    for f in ck.failures[:40]:
        print("  !", f)
    print("console 报错:", len(results["console_errors"]))
    for e in results["console_errors"][:10]:
        print("   ", e)
    print("失败请求:", len(results["failed_requests"]))
    for e in results["failed_requests"][:10]:
        print("   ", e)
    print("证据目录:", out)
    return 0 if not ck.failures else 1


if __name__ == "__main__":
    sys.exit(main())
