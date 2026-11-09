#!/usr/bin/env python3
"""真机验证：8190 上的「增强」下拉 —— Qwen 官方 PE 扩写。

覆盖：
  1. Qwen 文生图面板：下拉里有「qwen21图片官方增强PE」
  2. Qwen 图片编辑面板：同上
  3. 两个面板都没有独立 PE 勾选框
  4. 点「增强」-> 官方 PE 真跑 -> 提示词框被官方 PE 扩写内容填充
  5. console 0 报错
截图存到工作区 qwen-pe-enhance-evidence/。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

EV = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
          "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/qwen-pe-enhance-evidence")
EV.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190/"

errors = []
results = []


def shot(page, name):
    path = EV / name
    page.screenshot(path=str(path), full_page=False)
    print(f"  [shot] {path}")
    return str(path)


def check(label, ok, detail=""):
    results.append({"label": label, "ok": bool(ok), "detail": str(detail)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def open_menu(page):
    page.click("#enhanceMenuBtn")
    page.wait_for_timeout(500)


def menu_items(page):
    """下拉项（多选形态：每项带复选框）。"""
    return page.eval_on_selector_all(
        "#enhanceMenu .enhance-menu-item[data-method]",
        "els => els.map(e => {const c = e.querySelector('input[data-method-check]');"
        " return {id: e.dataset.method,"
        " label: e.querySelector('.enhance-menu-label').textContent.trim(),"
        " hint: (e.querySelector('.enhance-menu-hint')||{}).textContent||'',"
        " checked: !!(c && c.checked), disabled: !!(c && c.disabled)};})")


def select_only(page, method_id):
    """只勾选指定增强项（多选语义：先勾目标，再取消其余）。"""
    page.click(f'#enhanceMenu input[data-method-check="{method_id}"]')
    page.wait_for_timeout(300)
    for mid in ("basic", "refine", "layout", "qwen_pe"):
        if mid == method_id:
            continue
        page.evaluate("""(mid) => {
          const el = document.querySelector(
            `#enhanceMenu input[data-method-check="${mid}"]`);
          if (el && el.checked && !el.disabled) {
            el.checked = false;
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }""", mid)
        page.wait_for_timeout(200)


with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console",
            lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)

    # ---------------------------------------------------------- 文生图面板
    print("\n=== 1. Qwen-Image 2.1 文生图面板 ===")
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="t2i_qwen"]')
    page.wait_for_timeout(1500)

    # 页面无独立 PE 勾选框
    dom = page.evaluate("""() => ({
      hasCheckbox: !!document.querySelector('#imgUsePe'),
      hasField: !!document.querySelector('#imgPeField'),
      hasTier: !!document.querySelector('#imgPeTier'),
      hasNote: !!document.querySelector('#imgPeNote'),
      bodyTextHasLabel: (document.body.innerText||'').includes('官方 PE 提示词扩写'),
      checkboxes: Array.from(
        document.querySelectorAll('#imagePanel input[type=checkbox]')).map(i=>i.id),
    })""")
    check("文生图：无独立 PE 勾选框", not dom["hasCheckbox"], f"checkbox={dom['hasCheckbox']}")
    check("文生图：无独立 PE 区块", not dom["hasField"], f"field={dom['hasField']}")
    check("文生图：页面无「官方 PE 提示词扩写」文案",
          not dom["bodyTextHasLabel"], "")
    shot(page, "01-qwen-t2i-panel-no-pe-checkbox.png")

    open_menu(page)
    items = menu_items(page)
    ids = [i["id"] for i in items]
    labels = [i["label"] for i in items]
    print("  下拉项:", json.dumps(items, ensure_ascii=False, indent=2))
    check("文生图：下拉有「qwen21图片官方增强PE」",
          "qwen21图片官方增强PE" in labels, str(labels))
    check("文生图：官方 PE 项可用（未被禁用）",
          any(i["id"] == "qwen_pe" and not i["disabled"] for i in items))
    check("文生图：原有三种增强方式仍在",
          all(x in ids for x in ("basic", "refine", "layout")), str(ids))
    check("文生图：每项都有简短说明",
          all(i["hint"].strip() for i in items))
    shot(page, "02-qwen-t2i-enhance-menu.png")

    # 选中官方 PE（多选：只勾这一项）
    select_only(page, "qwen_pe")
    page.wait_for_timeout(600)
    selected = page.evaluate("() => window.enhanceMethods.getSelected()")
    btn_text = page.inner_text("#enhanceBtnText")
    check("文生图：官方 PE 进入选中状态", "qwen_pe" in selected,
          f"selected={selected}, btn='{btn_text}'")
    # 【新增】推荐参数自动套用 + 可见回显
    pe_params = page.evaluate("() => state.imgQwen.peParams")
    check("文生图：选中即自动套用官方 PE 推荐参数",
          pe_params and pe_params.get("presence_penalty") == 1.5
          and pe_params.get("max_new_tokens") == 16256,
          f"peParams={pe_params}")
    applied_note = page.inner_text("#enhanceAppliedParams")
    check("文生图：已套用参数有可见回显", "1.5" in applied_note and "16256" in applied_note,
          applied_note)
    shot(page, "03-qwen-t2i-qwen-pe-selected.png")

    # ---------------------------------------------------------- 真跑官方 PE
    print("\n=== 2. 真跑官方 PE 扩写（文生图档）===")
    page.fill("#prompt", "一只橘猫坐在木质窗台上，午后阳光斜照，暖色调，浅景深")
    before = page.input_value("#prompt")
    t0 = time.time()
    # 官方 PE 是 9B 模型 + thinking，要给足时间
    page.click("#enhanceBtn")
    try:
        page.wait_for_selector("#enhanceModal:not([hidden])", timeout=300000)
    except Exception as exc:
        check("文生图：官方 PE 扩写返回结果", False, f"超时/失败: {exc}")
        raise
    elapsed = time.time() - t0
    result_text = page.input_value("#enhanceResult")
    meta = page.inner_text("#enhanceMeta")
    hint = page.inner_text("#enhanceHint")
    print(f"  耗时 {elapsed:.1f}s，扩写长度 {len(result_text)} 字")
    print("  meta:", meta)
    print("  hint:", hint)
    check("文生图：官方 PE 返回了扩写内容", len(result_text) > 200,
          f"{len(result_text)} 字")
    check("文生图：结果与原文不同（真的扩写了）", result_text.strip() != before.strip())
    check("文生图：meta 标明是官方 PE 档位", "PE-T2I" in meta or "PE" in meta, meta)
    check("文生图：扩写内容是英文结构化描述（官方 PE 形态）",
          result_text[:120].strip() and result_text.lstrip()[0].isalpha(),
          result_text[:120])
    check("文生图：无 think 残留", "<think>" not in result_text
          and "</think>" not in result_text)
    shot(page, "04-qwen-t2i-official-pe-result-modal.png")

    # 回填到提示词框
    page.click("#enhanceReplace")
    page.wait_for_timeout(800)
    after = page.input_value("#prompt")
    check("文生图：扩写结果已写回提示词框",
          after.strip() == result_text.strip(), f"{len(after)} 字")
    check("文生图：写回后标记为「已增强」",
          page.is_visible("#promptEnhancedBadge"))
    check("文生图：出图不再二次扩写（usePe=false）",
          page.evaluate("() => state.imgQwen.usePe") is False)
    # 可继续编辑
    page.fill("#prompt", after + " 额外补充：窗外有远处的山峦")
    page.wait_for_timeout(400)
    check("文生图：写回后仍可继续编辑",
          "额外补充" in page.input_value("#prompt"))
    shot(page, "05-qwen-t2i-prompt-filled-and-editable.png")

    # ---------------------------------------------------------- 图片编辑面板
    print("\n=== 3. Qwen 图片编辑面板 ===")
    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="i2i_qwen"]')
    page.wait_for_timeout(1500)
    dom2 = page.evaluate("""() => ({
      hasCheckbox: !!document.querySelector('#imgUsePe'),
      hasField: !!document.querySelector('#imgPeField'),
      bodyTextHasLabel: (document.body.innerText||'').includes('官方 PE 提示词扩写'),
      refVisible: getComputedStyle(
        document.querySelector('#imgRefField')).display !== 'none',
    })""")
    check("图片编辑：无独立 PE 勾选框", not dom2["hasCheckbox"])
    check("图片编辑：无独立 PE 区块", not dom2["hasField"])
    check("图片编辑：页面无「官方 PE 提示词扩写」文案",
          not dom2["bodyTextHasLabel"])
    check("图片编辑：参考图区正常显示", dom2["refVisible"])
    shot(page, "06-qwen-i2i-panel-no-pe-checkbox.png")

    open_menu(page)
    items2 = menu_items(page)
    labels2 = [i["label"] for i in items2]
    check("图片编辑：下拉有「qwen21图片官方增强PE」",
          "qwen21图片官方增强PE" in labels2, str(labels2))
    check("图片编辑：官方 PE 项可用",
          any(i["id"] == "qwen_pe" and not i["disabled"] for i in items2))
    shot(page, "07-qwen-i2i-enhance-menu.png")

    # ---------------------------------------------------------- 其它面板
    print("\n=== 4. 其它模型面板：官方 PE 项被禁用 ===")
    for mode, name in (("t2i", "zimage"), ("t2i_moss", "moss")):
        page.click(f'.nav-item[data-mode="{mode}"]')
        page.wait_for_timeout(1200)
        open_menu(page)
        it = menu_items(page)
        pe = [i for i in it if i["id"] == "qwen_pe"]
        check(f"{name}：官方 PE 被禁用", pe and pe[0]["disabled"],
              json.dumps(pe, ensure_ascii=False))
        check(f"{name}：普通/二次增强仍可用",
              all(any(i["id"] == o and not i["disabled"] for i in it)
                  for o in ("basic", "refine")))
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    shot(page, "08-other-panels-qwen-pe-disabled.png")

    # ---------------------------------------------------------- console
    print("\n=== 5. console 检查 ===")
    check("console 0 报错", len(errors) == 0, "; ".join(errors[:5]))

    browser.close()

print("\n" + "=" * 70)
passed = sum(1 for r in results if r["ok"])
failed = [r for r in results if not r["ok"]]
print(f"结果：{passed}/{len(results)} 通过")
if failed:
    print("失败项：")
    for r in failed:
        print(f"  - {r['label']}: {r['detail']}")
(EV / "verify-results.json").write_text(
    json.dumps({"results": results, "console_errors": errors},
               ensure_ascii=False, indent=2), encoding="utf-8")
print(f"证据目录：{EV}")
sys.exit(1 if failed else 0)
