# -*- coding: utf-8 -*-
"""复现脚本（临时）：素材库页状态下切换生成标签时的首图巨图闪现。

用法：python3 tests/_repro_flash.py [base_url]
产出：/tmp/h3flash/repro-<ts>.json + 截图
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/tmp/h3flash")
OUT.mkdir(parents=True, exist_ok=True)

PROBE = """
() => {
  const grid = document.querySelector('#jobsGrid');
  const area = document.querySelector('#jobsArea');
  const assets = document.querySelector('#assetsPage');
  const imgs = Array.from(document.querySelectorAll('#jobsGrid .job-preview img'));
  return {
    t: performance.now(),
    gridCols: grid ? (grid.dataset.cols || '') : null,
    gridInline: grid ? grid.style.gridTemplateColumns : null,
    gridWidth: grid ? Math.round(grid.getBoundingClientRect().width) : null,
    areaDisplay: area ? getComputedStyle(area).display : null,
    assetsDisplay: assets ? getComputedStyle(assets).display : null,
    cardCount: document.querySelectorAll('#jobsGrid .job-card').length,
    imgCount: imgs.length,
    imgSizes: imgs.slice(0, 3).map(i => {
      const r = i.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height),
              nw: i.naturalWidth, nh: i.naturalHeight,
              complete: i.complete};
    }),
    previewBoxes: Array.from(document.querySelectorAll('#jobsGrid .job-preview')).slice(0,3).map(p => {
      const r = p.getBoundingClientRect();
      return {w: Math.round(r.width), h: Math.round(r.height)};
    }),
    bodyScrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  };
}
"""


def main():
    ts = time.strftime("%Y%m%d-%H%M%S")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card').length > 0",
            timeout=20000)

        # 记录素材库状态下的网格事实
        page.click("#navAssets")
        page.wait_for_timeout(2500)
        in_assets = page.evaluate(PROBE)
        page.screenshot(path=str(OUT / f"{ts}-01-assets.png"), full_page=False)

        # 切回「Z-IMAGE 文生图」：立刻开始采样
        samples = []
        page.evaluate("""
          () => {
            window.__samples = [];
            const t0 = performance.now();
            const tick = () => {
              const grid = document.querySelector('#jobsGrid');
              const imgs = Array.from(document.querySelectorAll('#jobsGrid .job-preview img'));
              const pb = document.querySelector('#jobsGrid .job-preview');
              window.__samples.push({
                t: Math.round(performance.now() - t0),
                cols: grid ? (grid.dataset.cols || '') : null,
                inline: grid ? grid.style.gridTemplateColumns : null,
                gw: grid ? Math.round(grid.getBoundingClientRect().width) : null,
                imgs: imgs.slice(0,2).map(i => {
                  const r = i.getBoundingClientRect();
                  return {w: Math.round(r.width), h: Math.round(r.height), nw: i.naturalWidth};
                }),
                pb: pb ? (() => {const r = pb.getBoundingClientRect();
                        return {w: Math.round(r.width), h: Math.round(r.height)};})() : null,
              });
              if (performance.now() - t0 < 3000) requestAnimationFrame(tick);
            };
            requestAnimationFrame(tick);
          }
        """)
        page.click(".nav-item[data-mode='t2i']")
        page.wait_for_timeout(3200)
        samples = page.evaluate("() => window.__samples")
        page.screenshot(path=str(OUT / f"{ts}-02-after-switch.png"), full_page=False)

        report = {"ts": ts, "base": BASE, "in_assets": in_assets,
                  "samples": samples, "errors": errors}
        (OUT / f"repro-{ts}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print("=== 素材库状态下 ===")
        print(json.dumps(in_assets, ensure_ascii=False, indent=2))
        print("=== 切换后采样（前 12 帧 + 每 10 帧）===")
        for i, s in enumerate(samples):
            if i < 12 or i % 10 == 0:
                print(json.dumps(s, ensure_ascii=False))
        print("errors:", errors)
        print("报告:", OUT / f"repro-{ts}.json")
        browser.close()


if __name__ == "__main__":
    main()
