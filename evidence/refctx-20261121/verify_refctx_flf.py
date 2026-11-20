#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第五轮：flf（首尾帧）面板的参考素材槽位右键移除 —— 覆盖「首帧/尾帧」两个槽位。"""
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
    page.wait_for_selector(".nav-item[data-mode='flf']", timeout=60000)
    page.wait_for_timeout(2500)
    page.click('.nav-item[data-mode="flf"]')
    page.wait_for_timeout(1800)

    OUT["slots"] = page.evaluate(
        """() => [...document.querySelectorAll('#uploadRow .up-slot')]
             .map(s => ({key: s.dataset.key,
                         filled: (s.getAttribute('class')||'').includes('filled')}))""")
    print("[slots]", json.dumps(OUT["slots"], ensure_ascii=False))

    OUT["setup"] = page.evaluate("""async () => {
        const data = await fetch('/api/refs/assets?category=image&page_size=2')
            .then(r => r.json());
        const ids = (data.assets || []).map(a => a.id);
        const st = window.__h3State;
        st.assetRefs.first = {asset_id: ids[0], name: 'F.png', category: 'image'};
        st.assetRefs.last  = {asset_id: ids[1], name: 'L.png', category: 'image'};
        window.renderModeKeepSlots();
        document.querySelector('#prompt').value = 'F=<Picture 1> L=<Picture 2>';
        return {ids: ids.slice(0,2)};
    }""")
    page.wait_for_timeout(1200)
    OUT["before"] = page.evaluate("""() => ({
        refs: Object.entries(window.__h3State.assetRefs).map(([k,v]) => [k,v.asset_id]),
        prompt: document.querySelector('#prompt').value,
        slots: [...document.querySelectorAll('#uploadRow .up-slot')].map(s => ({
            key: s.dataset.key,
            filled: (s.getAttribute('class')||'').includes('filled')})),
    })""")
    print("[before]", json.dumps(OUT["before"], ensure_ascii=False))
    shot(page, "14-flf-two-frames-filled.png")

    # 右键「首帧」槽位
    slot = page.query_selector('#uploadRow .up-slot[data-key="first"]')
    if slot:
        slot.click(button="right")
        page.wait_for_timeout(700)
        OUT["menu"] = page.evaluate("""() => {
            const m = document.querySelector('#refContextMenu');
            if (!m) return {exists:false};
            const r = m.getBoundingClientRect();
            return {exists:true,
                    open:(m.getAttribute('class')||'').includes('open'),
                    visible: r.width>0 && r.height>0,
                    buttons:[...m.querySelectorAll('button')].map(b=>({
                        text:b.textContent.trim(), act:b.dataset.act||null}))};
        }""")
        print("[menu]", json.dumps(OUT["menu"], ensure_ascii=False))
        shot(page, "15-flf-rightclick-first-frame.png")

        btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
        if btn:
            btn.click()
            page.wait_for_timeout(1200)
            OUT["after"] = page.evaluate("""() => ({
                refs: Object.entries(window.__h3State.assetRefs).map(([k,v]) => [k,v.asset_id]),
                prompt: document.querySelector('#prompt').value,
                slots: [...document.querySelectorAll('#uploadRow .up-slot')].map(s => ({
                    key: s.dataset.key,
                    filled: (s.getAttribute('class')||'').includes('filled')})),
            })""")
            print("[after]", json.dumps(OUT["after"], ensure_ascii=False))
            shot(page, "16-flf-after-remove.png")
        else:
            OUT["no_btn"] = True
    else:
        OUT["no_first_slot"] = True
        print("[!!] 找不到首帧槽位")

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:5])
    with open(os.path.join(EVID, "browser_result_flf.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result_flf.json")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
