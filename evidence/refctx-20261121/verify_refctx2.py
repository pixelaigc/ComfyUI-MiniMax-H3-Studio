#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二轮真实浏览器取证：Qwen 参考图面板 + 参考生音乐面板 + 空槽位不弹菜单。

只读。不改代码、不重启服务。
"""
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


def menu_state(page):
    return page.evaluate("""() => {
        const m = document.querySelector('#refContextMenu');
        if (!m) return {exists: false};
        const r = m.getBoundingClientRect();
        return {
            exists: true,
            open: (m.getAttribute('class')||'').includes('open'),
            rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
            visible: r.width > 0 && r.height > 0,
            buttons: [...m.querySelectorAll('button')].map(b => ({
                text: b.textContent.trim(), act: b.dataset.act || null})),
        };
    }""")


def main():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    # 注意：应用每 2 秒轮询一次任务列表，networkidle 永远不会到达 —— 用 domcontentloaded。
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(".nav-item[data-mode='r2v']", timeout=60000)
    page.wait_for_timeout(2500)

    # ============ A. Qwen 图片编辑面板的参考图右键 ============
    print("=== A. i2i_qwen ===")
    page.click('.nav-item[data-mode="i2i_qwen"]')
    page.wait_for_timeout(1800)
    OUT["qwen_api"] = page.evaluate(
        """() => ({
            hasQwenRefs: !!(window.QwenRefs),
            fns: window.QwenRefs ? Object.keys(window.QwenRefs) : [],
        })""")
    print("[A] api:", json.dumps(OUT["qwen_api"], ensure_ascii=False))

    # 用真实素材库 id，通过真实 addAssets() 加两张参考图
    OUT["qwen_add"] = page.evaluate("""async () => {
        const data = await fetch('/api/refs/assets?category=image&page_size=2')
            .then(r => r.json());
        const ids = (data.assets || []).map(a => a.id);
        if (!window.QwenRefs || typeof window.QwenRefs.addAssets !== 'function') {
            return {skipped: 'addAssets unavailable', ids};
        }
        await window.QwenRefs.addAssets(ids);
        return {ids};
    }""")
    page.wait_for_timeout(1500)
    OUT["qwen_items"] = page.evaluate("""() => ({
        count: (window.QwenRefs && window.QwenRefs.count) ? window.QwenRefs.count() : null,
        items: [...document.querySelectorAll('.img-ref-item')].map(e => ({
            idx: e.dataset.idx,
            badge: (e.querySelector('.img-ref-idx')||{}).textContent,
            hasDelBtn: !!e.querySelector('.img-ref-del'),
        })),
    })""")
    print("[A] items:", json.dumps(OUT["qwen_items"], ensure_ascii=False))

    items = page.query_selector_all(".img-ref-item")
    if items:
        # 先记录移除前状态
        OUT["qwen_before"] = page.evaluate("""() => ({
            count: window.QwenRefs.count(),
            refs: window.QwenRefs.getRefs().map(r => r.asset_id),
            prompt: (document.querySelector('#prompt')||{}).value,
        })""")
        page.evaluate("""() => {
            const p = document.querySelector('#prompt');
            if (p) p.value = 'A=<image1> B=<image2>';
        }""")
        # 真右键第一张参考图
        items[0].click(button="right")
        page.wait_for_timeout(700)
        OUT["qwen_menu"] = menu_state(page)
        print("[A] menu:", json.dumps(OUT["qwen_menu"], ensure_ascii=False))
        shot(page, "04-qwen-ref-rightclick.png")

        btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
        if btn:
            OUT["qwen_btn_text"] = btn.inner_text()
            btn.click()
            page.wait_for_timeout(1200)
            OUT["qwen_after"] = page.evaluate("""() => ({
                count: window.QwenRefs.count(),
                refs: window.QwenRefs.getRefs().map(r => r.asset_id),
                prompt: (document.querySelector('#prompt')||{}).value,
                badges: [...document.querySelectorAll('.img-ref-item .img-ref-idx')]
                    .map(e => e.textContent),
                menuStillOpen: (document.querySelector('#refContextMenu').getAttribute('class')||'')
                    .includes('open'),
            })""")
            print("[A] after:", json.dumps(OUT["qwen_after"], ensure_ascii=False))
            shot(page, "05-qwen-after-remove.png")
        else:
            OUT["qwen_no_btn"] = True
            print("[A] !! 没有 remove-ref 按钮")
    else:
        OUT["qwen_no_items"] = True
        print("[A] !! 没有 .img-ref-item")

    # ============ B. 空槽位右键 -> 不该弹菜单 ============
    print("=== B. r2v 空槽位 ===")
    page.click('.nav-item[data-mode="r2v"]')
    page.wait_for_timeout(1500)
    empty = page.query_selector('#uploadRow .up-slot[data-key="ref1"]')
    OUT["empty_slot_found"] = empty is not None
    if empty:
        OUT["empty_slot_filled_class"] = empty.get_attribute("class")
        empty.click(button="right")
        page.wait_for_timeout(600)
        OUT["empty_slot_menu"] = menu_state(page)
        print("[B] empty menu:", json.dumps(OUT["empty_slot_menu"], ensure_ascii=False))
        shot(page, "06-empty-slot-no-menu.png")

    # ============ C. 参考生音乐 a2a 面板 ============
    print("=== C. a2a ===")
    try:
        page.click('.nav-item[data-mode="a2a"]')
        page.wait_for_timeout(1800)
        OUT["music"] = {"panelFound": True}
        OUT["music"]["cards_before"] = page.evaluate(
            """() => document.querySelectorAll('#musicRefList .m-ref-card, .m-ref-card').length""")
        print("[C] music cards:", OUT["music"]["cards_before"])
        OUT["music"]["api"] = page.evaluate(
            """() => ({hasMusicUI: !!window.MusicUI,
                        fns: window.MusicUI ? Object.keys(window.MusicUI).slice(0,40) : []})""")
        print("[C] music api:", json.dumps(OUT["music"]["api"], ensure_ascii=False))
    except Exception as e:
        OUT["music"] = {"error": f"{type(e).__name__}: {e}"}
        print("[C][ERR]", OUT["music"]["error"])

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:8])

    with open(os.path.join(EVID, "browser_result2.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result2.json")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
