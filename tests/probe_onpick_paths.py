# -*- coding: utf-8 -*-
"""分别测「点空槽位」与「点槽位上的素材库按钮」两条路径，定位哪条坏。

用法: python3 tests/probe_onpick_paths.py [base_url]

【注意】dialog 监听器只能注册**一次** —— 注册多次时，第二个 handler
再调 d.dismiss() 会抛 "Cannot dismiss dialog which is already handled"，
把后续几轮的 alert 记录搞丢（实测踩过）。
"""
import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"

ALERTS = []


def _on_dialog(d):
    ALERTS.append(d.message)
    d.dismiss()


def run(page, mode, label, use_button):
    ALERTS.clear()
    page.click(f'[data-mode="{mode}"]')
    page.wait_for_timeout(1200)
    slots = page.query_selector_all("#uploadRow .up-slot")
    target = None
    for s in slots:
        if (s.get_attribute("data-label") or "") == label:
            target = s
            break
    if target is None:
        return {"mode": mode, "label": label, "error": "槽位不存在"}
    if use_button:
        target.query_selector(".pick-asset").click()
    else:
        # 点的位置必须避开右上角的「素材库」小按钮（top:3 right:3）
        target.click(position={"x": 10, "y": 40})
    page.wait_for_timeout(1000)
    picker_open = bool(page.query_selector("#alPickerMask.open"))
    cards = page.query_selector_all("#alPickerBody .al-card")
    n_cards = len(cards)
    if cards:
        cards[0].click()
        page.wait_for_timeout(300)
        ok = page.query_selector("#alPickerOk")
        if ok:
            ok.click()
            page.wait_for_timeout(1000)
    return {
        "mode": mode, "label": label,
        "path": "素材库按钮" if use_button else "点空槽位",
        "picker_open": picker_open,
        "picker_cards": n_cards,
        "alerts": list(ALERTS),
        "slot_filled": "filled" in (target.get_attribute("class") or ""),
        "has_thumb": bool(target.query_selector("img")),
    }


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True,
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = b.new_context(viewport={"width": 1600, "height": 1100}).new_page()
        page.on("dialog", _on_dialog)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1000)
        results = []
        for mode, label, use_btn in [
            ("r2v", "参考 1", False),
            ("r2v", "参考 1", True),
            ("r2v", "参考 2", False),
            ("r2v", "参考视频 1", False),
            ("r2v", "参考音乐 1", False),
            ("flf", "首帧", False),
            ("flf", "尾帧", False),
            ("flf", "参考视频 1", False),
            ("flf", "参考音乐 1", False),
        ]:
            results.append(run(page, mode, label, use_btn))
            page.goto(BASE, wait_until="networkidle")
            page.wait_for_timeout(700)
        b.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
