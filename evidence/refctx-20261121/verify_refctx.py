#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""核实「参考素材右键 -> 移除」在 8190 线上是否真的可用（只读取证）。

真实浏览器（Playwright + Chromium）+ 真实线上 8190 + 真实素材库。
不改任何代码、不重启服务。
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

EVID = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/refctx-20261121"
URL = "http://127.0.0.1:8190/"
OUT = {}

os.makedirs(EVID, exist_ok=True)


def shot(page, name):
    p = os.path.join(EVID, name)
    page.screenshot(path=p, full_page=False)
    print(f"[shot] {p}")
    return p


def main():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
    page = ctx.new_page()

    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="networkidle")
    page.wait_for_timeout(1200)

    # ---------- 步骤 0：线上页面确实加载了右键菜单模块 ----------
    OUT["module_loaded"] = page.evaluate("""() => ({
        hasRefContextMenu: typeof window.RefContextMenu === 'object'
            && window.RefContextMenu !== null,
        hasAttach: !!(window.RefContextMenu
            && typeof window.RefContextMenu.attach === 'function'),
        menuElExists: !!document.querySelector('#refContextMenu'),
        menuElId: (document.querySelector('#refContextMenu') || {}).id || null,
        scriptTags: [...document.querySelectorAll('script[src]')]
            .map(s => s.getAttribute('src'))
            .filter(s => s && (s.includes('ref-context-menu') || s.includes('ref-slots'))),
    })""")
    print("[step0] module:", json.dumps(OUT["module_loaded"], ensure_ascii=False))

    # ---------- 步骤 1：切到 r2v（参考生视频）面板 ----------
    page.click('.nav-item[data-mode="r2v"]')
    page.wait_for_timeout(1200)
    OUT["r2v_slots_before_fill"] = page.evaluate(
        """() => [...document.querySelectorAll('#uploadRow .up-slot')]
             .map(s => ({key: s.dataset.key,
                         filled: (s.getAttribute('class')||'').includes('filled')}))""")
    print("[step1] slots:", json.dumps(OUT["r2v_slots_before_fill"], ensure_ascii=False))

    # ---------- 步骤 2：把真实素材库里的图塞进 3 个参考图槽位 ----------
    filled = page.evaluate("""async () => {
        const data = await fetch('/api/refs/assets?category=image&page_size=3')
            .then(r => r.json());
        const ids = (data.assets || []).map(a => a.id);
        const st = window.__h3State;
        st.assetRefs.ref0 = {asset_id: ids[0], name: 'A.png', category: 'image'};
        st.assetRefs.ref1 = {asset_id: ids[1], name: 'B.png', category: 'image'};
        st.assetRefs.ref2 = {asset_id: ids[2], name: 'C.png', category: 'image'};
        window.renderModeKeepSlots();
        document.querySelector('#prompt').value =
            'A=<Picture 1> B=<Picture 2> C=<Picture 3>';
        return {ids, refs: JSON.parse(JSON.stringify(st.assetRefs))};
    }""")
    page.wait_for_timeout(900)
    OUT["fill_result"] = filled
    print("[step2] filled:", json.dumps(filled, ensure_ascii=False))

    OUT["dom_before"] = page.evaluate("""() => {
        const st = window.__h3State;
        return {
            assetRefs: JSON.parse(JSON.stringify(st.assetRefs)),
            refKeys: Object.keys(st.assetRefs),
            prompt: document.querySelector('#prompt').value,
            slots: [...document.querySelectorAll('#uploadRow .up-slot')]
                .map(s => ({key: s.dataset.key,
                            filled: (s.getAttribute('class')||'').includes('filled')})),
        };
    }""")
    print("[step2] dom_before:", json.dumps(OUT["dom_before"], ensure_ascii=False))
    shot(page, "01-r2v-three-refs-filled.png")

    # ---------- 步骤 3：真实右键「参考 2」槽位 ----------
    slot = page.query_selector('#uploadRow .up-slot[data-key="ref1"]')
    if slot is None:
        OUT["error"] = "找不到「参考 2」槽位"
        print("[FAIL]", OUT["error"])
    else:
        box = slot.bounding_box()
        OUT["slot_box_ref1"] = box
        # 真右键：Playwright 的 button="right" 走真实鼠标右键事件
        slot.click(button="right")
        page.wait_for_timeout(600)

        OUT["menu_state"] = page.evaluate("""() => {
            const m = document.querySelector('#refContextMenu');
            if (!m) return {exists: false};
            const r = m.getBoundingClientRect();
            return {
                exists: true,
                open: (m.getAttribute('class')||'').includes('open'),
                classes: m.getAttribute('class'),
                rect: {x: r.x, y: r.y, w: r.width, h: r.height},
                visible: r.width > 0 && r.height > 0,
                buttons: [...m.querySelectorAll('button')].map(b => ({
                    text: b.textContent.trim(),
                    act: b.dataset.act || null,
                    cls: b.className,
                })),
            };
        }""")
        print("[step3] menu_state:", json.dumps(OUT["menu_state"], ensure_ascii=False))
        shot(page, "02-rightclick-menu-open.png")

        # ---------- 步骤 4：点「移除」 ----------
        btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
        OUT["remove_btn_present"] = btn is not None
        if btn is None:
            OUT["error"] = "菜单里没有 data-act=remove-ref 的按钮"
            print("[FAIL]", OUT["error"])
        else:
            OUT["remove_btn_text"] = btn.inner_text()
            btn.click()
            page.wait_for_timeout(1000)

            OUT["dom_after"] = page.evaluate("""() => {
                const st = window.__h3State;
                const m = document.querySelector('#refContextMenu');
                return {
                    assetRefs: JSON.parse(JSON.stringify(st.assetRefs)),
                    refKeys: Object.keys(st.assetRefs),
                    refIds: Object.values(st.assetRefs).map(r => r.asset_id),
                    prompt: document.querySelector('#prompt').value,
                    slots: [...document.querySelectorAll('#uploadRow .up-slot')]
                        .map(s => ({key: s.dataset.key,
                                    filled: (s.getAttribute('class')||'').includes('filled')})),
                    menuStillOpen: m ? (m.getAttribute('class')||'').includes('open') : null,
                };
            }""")
            print("[step4] dom_after:", json.dumps(OUT["dom_after"], ensure_ascii=False))
            shot(page, "03-after-remove.png")

    # ---------- 步骤 5：Qwen 参考图面板的右键菜单 ----------
    OUT["qwen"] = {}
    try:
        page.click('.nav-item[data-mode="i2i_qwen"]')
        page.wait_for_timeout(1500)
        OUT["qwen"]["qwenRefsPresent"] = page.evaluate(
            "() => !!(window.QwenRefs && typeof window.QwenRefs.add === 'function')")
        OUT["qwen"]["refItemsBefore"] = page.evaluate(
            """() => [...document.querySelectorAll('.img-ref-item')]
                 .map(e => e.dataset.idx)""")
        # 通过真实的模块 API 加两张参考图（走素材库真实 id）
        OUT["qwen"]["addResult"] = page.evaluate("""async () => {
            const data = await fetch('/api/refs/assets?category=image&page_size=2')
                .then(r => r.json());
            const ids = (data.assets || []).map(a => a.id);
            if (!window.QwenRefs) return {skipped: 'no QwenRefs'};
            const out = [];
            for (const id of ids) {
                const asset = (data.assets||[]).find(a => a.id === id);
                out.push(await window.QwenRefs.add(asset));
            }
            return {ids, out};
        }""")
        page.wait_for_timeout(1200)
        OUT["qwen"]["refItemsAfter"] = page.evaluate(
            """() => [...document.querySelectorAll('.img-ref-item')]
                 .map(e => ({idx: e.dataset.idx,
                             badge: (e.querySelector('.img-ref-idx')||{}).textContent}))""")
        print("[step5] qwen:", json.dumps(OUT["qwen"], ensure_ascii=False))

        items = page.query_selector_all(".img-ref-item")
        if items:
            items[0].click(button="right")
            page.wait_for_timeout(600)
            OUT["qwen"]["menu_state"] = page.evaluate("""() => {
                const m = document.querySelector('#refContextMenu');
                if (!m) return {exists: false};
                return {
                    exists: true,
                    open: (m.getAttribute('class')||'').includes('open'),
                    buttons: [...m.querySelectorAll('button')]
                        .map(b => ({text: b.textContent.trim(), act: b.dataset.act||null})),
                };
            }""")
            print("[step5] qwen menu:", json.dumps(OUT["qwen"]["menu_state"], ensure_ascii=False))
            shot(page, "04-qwen-ref-rightclick.png")
        else:
            OUT["qwen"]["note"] = "Qwen 面板没有 .img-ref-item（没有可用参考图条目）"
    except Exception as e:
        OUT["qwen"]["error"] = f"{type(e).__name__}: {e}"
        print("[step5][ERR]", OUT["qwen"]["error"])

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:5])

    with open(os.path.join(EVID, "browser_result.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result.json")

    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
