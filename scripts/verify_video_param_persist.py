#!/usr/bin/env python3
"""实测取证：视频族面板参数在「刷新后」与「复用参数后」是否还保持用户值。

【要验的两条链路（来自并行审计 D-1 / D-2）】
  D-1 持久化：用户改了步数/采样器/调度器 -> 保存 -> 刷新页面
      -> 面板应仍是用户的值。
      疑点：web/video-panel.js 的 collectConfig() 顶层**不写** steps/sampler/scheduler，
            而载入时的守卫 `if (saved.steps == null) recomputeSamplingForAccel();`
            读的是顶层 saved.steps -> 恒为 undefined -> 条件恒真
            -> 无条件用加速 LoRA 推荐档覆盖用户值。
  D-2 复用参数：点某条任务的「复用参数」-> 面板应回填**那条任务**的参数。
      疑点：applyConfig() 优先读 mode_configs[当前模式]（面板自己上次的配置），
            忽略传入的顶层字段 -> 被复用任务的参数被面板旧配置整块覆盖。

【方法】真实 Chromium 打线上 8190，用**真实后端** /api/video-config 与
          /api/jobs 往返，不 mock。

用法：python3 scripts/verify_video_param_persist.py
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

BASE = "http://127.0.0.1:8190"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "evidence" / "video_param_persist.json"


async def main() -> int:
    from playwright.async_api import async_playwright

    ev: dict = {"base": BASE, "steps": {"problems": []}, "problems": []}
    original_config = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 960})
        page = await ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)[:200]))

        await page.goto(BASE + "/", wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        # 备份线上原本的视频面板配置（测完要还原）
        original_config = await (await ctx.request.get(
            BASE + "/api/video-config")).json()
        ev["original_config"] = original_config

        # ---- 进入 t2v 视频面板 ----
        await page.evaluate(
            """() => {
              const n = document.querySelector('a.nav-item[data-mode="t2v"]');
              if (n) n.click();
            }""")
        await page.wait_for_timeout(2000)

        # ---- D-1：把步数改成 30、采样器 euler、调度器 simple，保存 ----
        before = await page.evaluate(
            """() => ({
              steps: document.querySelector('#vidSteps').value,
              sampler: document.querySelector('#vidSamplerSelect').value,
              scheduler: document.querySelector('#vidSchedulerSelect').value,
            })""")
        ev["steps"]["before_change"] = before
        print("[before]", json.dumps(before, ensure_ascii=False))

        await page.evaluate(
            """() => {
              const set = (sel, v) => {
                const el = document.querySelector(sel);
                el.value = v;
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('input', {bubbles: true}));
              };
              set('#vidSteps', '30');
              set('#vidSamplerSelect', 'euler');
              set('#vidSchedulerSelect', 'simple');
            }""")
        await page.wait_for_timeout(2500)

        changed = await page.evaluate(
            """() => ({
              steps: document.querySelector('#vidSteps').value,
              sampler: document.querySelector('#vidSamplerSelect').value,
              scheduler: document.querySelector('#vidSchedulerSelect').value,
            })""")
        ev["steps"]["after_change"] = changed
        print("[after change]", json.dumps(changed, ensure_ascii=False))

        # 显式保存（走 collectConfig -> PUT /api/video-config）
        await page.evaluate(
            """async () => {
              const p = window.VIDEO_PANEL;
              if (p && typeof p.saveConfig === 'function') p.saveConfig();
            }""")
        await page.wait_for_timeout(1500)

        saved = await (await ctx.request.get(BASE + "/api/video-config")).json()
        ev["steps"]["saved_config"] = {
            "top_level_has_steps": "steps" in saved,
            "top_level_keys": sorted(saved.keys()),
            "mode_configs_steps": {
                k: v.get("steps") for k, v in (saved.get("mode_configs") or {}).items()
            },
            "mode_configs_sampler": {
                k: v.get("sampler") for k, v in (saved.get("mode_configs") or {}).items()
            },
        }
        print("[saved]", json.dumps(ev["steps"]["saved_config"], ensure_ascii=False))

        # ---- 刷新页面，看面板是否还保持 30 ----
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await page.evaluate(
            """() => {
              const n = document.querySelector('a.nav-item[data-mode="t2v"]');
              if (n) n.click();
            }""")
        await page.wait_for_timeout(2500)
        after_reload = await page.evaluate(
            """() => ({
              steps: document.querySelector('#vidSteps').value,
              sampler: document.querySelector('#vidSamplerSelect').value,
              scheduler: document.querySelector('#vidSchedulerSelect').value,
            })""")
        ev["steps"]["after_reload"] = after_reload
        print("[after reload]", json.dumps(after_reload, ensure_ascii=False))

        # ---- D-2：复用参数 ----
        reuse = await page.evaluate(
            """async () => {
              const r = await fetch('/api/jobs?limit=40&status=done');
              const d = await r.json();
              const jobs = Array.isArray(d) ? d : (d.jobs || d.items || []);
              const t2v = jobs.find(j => j.mode === 't2v');
              if (!t2v) return {found: false};
              const gp = t2v.gen_params || {};
              return {found: true, job_id: t2v.id,
                      expected_steps: gp.steps, expected_sampler: gp.sampler,
                      expected_scheduler: gp.scheduler};
            }""")
        ev["steps"]["reuse_target"] = reuse
        print("[reuse target]", json.dumps(reuse, ensure_ascii=False))

        if reuse.get("found"):
            # 先把面板改成别的值（模拟「面板自己上次的配置」与被复用任务不同）
            await page.evaluate(
                """() => {
                  const set = (sel, v) => {
                    const el = document.querySelector(sel);
                    el.value = v;
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                  };
                  set('#vidSteps', '7');
                }""")
            await page.wait_for_timeout(1200)
            await page.evaluate(
                """(jid) => {
                  const card = document.querySelector('.job-card[data-jid="' + jid + '"]');
                  const btn = card && card.querySelector('[data-act="reuse"]');
                  if (btn) { btn.click(); return true; }
                  return false;
                }""", reuse["job_id"])
            await page.wait_for_timeout(2500)
            after_reuse = await page.evaluate(
                """() => ({
                  steps: document.querySelector('#vidSteps').value,
                  sampler: document.querySelector('#vidSamplerSelect').value,
                  scheduler: document.querySelector('#vidSchedulerSelect').value,
                })""")
            ev["steps"]["after_reuse"] = after_reuse
            print("[after reuse]", json.dumps(after_reuse, ensure_ascii=False))

        ev["pageErrors"] = errs

        # ---- 还原线上原本的视频面板配置 ----
        if original_config:
            await ctx.request.put(BASE + "/api/video-config",
                                  data=json.dumps(original_config))
            await page.wait_for_timeout(800)
        restored = await (await ctx.request.get(BASE + "/api/video-config")).json()
        ev["restored_ok"] = (
            json.dumps(restored.get("mode_configs") or {}, sort_keys=True)
            == json.dumps(original_config.get("mode_configs") or {}, sort_keys=True))
        print("[restored]", ev["restored_ok"])

        await browser.close()

    # ---- 判据 ----
    p = ev["steps"]
    if p.get("before_change") and p.get("after_change"):
        if str(p["after_change"]["steps"]) != "30":
            p["problems"].append("面板改步数没生效：%r" % p["after_change"]["steps"])
    sc = p.get("saved_config") or {}
    if sc.get("top_level_has_steps") is False:
        p["problems"].append(
            "D-1 根因坐实：保存的配置**顶层没有 steps**（只有 mode_configs）")
    ar = p.get("after_reload") or {}
    if ar and str(ar.get("steps")) != "30":
        p["problems"].append(
            "D-1 坐实：刷新后步数 = %r（期望 30，用户改的值被覆盖）" % ar.get("steps"))
    ru = p.get("after_reuse")
    if ru and p.get("reuse_target", {}).get("expected_steps") is not None:
        if str(ru.get("steps")) != str(p["reuse_target"]["expected_steps"]):
            p["problems"].append(
                "D-2 坐实：复用参数后面板步数 = %r，期望 %r"
                % (ru.get("steps"), p["reuse_target"]["expected_steps"]))
    if ev.get("pageErrors"):
        p["problems"].append("页面 JS 错误: %s" % ev["pageErrors"][:3])

    ev["problems"] = p["problems"]
    ev["verdict"] = "DEFECTS-CONFIRMED" if p["problems"] else "NO-DEFECT-FOUND"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("证据:", OUT)
    print("判定:", ev["verdict"])
    for x in p["problems"]:
        print("  !!", x)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
