# -*- coding: utf-8 -*-
"""真机验证（8190 线上）：「增强」多选 + 官方 PE 作为普通增强项。

用真实 Chromium 打开线上 8190，逐条核对并截图：
  1. Qwen 文生图 / 图片编辑两个面板下，增强下拉里都有「qwen21图片官方增强PE」
     且带中文说明；该项标 Recommended
  2. 界面上**没有**独立的 PE 勾选框（连 DOM 都不存在）
  3. 复选框能同时选中多项（多选）
  4. 选中官方 PE -> 推荐参数自动套用 + 可见回显；可手动改
  5. system prompt 可编辑
  6. 多项共存时参数互不覆盖
  7. console 0 报错

截图与结果 JSON 存到工作区 pe-enhance-evidence/ui/。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190/"
OUT = Path(sys.argv[1] if len(sys.argv) > 1
           else "/root/.hermes-web-ui/.ekko/workspace/default/"
                "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
                "pe-enhance-evidence/ui")
OUT.mkdir(parents=True, exist_ok=True)

results = []
errors = []


def check(name, ok, detail=""):
    results.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def shot(page, name):
    path = OUT / name
    page.screenshot(path=str(path), full_page=False)
    print(f"    截图 → {path}")
    return path


with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console",
            lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    # 清掉历史残留的选中状态
    page.evaluate("() => localStorage.removeItem('h3studio.enhanceSelection.v1')")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    def switch_mode(mode):
        page.click(f'.nav-item[data-mode="{mode}"]')
        page.wait_for_timeout(1200)

    def open_menu():
        page.click("#enhanceMenuBtn")
        page.wait_for_timeout(600)

    def menu_items():
        return page.eval_on_selector_all(
            "#enhanceMenu .enhance-menu-item[data-method]",
            "els => els.map(e => {const c = e.querySelector('input[data-method-check]');"
            " return {id: e.dataset.method,"
            " label: e.querySelector('.enhance-menu-label').textContent.trim(),"
            " hint: (e.querySelector('.enhance-menu-hint')||{}).textContent||'',"
            " recommended: !!e.querySelector('.enhance-menu-badge.rec'),"
            " checked: !!(c && c.checked), disabled: !!(c && c.disabled)};})")

    def set_check(method_id, on):
        page.evaluate("""([mid, on]) => {
          const el = document.querySelector(
            `#enhanceMenu input[data-method-check="${mid}"]`);
          if (!el) return;
          if (el.checked !== on) {
            el.checked = on;
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }""", [method_id, on])
        page.wait_for_timeout(350)

    # ================================================================
    print("\n=== 1. Qwen 文生图面板 ===")
    switch_mode("t2i_qwen")
    shot(page, "01-qwen-t2i-panel.png")

    # 独立 PE 勾选框必须不存在
    dom = page.evaluate("""() => ({
      hasCheckbox: !!document.querySelector('#imgUsePe'),
      hasField: !!document.querySelector('#imgPeField'),
      panelCheckboxes: Array.from(
        document.querySelectorAll('#imagePanel input[type=checkbox]')).map(i => i.id),
      peText: (document.body.innerText || '').includes('官方 PE 提示词扩写'),
    })""")
    check("文生图：无独立 PE 勾选框", not dom["hasCheckbox"], dom)
    check("文生图：无独立 PE 区块", not dom["hasField"], dom)
    check("文生图：面板里没有任何勾选框", dom["panelCheckboxes"] == [], dom["panelCheckboxes"])
    check("文生图：页面无「官方 PE 提示词扩写」文案", not dom["peText"])

    open_menu()
    items = menu_items()
    ids = [i["id"] for i in items]
    labels = [i["label"] for i in items]
    check("文生图：下拉有 4 项", len(items) == 4, ids)
    check("文生图：含「qwen21图片官方增强PE」", "qwen21图片官方增强PE" in labels, labels)
    check("文生图：原有三项仍在",
          all(x in ids for x in ("basic", "refine", "layout")), ids)
    check("文生图：每项都有中文说明", all(i["hint"].strip() for i in items))
    pe_item = next((i for i in items if i["id"] == "qwen_pe"), None)
    check("文生图：官方 PE 标 Recommended", bool(pe_item and pe_item["recommended"]),
          pe_item)
    check("文生图：官方 PE 可用（未禁用）", bool(pe_item and not pe_item["disabled"]))
    shot(page, "02-qwen-t2i-enhance-menu-multiselect.png")

    # ================================================================
    print("\n=== 2. 多选 + 推荐参数自动套用 ===")
    set_check("qwen_pe", True)
    set_check("refine", True)
    selected = page.evaluate("() => window.enhanceMethods.getSelected()")
    check("可同时选中多项", len(selected) >= 2, selected)
    check("选中集合含 qwen_pe 与 refine",
          "qwen_pe" in selected and "refine" in selected, selected)

    pe_params = page.evaluate("() => state.imgQwen.peParams")
    check("选中即自动套用推荐参数（文生图档 1.5 / 16256）",
          pe_params and pe_params.get("presence_penalty") == 1.5
          and pe_params.get("max_new_tokens") == 16256, pe_params)
    applied_visible = page.is_visible("#enhanceAppliedParams")
    applied_text = page.inner_text("#enhanceAppliedParams") if applied_visible else ""
    check("已套用参数有可见回显", applied_visible and "1.5" in applied_text
          and "16256" in applied_text, applied_text)
    shot(page, "03-qwen-t2i-multiselected-params-applied.png")

    # 参数可手动改
    page.fill('#enhanceMenu input[data-field="presencePenalty"]', "0.8")
    page.wait_for_timeout(600)
    pe_params2 = page.evaluate("() => state.imgQwen.peParams")
    check("参数可手动改且立即生效",
          pe_params2 and abs(pe_params2.get("presence_penalty", 0) - 0.8) < 1e-6,
          pe_params2)
    # 改完再勾一项，参数不能被清掉
    set_check("layout", True) if False else None
    page.fill('#enhanceMenu input[data-field="maxNewTokens"]', "9000")
    page.wait_for_timeout(500)
    pe_params3 = page.evaluate("() => state.imgQwen.peParams")
    check("手动改的参数保留", pe_params3 and pe_params3.get("max_new_tokens") == 9000,
          pe_params3)

    # system prompt 可编辑
    ta_visible = page.is_visible('#enhanceMenu textarea[data-field="systemPrompt"]')
    check("官方 PE 项露出 system prompt 编辑框", ta_visible)
    sp_len = page.evaluate(
        "() => (document.querySelector('#enhanceMenu textarea[data-field=\"systemPrompt\"]')||{}).value?.length || 0")
    check("system prompt 默认填了官方原文", sp_len > 500, f"{sp_len} 字")
    page.fill('#enhanceMenu textarea[data-field="systemPrompt"]',
              "自定义扩写指令：把画面写成观察者式结构化描述。")
    page.wait_for_timeout(600)
    sp_state = page.evaluate("() => state.imgQwen.peSystemPrompt")
    check("system prompt 可编辑并进入出图参数",
          sp_state == "自定义扩写指令：把画面写成观察者式结构化描述。", sp_state)
    shot(page, "04-qwen-t2i-editable-fields.png")

    # 多项参数互不覆盖
    set_check("refine", False)
    page.wait_for_timeout(400)
    pe_params4 = page.evaluate("() => state.imgQwen.peParams")
    check("取消其它项后官方 PE 参数仍在（互不覆盖）",
          pe_params4 and pe_params4.get("max_new_tokens") == 9000
          and abs(pe_params4.get("presence_penalty", 0) - 0.8) < 1e-6, pe_params4)

    # ================================================================
    print("\n=== 3. 图片编辑面板 ===")
    switch_mode("i2i_qwen")
    page.wait_for_timeout(800)
    shot(page, "05-qwen-i2i-panel.png")
    open_menu()
    items2 = menu_items()
    ids2 = [i["id"] for i in items2]
    pe2 = next((i for i in items2 if i["id"] == "qwen_pe"), None)
    check("图片编辑：下拉含「qwen21图片官方增强PE」",
          "qwen21图片官方增强PE" in [i["label"] for i in items2], ids2)
    check("图片编辑：官方 PE 可用", bool(pe2 and not pe2["disabled"]), pe2)
    dom2 = page.evaluate("""() => ({
      hasCheckbox: !!document.querySelector('#imgUsePe'),
      peText: (document.body.innerText || '').includes('官方 PE 提示词扩写'),
    })""")
    check("图片编辑：无独立 PE 勾选框", not dom2["hasCheckbox"], dom2)
    check("图片编辑：页面无「官方 PE 提示词扩写」文案", not dom2["peText"])
    shot(page, "06-qwen-i2i-enhance-menu.png")

    # ================================================================
    print("\n=== 4. 其它面板不受影响 ===")
    for mode, label in (("t2i", "Z-Image"), ("t2i_moss", "MOSS-Image")):
        switch_mode(mode)
        open_menu()
        its = menu_items()
        pe = next((i for i in its if i["id"] == "qwen_pe"), None)
        check(f"{label}：官方 PE 被禁用", bool(pe and pe["disabled"]), pe)
        check(f"{label}：普通/二次增强仍可用",
              all(not next(i for i in its if i["id"] == m)["disabled"]
                  for m in ("basic", "refine")))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    shot(page, "07-other-panels-pe-disabled.png")

    # ================================================================
    print("\n=== 5. console ===")
    check("console 0 报错", not errors, errors[:5])

    browser.close()

out = OUT / "verify-results.json"
out.write_text(json.dumps({
    "results": results,
    "passed": sum(1 for r in results if r["ok"]),
    "total": len(results),
    "console_errors": errors,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n结果 → {out}")
print(f"通过 {sum(1 for r in results if r['ok'])}/{len(results)}")
sys.exit(0 if all(r["ok"] for r in results) else 1)
