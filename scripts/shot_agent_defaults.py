# -*- coding: utf-8 -*-
"""
「内置 Agent 管理」面板的默认参数截图（flf / r2v）。

【为什么用真实浏览器】静态断言只能证明「源码里有这段文字」，
证明不了「面板真的渲染成那样」。用户报的现象就是**面板上看到的**。

【采集内容】
  * 截图（同一入口、同一视口，flf 与 r2v 各一组）
  * 控制台错误数（必须为 0）
  * 375px 下的横向溢出（必须无）
  * 面板里 flf / r2v 的默认参数字段快照（JSON）

用法：python3 shot_agent_defaults.py <base_url> <out_dir> <tag>
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/shot")
TAG = sys.argv[3] if len(sys.argv) > 3 else "run"
OUT.mkdir(parents=True, exist_ok=True)

VIEWPORT = {"width": 1280, "height": 1400}

# 面板里要抓的两个生成类型
TARGETS = ("flf", "r2v")

# 采集「这个生成类型区块里有哪些字段、默认值是什么」
COLLECT = """
() => {
  const out = {};
  const blocks = document.querySelectorAll('#agManageBody details.agm-gen');
  blocks.forEach(b => {
    const tag = b.querySelector('.agm-gen-head .agm-tag:not(.req)');
    const title = b.querySelector('.agm-gen-title');
    const key = (tag && tag.textContent || '').trim();
    if (!key) return;
    const fields = {};
    b.querySelectorAll('.agm-field').forEach(f => {
      const label = f.querySelector('span');
      const input = f.querySelector('input, select');
      if (!label || !input) return;
      const name = label.textContent.trim();
      let value = input.value;
      if (input.tagName === 'SELECT') {
        const opt = input.options[input.selectedIndex];
        value = opt ? (opt.textContent || opt.value) : input.value;
        fields[name] = {kind: 'select', value: value,
                        raw: input.value,
                        options: Array.from(input.options).map(o => o.value)};
      } else {
        fields[name] = {kind: input.type, value: value, raw: input.value};
      }
    });
    const note = b.querySelector('.agm-field-note');
    out[key] = {title: title ? title.textContent.trim() : '',
                open: b.open, fields: fields,
                notes: Array.from(b.querySelectorAll('.agm-field-note'))
                            .map(n => n.textContent.trim())};
  });
  return out;
}
"""


def main():
    result = {"base": BASE, "tag": TAG, "console_errors": [],
              "shots": [], "panels": {}, "overflow_375": None}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport=VIEWPORT)
        page.on("console", lambda m: result["console_errors"].append(
            f"{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: result["console_errors"].append(
            f"pageerror: {e}"))

        page.goto(BASE, wait_until="networkidle")
        # 「内置 Agent 管理」按钮在 Agent 页内 -> 先切到 Agent 页
        page.wait_for_selector("#navAgent", timeout=15000)
        page.click("#navAgent")
        page.wait_for_timeout(600)
        # 打开「内置 Agent 管理」
        page.wait_for_selector("#agManageBtn", state="visible", timeout=15000)
        page.click("#agManageBtn")
        page.wait_for_selector("#agManageBody details.agm-gen", timeout=15000)
        page.wait_for_timeout(800)

        for key in TARGETS:
            # 展开该生成类型区块（用 JS 精确匹配 mode 标签）
            opened = page.evaluate(
                """(key) => {
                    const blocks = document.querySelectorAll(
                        '#agManageBody details.agm-gen');
                    for (const b of blocks) {
                        const tag = b.querySelector(
                            '.agm-gen-head .agm-tag:not(.req)');
                        if (tag && tag.textContent.trim() === key) {
                            b.open = true;
                            b.scrollIntoView({block: 'center'});
                            return true;
                        }
                    }
                    return false;
                }""", key)
            page.wait_for_timeout(400)
            path = OUT / f"agent_manage_{key}_{TAG}.png"
            # 只截该区块所在区域（让默认参数字段可见）
            try:
                el = page.query_selector(
                    f'#agManageBody details.agm-gen >> nth=0')
                blocks = page.query_selector_all("#agManageBody details.agm-gen")
                target = None
                for b in blocks:
                    tag = b.query_selector(".agm-gen-head .agm-tag:not(.req)")
                    if tag and tag.textContent.strip() == key:
                        target = b
                        break
                if target is not None:
                    target.screenshot(path=str(path))
                else:
                    page.screenshot(path=str(path), full_page=True)
            except Exception as exc:
                page.screenshot(path=str(path), full_page=True)
                result.setdefault("shot_errors", []).append(str(exc))
            result["shots"].append({"mode": key, "opened": opened,
                                    "path": str(path)})

        result["panels"] = page.evaluate(COLLECT)

        # 375px 无横向溢出
        page.set_viewport_size({"width": 375, "height": 900})
        page.wait_for_timeout(600)
        result["overflow_375"] = page.evaluate(
            """() => ({
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
                overflow: document.documentElement.scrollWidth >
                          document.documentElement.clientWidth + 1,
                bodyScrollWidth: document.body.scrollWidth,
            })""")
        path375 = OUT / f"agent_manage_375px_{TAG}.png"
        page.screenshot(path=str(path375), full_page=True)
        result["shots"].append({"mode": "375px", "path": str(path375)})

        browser.close()

    (OUT / f"agent_manage_{TAG}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "console_errors": len(result["console_errors"]),
        "overflow_375": result["overflow_375"],
        "shots": [s["path"] for s in result["shots"]],
        "panel_modes": list(result["panels"].keys()),
    }, ensure_ascii=False, indent=2))
    if result["console_errors"]:
        print("CONSOLE ERRORS:", result["console_errors"][:10])


if __name__ == "__main__":
    main()
