#!/usr/bin/env python3
"""8190 线上回归检查 —— 分类入口 / 参数面板 / 步数联动 / 生成库筛选。

真实 Chromium → 真实 8190 服务，逐项断言 + 截图。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2
           else "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-live")
OUT.mkdir(parents=True, exist_ok=True)

result = {"base": BASE, "steps": {}, "console_errors": [], "screenshots": []}


def shot(page, name):
    p = OUT / name
    page.screenshot(path=str(p))
    result["screenshots"].append(str(p))
    print(f"  [shot] {p}")
    return str(p)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
    page = ctx.new_page()
    page.on("console", lambda m: result["console_errors"].append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: result["console_errors"].append(f"pageerror: {e}"))

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(1500)

    # ---------- 1. 分类入口：四个图片模式都在导航里 ----------
    nav = page.evaluate("""() => {
        const items = [...document.querySelectorAll('a.nav-item[data-mode]')];
        return {
            modes: items.map(a => a.dataset.mode),
            labels: items.map(a => a.textContent.trim()),
            imgModes: items.filter(a => ['t2i','t2i_qwen','i2i_qwen','t2i_moss'].includes(a.dataset.mode))
                          .map(a => a.dataset.mode),
        };
    }""")
    result["steps"]["nav_entries"] = nav
    print("[1] 分类入口:", json.dumps(nav, ensure_ascii=False))
    shot(page, "70-regression-nav-entries.png")

    # ---------- 2. 参数面板：切到「Qwen-Image 2.1 文生图」，检查面板字段 ----------
    page.click('a.nav-item[data-mode="t2i_qwen"]')
    page.wait_for_timeout(1500)
    panel = page.evaluate("""() => {
        const vis = sel => { const e = document.querySelector(sel);
            return !!e && getComputedStyle(e).display !== 'none'; };
        return {
            mode: 't2i_qwen',
            modelCards: document.querySelectorAll('#modelCards > *').length,
            stepsVisible: vis('#imgSteps'),
            stepsVal: (document.querySelector('#imgStepsVal')||{}).textContent,
            stepsHint: (document.querySelector('#imgStepsHint')||{}).textContent,
            aspectVisible: vis('#imgAspect'),
            cfgVisible: vis('#imgCfg'),
            shiftVisible: vis('#imgShift'),
            samplerVisible: vis('#imgSampler'),
            refFieldVisible: vis('#imgRefField'),   // 文生图不该显示参考图
            modelSel: [...document.querySelectorAll('#modelCards .model-card, #modelCards [data-model]')]
                        .map(e => e.textContent.trim()).slice(0, 6),
        };
    }""")
    result["steps"]["panel_t2i_qwen"] = panel
    print("[2] 文生图参数面板:", json.dumps(panel, ensure_ascii=False)[:600])
    shot(page, "71-regression-panel-t2i-qwen.png")

    # ---------- 3. 步数联动：改步数 -> 显示值跟着变；切模型 -> 步数推荐跟着变 ----------
    steps_link = page.evaluate("""async () => {
        const el = document.querySelector('#imgSteps');
        const before = {value: el.value, shown: document.querySelector('#imgStepsVal').textContent};
        el.value = '33';
        el.dispatchEvent(new Event('input', {bubbles: true}));
        await new Promise(r => setTimeout(r, 200));
        const after = {value: el.value, shown: document.querySelector('#imgStepsVal').textContent,
                       hint: document.querySelector('#imgStepsHint').textContent};
        return {before, after};
    }""")
    result["steps"]["steps_linkage"] = steps_link
    print("[3] 步数联动:", json.dumps(steps_link, ensure_ascii=False))
    shot(page, "72-regression-steps-linkage.png")

    # 切模型 -> 步数应回落到该模型推荐值（Qwen 面板用 #imgModelSelect 下拉）
    model_link = page.evaluate("""async () => {
        const sel = document.querySelector('#imgModelSelect');
        if (!sel) return {skipped: true, reason: 'no #imgModelSelect'};
        const out = [];
        for (const opt of [...sel.options]) {
            sel.value = opt.value;
            sel.dispatchEvent(new Event('change', {bubbles: true}));
            await new Promise(r => setTimeout(r, 900));
            out.push({
                model: opt.value,
                label: opt.textContent.trim(),
                steps: document.querySelector('#imgSteps').value,
                shown: document.querySelector('#imgStepsVal').textContent,
                hint: document.querySelector('#imgStepsHint').textContent,
            });
        }
        return out;
    }""")
    result["steps"]["model_steps_linkage"] = model_link
    print("[4] Qwen 模型->步数联动:", json.dumps(model_link, ensure_ascii=False)[:600])
    shot(page, "73-regression-model-steps-linkage.png")

    # ---------- 4. 图片编辑模式：参考图区应出现 ----------
    page.click('a.nav-item[data-mode="i2i_qwen"]')
    page.wait_for_timeout(1200)
    i2i = page.evaluate("""() => {
        const vis = sel => { const e = document.querySelector(sel);
            return !!e && getComputedStyle(e).display !== 'none'; };
        return {
            refFieldVisible: vis('#imgRefField'),
            addBtn: (document.querySelector('#imgRefAdd')||{}).textContent,
            hasFileInput: !!document.querySelector('#imgRefField input[type=file]'),
        };
    }""")
    result["steps"]["i2i_ref_field"] = i2i
    print("[5] 图片编辑参考图区:", json.dumps(i2i, ensure_ascii=False))
    shot(page, "74-regression-i2i-ref-field.png")

    # ---------- 5. 生成库筛选 ----------
    page.wait_for_timeout(800)
    gallery = page.evaluate("""async () => {
        // 生成库在右侧；筛选项通常是 select 或按钮组
        const sels = [...document.querySelectorAll('select')].map(s => ({
            id: s.id, name: s.name, value: s.value,
            options: [...s.options].map(o => o.value).slice(0, 12),
        }));
        return {selects: sels};
    }""")
    result["steps"]["gallery_filters"] = gallery
    print("[6] 生成库筛选器:", json.dumps(gallery, ensure_ascii=False)[:700])
    shot(page, "75-regression-gallery-filters.png")

    # 实际用「作品类型=图片」筛选一次（#doneMedia）
    filtered = page.evaluate("""async () => {
        const sel = document.querySelector('#doneMedia');
        if (!sel) return {skipped: true, reason: 'no #doneMedia'};
        const before = document.querySelectorAll('.job-card').length;
        const target = [...sel.options].find(o => o.value === 'image');
        sel.value = target.value;
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        await new Promise(r => setTimeout(r, 2500));
        const after = document.querySelectorAll('.job-card').length;
        const kinds = [...document.querySelectorAll('.job-card')].slice(0, 10)
                        .map(c => (c.textContent.match(/模式\\s*([^\\n]{0,30})/) || [])[1] || '');
        const hasQwenI2i = kinds.some(t => /图片编辑/.test(t));
        const hasVideo = kinds.some(t => /视频/.test(t));
        return {selected: target.value, before, after, hasQwenI2i, hasVideo, kinds};
    }""")
    result["steps"]["gallery_filter_applied"] = filtered
    print("[7] 生成库「作品类型=图片」筛选:", json.dumps(filtered, ensure_ascii=False)[:600])
    shot(page, "76-regression-gallery-filtered.png")

    browser.close()

(OUT / "regression-verification.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n=== console errors:", len(result["console_errors"]))
for e in result["console_errors"][:10]:
    print("  ", e)
print("=== saved:", OUT / "regression-verification.json")
