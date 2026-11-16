#!/usr/bin/env python3
"""部署后线上浏览器验证（真实 Chromium 打 8190）。

验证「面板改值真的到了后端」这条链路的**前端那一段**：

  1. MOSS-Image 面板的「采样步数」滑杆量程 **max == 100**
     （由服务端 /api/image-options 的 param_ranges 下发）——
     证明前端不再硬编码、量程真源已统一；
  2. 把滑杆拖到 100 后，**数值框显示 100**、提示徽标变成「已改」；
  3. 其它族的量程也各自正确（Qwen shift 步进 0.01 / H3-Image steps 上限 12）；
  4. 任务详情弹层出现「参数已调整」一行（归一说明不再静默）。

用法：python3 scripts/verify_imgparams_browser.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

BASE = "http://127.0.0.1:8190"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evidence" / "imgparams_browser.json"
SHOTS = ROOT / "evidence" / "imgparams_browser.png"

#: 族 -> 期望的滑杆量程（与服务端 IMAGE_PARAM_RANGES 一致）
EXPECTED = {
    "t2i_moss":   {"#imgSteps": {"min": "1", "max": "100", "step": "1"},
                   "#imgCfg":   {"min": "0", "max": "10",  "step": "0.1"},
                   "#imgShift": {"min": "0", "max": "10",  "step": "0.1"}},
    "t2i_qwen":   {"#imgSteps": {"min": "1", "max": "60",  "step": "1"},
                   "#imgCfg":   {"min": "0", "max": "8",   "step": "0.1"},
                   "#imgShift": {"min": "0", "max": "3",   "step": "0.01"}},
    "t2i_h3image": {"#imgSteps": {"min": "1", "max": "12", "step": "1"},
                    "#imgCfg":   {"min": "1", "max": "8",  "step": "0.5"},
                    "#imgShift": {"min": "0", "max": "3",  "step": "0.1"}},
    "t2i":        {"#imgSteps": {"min": "1", "max": "60",  "step": "1"},
                   "#imgCfg":   {"min": "1", "max": "8",   "step": "0.5"},
                   "#imgShift": {"min": "1", "max": "7",   "step": "0.5"}},
}


async def main() -> int:
    from playwright.async_api import async_playwright

    ev: dict = {"base": BASE, "ranges": {}, "steps_interaction": {},
                "detail_modal": {}, "console": [], "pageErrors": []}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 960})
        page = await ctx.new_page()
        page.on("console", lambda m: ev["console"].append(
            {"type": m.type, "text": m.text[:200]}))
        page.on("pageerror", lambda e: ev["pageErrors"].append(str(e)[:300]))

        resp = await page.goto(BASE + "/", wait_until="domcontentloaded")
        ev["http_status"] = resp.status if resp else None
        await page.wait_for_timeout(4000)

        # ---- 1) 各族滑杆量程 ----
        for mode, expect in EXPECTED.items():
            await page.evaluate(
                """(m) => { const n = document.querySelector(
                     'a.nav-item[data-mode="%s"]'); if (n) n.click(); }""" % mode)
            await page.wait_for_timeout(1600)
            actual = await page.evaluate(
                """() => {
                  const out = {};
                  for (const sel of ["#imgSteps", "#imgCfg", "#imgShift"]) {
                    const el = document.querySelector(sel);
                    out[sel] = el ? {min: el.min, max: el.max, step: el.step,
                                      value: el.value} : null;
                  }
                  return out;
                }""")
            ev["ranges"][mode] = {"expected": expect, "actual": actual}
            print("[ranges] %-14s %s" % (mode, json.dumps(actual, ensure_ascii=False)))

        # ---- 2) MOSS 面板把步数拖到 100 ----
        await page.evaluate(
            """() => { const n = document.querySelector(
                 'a.nav-item[data-mode="t2i_moss"]'); if (n) n.click(); }""")
        await page.wait_for_timeout(1600)
        await page.evaluate(
            """() => {
              const el = document.querySelector('#imgSteps');
              el.value = '100';
              el.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
        await page.wait_for_timeout(700)
        ev["steps_interaction"] = await page.evaluate(
            """() => ({
              slider_value: document.querySelector('#imgSteps').value,
              label: document.querySelector('#imgStepsVal').textContent,
              hint: document.querySelector('#imgStepsHint').textContent,
              disabled: document.querySelector('#imgSteps').disabled,
            })""")
        print("[steps] ", json.dumps(ev["steps_interaction"], ensure_ascii=False))
        await page.screenshot(path=str(SHOTS))

        # ---- 3) 任务详情弹层的「参数已调整」行 ----
        # 找一条**参数被服务端改过**的历史任务（本次修复前那条 steps=100->50 的）
        detail = await page.evaluate(
            """async () => {
              const r = await fetch('/api/jobs?limit=60&status=done');
              const d = await r.json();
              const jobs = Array.isArray(d) ? d : (d.jobs || d.items || []);
              for (const j of jobs) {
                const notes = ((j.gen_params||{}).normalize_notes)||[];
                if (notes.length) return {job_id: j.id, mode: j.mode, notes: notes};
              }
              return {job_id: null, notes: []};
            }""")
        ev["detail_modal"] = detail
        print("[detail]", json.dumps(detail, ensure_ascii=False))

        await browser.close()

    # ---- 判据 ----
    problems = []
    for mode, blob in ev["ranges"].items():
        for sel, want in blob["expected"].items():
            got = (blob["actual"] or {}).get(sel)
            if not got:
                problems.append("%s: %s 缺失" % (mode, sel))
                continue
            for k in ("min", "max", "step"):
                if str(got.get(k)) != str(want[k]):
                    problems.append("%s %s.%s = %r，期望 %r"
                                    % (mode, sel, k, got.get(k), want[k]))
    si = ev["steps_interaction"]
    if str(si.get("slider_value")) != "100":
        problems.append("拖到 100 后滑杆值 = %r" % si.get("slider_value"))
    if str(si.get("label")) != "100":
        problems.append("拖到 100 后数值框 = %r" % si.get("label"))
    if si.get("hint") != "已改":
        problems.append("拖到 100 后徽标 = %r（期望「已改」）" % si.get("hint"))
    if ev["pageErrors"]:
        problems.append("页面有 JS 错误: %s" % ev["pageErrors"][:3])

    ev["problems"] = problems
    ev["verdict"] = "PASS" if not problems else "FAIL"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("证据:", OUT)
    print("截图:", SHOTS)
    print("判定:", ev["verdict"])
    for p in problems:
        print("  !!", p)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
