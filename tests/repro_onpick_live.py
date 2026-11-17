# -*- coding: utf-8 -*-
"""在**线上 8190** 复现「素材库选择回调未传 -> onPick is not a function」。

用法: python3 tests/repro_onpick_live.py [base_url]
输出: JSON（结果 + 控制台错误 + 截图路径）
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/root/projects/h3studio-fix-onpick/evidence/onpick-repro")
OUT.mkdir(parents=True, exist_ok=True)

# 弹窗文本（alert）
alerts = []
console_errors = []
page_errors = []


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(m.text)
                if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        def _on_dialog(d):
            alerts.append(d.message)
            d.dismiss()
        page.on("dialog", _on_dialog)

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)

        # 切到「参考生视频」（视频导航是 <button data-mode="r2v">，不是 <a>）
        page.click('[data-mode="r2v"]')
        page.wait_for_timeout(1500)

        slots = page.query_selector_all("#uploadRow .up-slot")
        info = {"base": BASE, "slot_count": len(slots),
                "slot_labels": [s.get_attribute("data-label") for s in slots]}

        # 点「参考 1」槽位本体（不是「素材库」小按钮）
        target = None
        for s in slots:
            if (s.get_attribute("data-label") or "") == "参考 1":
                target = s
                break
        if target is None:
            info["error"] = "找不到参考 1 槽位"
            print(json.dumps(info, ensure_ascii=False, indent=2))
            browser.close()
            return

        # 点槽位（会走 openAssetPickerForSlot(kind) 这条**无回调**分支）
        target.click(position={"x": 20, "y": 20})
        page.wait_for_timeout(1200)
        info["picker_open_after_slot_click"] = bool(
            page.query_selector("#alPickerMask.open"))

        # 选第一张图 -> 确定使用
        cards = page.query_selector_all("#alPickerBody .al-card")
        info["picker_card_count"] = len(cards)
        if cards:
            cards[0].click()
            page.wait_for_timeout(300)
            ok = page.query_selector("#alPickerOk")
            if ok:
                ok.click()
                page.wait_for_timeout(1200)

        info["alerts"] = list(alerts)
        info["console_errors"] = list(console_errors)
        info["page_errors"] = list(page_errors)

        # 槽位是否回填
        filled = []
        for s in slots:
            filled.append({
                "label": s.get_attribute("data-label"),
                "filled": "filled" in (s.get_attribute("class") or ""),
                "has_img": bool(s.query_selector("img")),
                "text": (s.inner_text() or "").strip().replace("\n", "|"),
            })
        info["slots_after"] = filled

        shot = OUT / f"repro-{int(time.time())}.png"
        page.screenshot(path=str(shot), full_page=False)
        info["screenshot"] = str(shot)
        browser.close()

    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
