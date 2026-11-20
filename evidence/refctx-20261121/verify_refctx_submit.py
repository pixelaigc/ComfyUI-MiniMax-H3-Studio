#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""独立验证「移除后提交体不再包含被移除素材」—— 不依赖仓库自带测试。

真实浏览器 + 真实线上 8190 + 真实素材库 + 拦截 /api/jobs POST 抓提交体。
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


def main():
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True,
                                args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_context(viewport={"width": 1600, "height": 1100}).new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))

    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(".nav-item[data-mode='r2v']", timeout=60000)
    page.wait_for_timeout(2500)
    page.click('.nav-item[data-mode="r2v"]')
    page.wait_for_timeout(1800)

    # 填 2 张真实素材 + 拦截提交
    OUT["setup"] = page.evaluate("""async () => {
        const data = await fetch('/api/refs/assets?category=image&page_size=3')
            .then(r => r.json());
        const ids = (data.assets || []).map(a => a.id);
        window.__captured = {ids, body: null};
        const origFetch = window.fetch;
        window.fetch = function (url, opts) {
            if (String(url).includes('/api/jobs') && opts && opts.method === 'POST') {
                window.__captured.body = opts.body;
                return Promise.resolve(new Response(
                    JSON.stringify({id: 'fake-job'}), {status: 200}));
            }
            return origFetch.apply(this, arguments);
        };
        const st = window.__h3State;
        st.assetRefs.ref0 = {asset_id: ids[0], name: 'A.png', category: 'image'};
        st.assetRefs.ref1 = {asset_id: ids[1], name: 'B.png', category: 'image'};
        window.renderModeKeepSlots();
        return {ids: ids.slice(0, 2)};
    }""")
    page.wait_for_timeout(1200)
    print("[setup]", json.dumps(OUT["setup"], ensure_ascii=False))

    OUT["before"] = page.evaluate("""() => ({
        refs: Object.entries(window.__h3State.assetRefs).map(([k,v]) => [k, v.asset_id]),
    })""")
    print("[before]", json.dumps(OUT["before"], ensure_ascii=False))

    # 右键第 1 张 -> 移除
    slot = page.query_selector('#uploadRow .up-slot[data-key="ref0"]')
    slot.click(button="right")
    page.wait_for_timeout(700)
    OUT["menu"] = page.evaluate("""() => {
        const m = document.querySelector('#refContextMenu');
        return {open: (m.getAttribute('class')||'').includes('open'),
                buttons: [...m.querySelectorAll('button')]
                    .map(b => ({text: b.textContent.trim(), act: b.dataset.act||null}))};
    }""")
    print("[menu]", json.dumps(OUT["menu"], ensure_ascii=False))
    shot(page, "12-before-submit-remove.png")

    page.query_selector('#refContextMenu button[data-act="remove-ref"]').click()
    page.wait_for_timeout(1200)
    OUT["after_remove"] = page.evaluate("""() => ({
        refs: Object.entries(window.__h3State.assetRefs).map(([k,v]) => [k, v.asset_id]),
    })""")
    print("[after_remove]", json.dumps(OUT["after_remove"], ensure_ascii=False))

    # 点生成 -> 抓提交体
    page.evaluate("""() => {
        document.querySelector('#prompt').value = '测试提示词';
        document.querySelector('#genBtn').click();
    }""")
    page.wait_for_timeout(2000)

    OUT["captured"] = page.evaluate("""() => {
        const body = window.__captured.body;
        if (!body) return {hasBody: false};
        const out = {hasBody: true, kind: body.constructor.name, entries: []};
        if (typeof body.forEach === 'function' && !body.get) {
            body.forEach((v, k) => out.entries.push([k, typeof v === 'string' ? v : '[blob]']));
        }
        return out;
    }""")
    print("[captured]", json.dumps(OUT["captured"], ensure_ascii=False, indent=1))

    OUT["assertion"] = page.evaluate("""() => {
        const ids = window.__captured.ids;
        const body = window.__captured.body;
        const st = window.__h3State;
        const stillRefs = Object.values(st.assetRefs).map(r => r.asset_id);
        const removedId = ids[0];
        const keptId = ids[1];
        const raw = body ? (typeof body.entries === 'function'
            ? JSON.stringify(Object.fromEntries(body.entries()))
            : String(body)) : '';
        return {
            removedId,
            keptId,
            stillInState: stillRefs,
            removedStillInState: stillRefs.includes(removedId),
            keptStillInState: stillRefs.includes(keptId),
            removedInPayloadText: raw.includes(removedId),
            keptInPayloadText: raw.includes(keptId),
            payloadKeys: body && typeof body.entries === 'function'
                ? Object.fromEntries(body.entries()) : null,
        };
    }""")
    print("[assertion]", json.dumps(OUT["assertion"], ensure_ascii=False, indent=1))
    shot(page, "13-after-submit-captured.png")

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:5])

    with open(os.path.join(EVID, "browser_result_submit.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result_submit.json")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
