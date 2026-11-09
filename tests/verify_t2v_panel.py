# -*- coding: utf-8 -*-
"""文生视频面板的真实浏览器渲染验证。

跑三档视口（375 / 390 / 1280），逐档：
  * 切到「文生视频」模式
  * 断言新面板可见、旧面板隐藏
  * 断言宽高输入 / 模型下拉 / 加速 LoRA 列表 / 注意力下拉 / 二采开关都在
  * 手填一个非 32 倍数的宽高 -> 等吸附 -> 断言「输入值 → 实际使用值 + 原因」出现
  * 勾一个加速 LoRA 组合 -> 断言勾选态生效
  * 打开二采 -> 断言参数区展开
  * 收集 console 报错 / 页面异常

用法: python3 tests/verify_t2v_panel.py [base_url]
产物: /tmp/h3t2v-verify/*.png + /tmp/h3t2v-verify/report.json
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8193"
OUT = Path("/tmp/h3t2v-verify")
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORTS = [
    (375, 812, "375"),
    (390, 844, "390"),
    (1280, 900, "1280"),
]


def main():
    report = {"base": BASE, "viewports": {}, "console_errors": [], "page_errors": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for width, height, tag in VIEWPORTS:
            context = browser.new_context(viewport={"width": width, "height": height},
                                          device_scale_factor=2 if width < 500 else 1)
            page = context.new_page()
            console_errors = []
            page_errors = []
            page.on("console", lambda m, bag=console_errors: (
                bag.append({"type": m.type, "text": m.text})
                if m.type in ("error", "warning") else None))
            page.on("pageerror", lambda e, bag=page_errors: bag.append(str(e)))
            # 注意：不能用 networkidle —— 页面有 SSE 状态流（/api/status/stream）
            # 常驻连接，networkidle 永远等不到（本项目的老坑）。
            page.goto(BASE, wait_until="load")
            page.wait_for_timeout(1500)
            # 等清单加载完（面板里的模型下拉有选项 = 清单到了）
            page.wait_for_selector("#videoPanel", state="attached")
            page.wait_for_function(
                "() => { const s = document.getElementById('vidModelSelect');"
                " return s && s.options.length > 0; }", timeout=20000)
            # 切到文生视频（窄屏下侧栏是抽屉，元素在视口外 —— 直接用 JS 点）
            page.evaluate(
                "() => document.querySelector('.nav-item[data-mode=\"t2v\"]').click()")
            page.wait_for_timeout(1500)
            # 窄屏下参数面板是底部抽屉，要先打开才能交互
            if width <= 820:
                page.evaluate(
                    "() => { const fab = document.getElementById('panelFab');"
                    " if (fab) fab.click(); }")
                page.wait_for_timeout(600)

            checks = {}
            checks["video_panel_visible"] = page.is_visible("#videoPanel")
            checks["old_res_hidden"] = not page.is_visible("#resField")
            checks["old_model_hidden"] = not page.is_visible("#modelField")
            checks["old_steps_hidden"] = not page.is_visible("#stepsField")
            checks["width_input_visible"] = page.is_visible("#vidWidth")
            checks["height_input_visible"] = page.is_visible("#vidHeight")
            checks["model_select_visible"] = page.is_visible("#vidModelSelect")
            checks["model_options"] = page.eval_on_selector_all(
                "#vidModelSelect option", "els => els.map(e => e.value)")
            checks["accel_lora_items"] = page.eval_on_selector_all(
                "#vidAccelLoraList .img-lora-item", "els => els.length")
            checks["attention_options"] = page.eval_on_selector_all(
                "#vidAttentionSelect option", "els => els.map(e => e.value)")
            checks["twopass_switch_visible"] = page.is_visible("#vidTwopassEnabled")
            checks["enhance_menu_has_director"] = page.evaluate(
                "() => !!(window.enhanceMethods && window.enhanceMethods.list"
                " && window.enhanceMethods.list.some(m => m.id === 'director'))")

            # ---- 宽高吸附：填一个非 32 倍数的值 ----
            page.fill("#vidWidth", "1000")
            page.dispatch_event("#vidWidth", "input")
            page.fill("#vidHeight", "563")
            page.dispatch_event("#vidHeight", "input")
            page.wait_for_timeout(900)
            page.dispatch_event("#vidWidth", "change")
            page.wait_for_timeout(700)
            checks["snapped_width"] = page.input_value("#vidWidth")
            checks["snapped_height"] = page.input_value("#vidHeight")
            checks["size_hint"] = page.text_content("#vidSizeHint")
            adjust = page.query_selector("#vidSizeAdjust")
            checks["size_adjust_visible"] = bool(adjust and adjust.is_visible())
            checks["size_adjust_text"] = (adjust.text_content() if adjust else "") or ""

            # ---- 勾一个加速 LoRA 组合 ----
            toggles = page.query_selector_all('#vidAccelLoraList button.toggle')
            checks["accel_toggle_count"] = len(toggles)
            if toggles:
                page.evaluate(
                    "() => document.querySelector("
                    "'#vidAccelLoraList button.toggle').click()")
                page.wait_for_timeout(500)
            checks["accel_note_after_check"] = page.text_content("#vidAccelLoraNote")
            checks["accel_checked"] = page.eval_on_selector_all(
                '#vidAccelLoraList button.toggle',
                "els => els.filter(e => e.textContent.trim() === '●').length")

            # ---- 打开二采 ----
            page.evaluate(
                "() => { const c = document.getElementById('vidTwopassEnabled');"
                " if (!c.checked) { c.checked = true;"
                " c.dispatchEvent(new Event('change', {bubbles: true})); } }")
            page.wait_for_timeout(500)
            checks["twopass_body_visible"] = page.is_visible("#vidTwopassBody")
            checks["twopass_note"] = page.text_content("#vidTwopassNote")

            # ---- 切模型，验证兼容性清单刷新 ----
            opts = page.eval_on_selector_all(
                "#vidModelSelect option", "els => els.map(e => e.value)")
            if "fl2va_pruned_w4a8" in opts:
                page.select_option("#vidModelSelect", "fl2va_pruned_w4a8")
                page.wait_for_timeout(1200)
                checks["w4a8_blocked_count"] = page.eval_on_selector_all(
                    '#vidAccelLoraList .img-lora-item.excluded', "els => els.length")
                page.select_option("#vidModelSelect", "fl2va_pruned_int8")
                page.wait_for_timeout(1000)

            # 截图（面板滚动到顶部）+ 面板整段截图（含注意力 / 二采）
            page.evaluate("() => { const p = document.getElementById('panel');"
                          " if (p) p.scrollTop = 0; }")
            page.wait_for_timeout(300)
            shot = OUT / f"t2v-panel-{tag}.png"
            page.screenshot(path=str(shot), full_page=False)
            checks["screenshot"] = str(shot)

            # 面板下半段截图（注意力机制 / 二采 在长列表下面，
            # 面板是可滚动的，元素截图只拍到可见区 -> 要滚到底再拍）
            panel_shot = OUT / f"t2v-panel-full-{tag}.png"
            try:
                page.evaluate("() => { const p = document.getElementById('panel');"
                              " if (p) p.scrollTop = p.scrollHeight; }")
                page.wait_for_timeout(500)
                panel_el = page.query_selector("#panel")
                if panel_el:
                    panel_el.screenshot(path=str(panel_shot))
                    checks["panel_screenshot"] = str(panel_shot)
                # 滚到底之后整屏再拍一张（窄屏抽屉里能直接看到二采开关）
                shot2 = OUT / f"t2v-panel-bottom-{tag}.png"
                page.screenshot(path=str(shot2), full_page=False)
                checks["screenshot_bottom"] = str(shot2)
                page.evaluate("() => { const p = document.getElementById('panel');"
                              " if (p) p.scrollTop = 0; }")
            except Exception as exc:
                checks["panel_screenshot_error"] = str(exc)

            # 注意力 / 二采控件是否真的渲染出来了
            checks["attention_select_has_options"] = bool(
                checks["attention_options"])
            checks["attention_selected"] = page.eval_on_selector(
                "#vidAttentionSelect", "el => el.value")
            checks["attention_note"] = page.text_content("#vidAttentionNote")
            checks["twopass_range_controls"] = page.eval_on_selector_all(
                "#vidTwopassBody input[type=range]",
                "els => els.map(e => e.id)")
            checks["upscaler_options"] = page.eval_on_selector_all(
                "#vidUpscalerSelect option", "els => els.map(e => e.value)")

            # 注意力下拉单独滚到可见处再拍一张（证明它在 UI 里真实可见）
            att_shot = OUT / f"t2v-attention-{tag}.png"
            try:
                page.evaluate(
                    "() => { const el = document.getElementById('vidAttentionSelect');"
                    " if (el) el.scrollIntoView({block: 'center'}); }")
                page.wait_for_timeout(400)
                page.screenshot(path=str(att_shot), full_page=False)
                checks["attention_screenshot"] = str(att_shot)
                checks["attention_visible"] = page.is_visible("#vidAttentionSelect")
            except Exception as exc:
                checks["attention_screenshot_error"] = str(exc)

            report["viewports"][tag] = checks
            report["console_errors"].extend(
                [{"viewport": tag, **e} for e in console_errors])
            report["page_errors"].extend(
                [{"viewport": tag, "text": e} for e in page_errors])
            context.close()
        browser.close()

    (OUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
