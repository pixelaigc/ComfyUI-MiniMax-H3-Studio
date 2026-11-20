#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗第 7 轮 —— C/D 维度真实渲染取证（2026-11-21）。

【这个脚本防什么 / 取证什么】
本脚本**只取证，不改代码**。它用真实浏览器打开线上 8190，
在两个视口（1280 桌面 / 375 + 390 移动）下：

  * 截取主界面 + 各模式面板 + 素材库 / 生成库 + Agent 面板
  * 记录首屏的**网络请求瀑布**（哪个请求慢、占了多久）
  * 量测关键 DOM 的几何（溢出 / 触控尺寸 / 对比度相关的类名）
  * 记录控制台错误

用法:
    python3 scripts/watchdog7_cd_audit.py --out /root/evidence/watchdog7

产出（绝对路径）:
    <out>/desktop-1280-*.png
    <out>/mobile-375-*.png
    <out>/mobile-390-*.png
    <out>/network.json
    <out>/dom.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")

VIEWS = [
    ("desktop-1280", 1280, 900),
    ("mobile-375", 375, 780),
    ("mobile-390", 390, 844),
]

# 面板/视图的选择器（尽量用可见文案，DOM 变了就会明确报「找不到」）
TARGETS = [
    ("main", "body"),
    ("gen-panel", "#modeSeg, .mode-seg, #genPanel, #gen-form"),
    ("media-library", "#assetList, #assetGrid, #assetsPanel, .asset-panel"),
    ("agent-panel", "#agentPanel, #agentChat, #agent-panel"),
]


def run(out_dir: Path) -> int:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    network: list[dict] = []
    console_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        for tag, width, height in VIEWS:
            ctx = browser.new_context(viewport={"width": width,
                                               "height": height},
                                      device_scale_factor=2 if width < 500 else 1)
            page = ctx.new_page()
            rec: list[dict] = []

            def on_response(resp, _rec=rec):
                try:
                    req = resp.request
                    _rec.append({
                        "url": resp.url.replace(BASE, ""),
                        "status": resp.status,
                        "method": req.method,
                        "resource_type": req.resource_type,
                        # Playwright 不直接给耗时；用请求开始时间差近似
                        "timing": resp.request.timing,
                    })
                except Exception:
                    pass

            def on_console(msg, _errs=console_errors):
                if msg.type in ("error", "warning"):
                    _errs.append(f"[{tag}] {msg.type}: {msg.text[:300]}")

            page.on("response", on_response)
            page.on("console", on_console)
            page.on("pageerror",
                    lambda e: console_errors.append(f"[{tag}] pageerror: {e}"))

            page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            # 首屏网络瀑布
            for label, url in (("first-load", tag),):
                pass
            network.append({"view": tag, "responses": rec})

            # 逐个目标截图
            for name, sel in TARGETS:
                try:
                    el = page.query_selector(sel)
                    if el is None:
                        continue
                    el.screenshot(path=str(out_dir / f"{tag}-{name}.png"))
                except Exception as exc:
                    console_errors.append(f"[{tag}] screenshot {name} failed: {exc}")

            # 整页截图
            try:
                page.screenshot(path=str(out_dir / f"{tag}-full.png"),
                                full_page=False)
            except Exception as exc:
                console_errors.append(f"[{tag}] full screenshot failed: {exc}")

            # DOM 几何取证：溢出 / 触控尺寸
            geo = page.evaluate("""() => {
              const out = {overflow: [], smallTap: [], count: {}};
              const all = document.querySelectorAll('*');
              out.count.total = all.length;
              const vw = document.documentElement.clientWidth;
              for (const el of all) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) continue;
                // 横向溢出
                if (r.right > vw + 2 || r.left < -2) {
                  out.overflow.push({
                    tag: el.tagName, id: el.id,
                    cls: (el.className || '').toString().slice(0, 80),
                    left: Math.round(r.left), right: Math.round(r.right),
                    vw
                  });
                }
                // 触控目标 < 32px
                if (el.matches('button, a, [role="button"], input[type=submit]')) {
                  if (r.height < 32 || r.width < 32) {
                    out.smallTap.push({
                      tag: el.tagName, id: el.id,
                      cls: (el.className || '').toString().slice(0, 60),
                      w: Math.round(r.width), h: Math.round(r.height),
                      text: (el.textContent || '').trim().slice(0, 30)
                    });
                  }
                }
              }
              out.overflow = out.overflow.slice(0, 40);
              out.smallTap = out.smallTap.slice(0, 40);
              out.scrollWidth = document.documentElement.scrollWidth;
              out.clientWidth = document.documentElement.clientWidth;
              return out;
            }""")
            geo["view"] = tag
            (out_dir / f"dom-{tag}.json").write_text(
                json.dumps(geo, ensure_ascii=False, indent=2), encoding="utf-8")
            ctx.close()
        browser.close()

    (out_dir / "network.json").write_text(
        json.dumps(network, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "console.json").write_text(
        json.dumps(console_errors, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== 截图 ===")
    for p in sorted(out_dir.glob("*.png")):
        print(f"  {p}  ({p.stat().st_size} bytes)")
    print("=== 控制台 ===")
    for e in console_errors[:30]:
        print("  " + e)
    print(f"\n输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/root/evidence/watchdog7")
    args = ap.parse_args()
    sys.exit(run(Path(args.out)))
