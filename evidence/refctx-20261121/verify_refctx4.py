#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第四轮（修正版）：参考生音乐 a2a —— 走真实素材库选择器 UI（#alPickerMask /
.al-card[data-id] / #alPickerOk），填入参考音频后右键移除。"""
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

    # 1) 打开真实素材库选择器
    page.click("#musicRefPick")
    page.wait_for_selector("#alPickerMask.open", timeout=20000)
    page.wait_for_timeout(2500)
    OUT["picker_open"] = page.evaluate("""() => ({
        maskOpen: document.querySelector('#alPickerMask').classList.contains('open'),
        cards: [...document.querySelectorAll('#alPickerBody .al-card')].map(e => ({
            id: e.dataset.id,
            name: (e.querySelector('.al-name')||{}).textContent,
            meta: (e.querySelector('.al-meta')||{}).textContent,
        })),
    })""")
    print("[picker_open]", json.dumps(OUT["picker_open"], ensure_ascii=False, indent=1))
    shot(page, "07-music-asset-picker.png")

    cards = page.query_selector_all("#alPickerBody .al-card")
    OUT["card_count"] = len(cards)
    if cards:
        cards[0].click()
        page.wait_for_timeout(800)
        OUT["picked"] = page.evaluate("""() => ({
            pickedCards: document.querySelectorAll('#alPickerBody .al-card.picked').length,
            foot: (document.querySelector('#alPickerFoot')||{}).textContent,
        })""")
        print("[picked]", json.dumps(OUT["picked"], ensure_ascii=False))
        page.click("#alPickerOk")
        page.wait_for_timeout(2500)
    else:
        OUT["no_cards"] = True
        print("[!!] 选择器里没有卡片")

    # 2) 参考音频卡片是否渲染出来
    OUT["ref_card"] = page.evaluate("""() => {
        const box = document.querySelector('#musicRefBox');
        const card = box && box.querySelector('.img-ref-card');
        return {
            boxExists: !!box,
            cardExists: !!card,
            cardText: card ? card.textContent.trim().slice(0, 60) : null,
            cardVisible: card ? card.getBoundingClientRect().height > 0 : false,
            note: (document.querySelector('#musicRefNote')||{}).textContent,
        };
    }""")
    print("[ref_card]", json.dumps(OUT["ref_card"], ensure_ascii=False))
    shot(page, "09-music-ref-audio-filled.png")

    # 3) 右键参考音频卡片
    card = page.query_selector("#musicRefBox .img-ref-card")
    if card:
        card.click(button="right")
        page.wait_for_timeout(800)
        OUT["menu"] = page.evaluate("""() => {
            const m = document.querySelector('#refContextMenu');
            if (!m) return {exists: false};
            const r = m.getBoundingClientRect();
            return {
                exists: true,
                open: (m.getAttribute('class')||'').includes('open'),
                rect: {x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)},
                visible: r.width > 0 && r.height > 0,
                buttons: [...m.querySelectorAll('button')].map(b => ({
                    text: b.textContent.trim(), act: b.dataset.act||null})),
            };
        }""")
        print("[menu]", json.dumps(OUT["menu"], ensure_ascii=False))
        shot(page, "10-music-ref-rightclick.png")

        btn = page.query_selector('#refContextMenu button[data-act="remove-ref"]')
        if btn:
            OUT["btn_text"] = btn.inner_text()
            btn.click()
            page.wait_for_timeout(1500)
            OUT["card_after"] = page.evaluate("""() => ({
                cardExists: !!document.querySelector('#musicRefBox .img-ref-card'),
                boxText: (document.querySelector('#musicRefBox')||{}).textContent,
                note: (document.querySelector('#musicRefNote')||{}).textContent,
            })""")
            print("[after]", json.dumps(OUT["card_after"], ensure_ascii=False))
            shot(page, "11-music-after-remove.png")
        else:
            OUT["no_btn"] = True
            print("[!!] 菜单里没有 remove-ref 按钮")
    else:
        OUT["no_card"] = True
        print("[!!] 没有参考音频卡片 —— 无法右键")

    OUT["console_errors"] = errors
    OUT["console_error_count"] = len(errors)
    print("[console errors]", len(errors), errors[:8])
    with open(os.path.join(EVID, "browser_result4.json"), "w", encoding="utf-8") as f:
        json.dump(OUT, f, ensure_ascii=False, indent=2)
    print("[saved] browser_result4.json")
    browser.close()
    pw.stop()


if __name__ == "__main__":
    main()
