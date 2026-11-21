# -*- coding: utf-8 -*-
"""主生成面板 t2v 的加速 LoRA 下拉**真实渲染**取证。

【为什么不能用 ``a.nav-item[data-mode='t2v']``】
侧栏的「类型」是**弹层选择器**渲染的，初始 DOM 里没有那些 ``a.nav-item``。
正确入口是顶栏的类型选择器（``#navGen`` 之类），点它才展开类型列表。

用法：python3 probe_t2v_lora_options.py <base_url> <out_json>
"""
import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/t2v_lora_options.json"


def dump(page, mode):
    return page.evaluate(
        """() => {
          const s = document.getElementById('vidAccelLoraSelect');
          const m = document.getElementById('vidModelSelect');
          if (!s) return {error: 'no vidAccelLoraSelect'};
          return {
            model: m ? m.value : null,
            options: Array.from(s.options).map(o => ({
              v: o.value, label: o.textContent.trim()})),
          };
        }""")


def main():
    result = {"base": BASE, "console_errors": [], "panels": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 1200})
        page.on("console", lambda m: result["console_errors"].append(
            f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: result["console_errors"].append(
            f"pageerror: {e}"))

        page.goto(BASE, wait_until="domcontentloaded", timeout=90000)
        # 等顶栏 / 侧栏里的「文生视频」入口真的出现
        page.wait_for_function(
            """() => {
                const el = document.querySelector(
                    "[data-mode='t2v'], a[data-mode='t2v']");
                return !!el;
            }""", timeout=60000)

        for mode in ("t2v", "flf"):
            # 直接派发点击（走前端真实处理函数），不要求元素可见
            page.evaluate(
                """(m) => {
                    const el = document.querySelector(
                        "[data-mode='" + m + "']");
                    if (el) el.click();
                }""", mode)
            page.wait_for_timeout(2500)
            # 打开参数面板（宽屏常驻，窄屏是抽屉）
            for sel in ("#panelToggle", "#paramsFab", "#openParams",
                        "#navParams"):
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        page.wait_for_timeout(1200)
                        break
                except Exception:
                    pass
            page.wait_for_timeout(1500)
            result["panels"][mode] = dump(page, mode)

        browser.close()

    result["console_error_count"] = len(result["console_errors"])
    text = json.dumps(result, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
