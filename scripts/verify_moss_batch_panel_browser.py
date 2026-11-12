#!/usr/bin/env python3
"""C 项 · 批次面板截图：真实 Chromium 打开真实 server，看批次卡片展开态。

只读真实 SQLite 数据，不 mock 任何接口。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = Path("/tmp/h3-fix-evidence")
SHOTS = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
              "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/agent-enh-merge")
PORT = int(os.environ.get("H3_TEST_PORT", "8198"))
DATA = Path(os.environ.get("H3_TEST_DATA", "/tmp/h3-moss-batch-e2e2"))
VIEWPORTS = [(375, 812), (390, 844), (1280, 900)]

# 切到 MOSS 面板（窄屏侧栏收起，nav 在视口外 -> JS dispatch）
JS_SWITCH_MOSS = """() => {
  const n = document.querySelector('a.nav-item[data-mode="t2i_moss"]');
  if (n) { n.click(); return true; }
  return false;
}"""

# 展开批次（真实 DOM: [data-act="toggle-batches"]）
JS_EXPAND = """() => {
  const b = document.querySelector('[data-act="toggle-batches"]');
  if (b) { b.click(); return true; }
  return false;
}"""


async def main() -> int:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["H3STUDIO_PORT"] = str(PORT)
    env["H3STUDIO_DATA_DIR"] = str(DATA)
    srv = subprocess.Popen(
        ["/home/ubuntu/comfyui/venv/bin/python", "server.py"],
        cwd=str(REPO), env=env,
        stdout=open("/tmp/h3-fix-evidence/moss_batch_panel_server.log", "wb"),
        stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{PORT}"
    print(f"server pid={srv.pid} port={PORT}", flush=True)
    evidence: dict = {"port": PORT, "console": [], "pageErrors": [], "shots": [],
                      "viewports": {}}
    try:
        import urllib.request
        ok = False
        for _ in range(90):
            try:
                urllib.request.urlopen(base + "/", timeout=3)
                ok = True
                break
            except Exception:
                time.sleep(1)
        if not ok:
            print("!! server 未就绪")
            return 2

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=["--no-sandbox"])
            for w, h in VIEWPORTS:
                ctx = await browser.new_context(viewport={"width": w, "height": h})
                page = await ctx.new_page()
                page.on("console", lambda m, w=w: evidence["console"].append(
                    {"width": w, "type": m.type, "text": m.text[:300]}))
                page.on("pageerror", lambda e, w=w: evidence["pageErrors"].append(
                    {"width": w, "error": str(e)[:300]}))
                await page.goto(base + "/", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                switched = await page.evaluate(JS_SWITCH_MOSS)
                await page.wait_for_timeout(2000)
                # 窄屏：任务列表在底部抽屉里（#panelFab 打开），先开抽屉
                if w <= 1100:
                    await page.evaluate("""() => {
                      const f = document.querySelector('#panelFab');
                      if (f) f.click();
                    }""")
                    await page.wait_for_timeout(1200)
                expanded = await page.evaluate(JS_EXPAND)
                await page.wait_for_timeout(2000)

                shot = SHOTS / f"moss-batch-panel-{w}.png"
                await page.screenshot(path=str(shot), full_page=True)
                evidence["shots"].append(str(shot))
                info = await page.evaluate("""() => {
                  const batches = Array.from(document.querySelectorAll('.job-batch'));
                  const toggle = document.querySelector('[data-act="toggle-batches"]');
                  const grid = document.querySelector('[data-batches]');
                  return {
                    batchRows: batches.length,
                    batchHidden: grid ? grid.hasAttribute('hidden') : null,
                    toggleText: toggle ? (toggle.innerText||'').replace(/\\s+/g,' ').trim() : null,
                    toggleExpanded: toggle ? toggle.getAttribute('aria-expanded') : null,
                    batchTexts: batches.slice(0, 6).map(b =>
                      (b.innerText||'').replace(/\\s+/g,' ').trim().slice(0, 100)),
                    bodyHasSteps100: /100/.test(document.body.innerText),
                  };
                }""")
                info["switched_to_moss"] = switched
                info["expand_clicked"] = expanded
                evidence["viewports"][str(w)] = info
                print(f"  [{w}] batchRows={info['batchRows']} "
                      f"hidden={info['batchHidden']} expanded={expanded} "
                      f"toggle={info['toggleText']}", flush=True)
                await ctx.close()
            await browser.close()
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()

    evidence["console_errors"] = [c for c in evidence["console"] if c["type"] == "error"]
    evidence["page_error_count"] = len(evidence["pageErrors"])
    (OUT / "moss_batch_panel_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 批次面板截图 ===")
    for s in evidence["shots"]:
        print("  " + s)
    print(f"console errors = {len(evidence['console_errors'])}, "
          f"pageErrors = {evidence['page_error_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
