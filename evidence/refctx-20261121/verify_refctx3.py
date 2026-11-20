#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第三轮：参考生音乐 a2a 面板的参考音频右键移除（真实素材库音频素材）。"""
import json
import os

from playwright.sync_api import sync_playwright

EVID = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/refctx-20261121"
URL = "http://127.0.0.1:8190/"
OUT = {}


def shot(page, name):
    p = os.path.join(EVID, name)
    page.screenshot(path=p, full_page=False)
    print(f"[shot] {p}")


def main():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(".nav-item[data-mode='a2a']", timeout=60000)
    page.wait_for_timeout(2500)

    page.click('.nav-item[data-mode="a2a"]')
    page.wait_for_timeout(2000)

    # 找参考音频的容器
    OUT["containers"] = page.evaluate("""() => {
        const out = {};
        document.querySelectorAll('[id]').forEach(e => {
            const id = e.id;
            if (/ref|music|audio/i.test(id)) {
                out[id] = {
                    tag: e.tagName,
                    cls: e.className,
                    childCount: e.children.length,
                    visible: e.getBoundingClientRect().height > 0,
                };
            }
        });
        return out;
    }""")
    print("[containers]", json.dumps(OUT["containers"], ensure_ascii=False, indent=1)[:2500])

    # 用真实音频素材填进去（找 audio 类素材）
    OUT["audio_assets"] = page.evaluate("""async () => {
        const data = await fetch('/api/refs/assets?category=audio&page_size=3')
            .then(r => r.json());
        return (data.assets || []).map(a => ({id: a.id, name: a.name}));
    }""")
    print("[audio assets]", json.dumps(OUT["audio_assets"], ensure_ascii=False))

    # 打开素材库选择器 —— 需要找到真实的交互入口
    OUT["picker_entry"] = page.evaluate("""() => {
        const btns = [...document.querySelectorAll('button')].map(b => ({
            text: b.textContent.trim().slice(0, 30),
            id: b.id || null,
            cls: b.className,
        })).filter(b => /素材库|音频|参考/.test(b.text));
        return btns;
    }""")
    print("[picker entry]", json.dumps(OUT["picker_entry"], ensure_ascii=False))

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:8])
    with open(os.path.join(EVID, "browser_result3.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result3.json")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
