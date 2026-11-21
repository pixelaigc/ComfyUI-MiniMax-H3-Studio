#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 8 轮 —— 部署后的**真实渲染截图**取证（主界面 / 媒体库 / Agent 面板）。

【为什么要单独写】第 7 轮的 watchdog7_cd_audit.py 只截「主界面」。
但本轮 C 维度要求覆盖「各模式面板、生成媒体库/素材库（含正在开发中的收藏栏）、
Agent 面板」—— 这几处当前线上都**没有**截图证据。
本脚本补上这几屏（纯截图 + DOM 几何，不改任何配置、不提交任何任务）。

用法：
    python3 scripts/wd8_shots.py --base http://127.0.0.1:8190 \
        --out /root/evidence/watchdog8-20261121/screenshots-post
"""
from __future__ import annotations

import argparse
import json
import os
import sys

OUT = "/root/evidence/watchdog8-20261121/screenshots-post"
BASE = "http://127.0.0.1:8190"


def shoot(page, name, full=False):
    path = os.path.join(OUT, f"{name}.png")
    page.screenshot(path=path, full_page=full)
    print(f"  截图 {path}  ({os.path.getsize(path)} bytes)")


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--out", default="/root/evidence/watchdog8-20261121/screenshots-post")
    args = ap.parse_args()
    BASE = args.base
    global OUT
    OUT = args.out
    os.makedirs(OUT, exist_ok=True)

    from playwright.sync_api import sync_playwright

    report = {"base": BASE, "shots": [], "dom": {}}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)

        # 1) 主界面
        shoot(page, "01-main-1280")
        report["shots"].append("01-main-1280.png")

        # 2) 生成媒体库（点侧栏「生成媒体库」）
        for label, shot in (("生成媒体库", "02-media-library-1280"),
                            ("素材库", "03-asset-library-1280")):
            clicked = page.evaluate(
                """(label) => {
                  const els = Array.from(document.querySelectorAll(
                    'a,button,.nav-item,.side-item,[role=button]'));
                  const el = els.find(e => (e.textContent||'').trim().includes(label));
                  if (el) { el.click(); return true; }
                  return false;
                }""", label)
            page.wait_for_timeout(1800)
            shoot(page, shot)
            report["shots"].append(f"{shot}.png (nav clicked={clicked})")

        # 3) Agent 面板
        clicked = page.evaluate(
            """() => {
              const els = Array.from(document.querySelectorAll(
                'a,button,.nav-item,.side-item,[role=button]'));
              const el = els.find(e => /agent|智能体/i.test((e.textContent||'').trim()));
              if (el) { el.click(); return (el.textContent||'').trim(); }
              return null;
            }""")
        page.wait_for_timeout(1800)
        shoot(page, "04-agent-panel-1280")
        report["shots"].append(f"04-agent-panel-1280.png (nav clicked={clicked})")

        # DOM 几何 + 面板可见性（可复算）
        report["dom"] = page.evaluate(
            """() => {
              const vis = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
              };
              const panels = {};
              document.querySelectorAll('[id^=panel],[id$=-panel],[class*=panel]')
                .forEach(el => {
                  if (vis(el)) panels[el.id || el.className] = {
                    w: Math.round(el.getBoundingClientRect().width),
                    h: Math.round(el.getBoundingClientRect().height),
                  };
                });
              return {
                title: document.title,
                visiblePanels: panels,
                scrollWidth: document.documentElement.scrollWidth,
                clientWidth: document.documentElement.clientWidth,
              };
            }""")

        # 4) 移动端两视口
        for w, h, tag in ((375, 812, "375"), (390, 844, "390")):
            page.set_viewport_size({"width": w, "height": h})
            page.wait_for_timeout(1500)
            shoot(page, f"05-main-{tag}")
            report["shots"].append(f"05-main-{tag}.png")

        report["console_errors"] = errors
        browser.close()

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n控制台错误:", len(errors))
    print("输出目录:", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
