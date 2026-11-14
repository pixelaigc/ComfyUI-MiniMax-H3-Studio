"""D11 收口轮第三轮：线上 8190 的**真实浏览器**验收。

验证（都是用户可见的）：
  1. 页面加载无 console error / warning / pageError
  2. 三族清单：available 数、compatible=false 数、派生文件泄漏数
  3. MOSS 面板里 D1（融合 qkv，已验证）条目带「融合 qkv · 已验证可转换」标签
  4. 分离形态条目**不**带任何 qkv 标签
  5. 详情面板「权重形态」行如实写明；「基座适用性」不再兜底成「原生」

用法：python3 verify_live_d11_r2.py <out.json>
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/d11_r2_live.json")

result = {"base": BASE}
errors, warnings, page_errors = [], [], []

with sync_playwright() as p:
    browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    page.on("console", lambda m: (errors if m.type == "error" else warnings).append(m.text)
            if m.type in ("error", "warning") else None)
    page.on("pageerror", lambda e: page_errors.append(str(e)))

    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)

    result["title"] = page.title()
    result["readyState"] = page.evaluate("() => document.readyState")
    result["domLength"] = page.evaluate("() => document.documentElement.outerHTML.length")

    result["api"] = page.evaluate(
        """async () => {
          const out = {};
          for (const b of ['Z-Image', 'Qwen-Image 2.1', 'MOSS-Image']) {
            const r = await fetch('/api/image-loras?base=' + encodeURIComponent(b));
            const d = await r.json();
            const av = d.available || [];
            out[b] = {
              http: r.status,
              available: av.length,
              compatibleFalse: av.filter(i => !i.compatible).length,
              derivedLeak: av.filter(i => /h3studio_(mossnorm__|ctx_)/.test(i.name)).length,
              convStatus: av.reduce((a,i) => (a[i.conversion_status||'-']=(a[i.conversion_status||'-']||0)+1, a), {}),
              d1: av.filter(i => i.name === 'moss-effects/moss_image_sft_look_lora.safetensors')
                    .map(i => ({compatible: i.compatible, qkv_shape: i.qkv_shape,
                                qkv_convertible: i.qkv_convertible,
                                conversion_status: i.conversion_status,
                                attention_hit: i.attention_hit})),
              d2Present: av.some(i => i.name.includes('animeEnhance')),
              lokrPresent: ['MOSSRealisticSnapshotV1','lokr-moss-kodachrome-v1',
                            'lokr-moss-kodachrome-v2','skin_tone_moss_NW']
                           .filter(t => av.some(i => i.name.includes(t))),
            };
          }
          return out;
        }""")

    # 切 MOSS 模式，渲染面板
    page.evaluate("() => { if (window.__h3State) window.__h3State.mode = 't2i_moss'; }")
    page.wait_for_timeout(600)
    page.evaluate(
        """() => {
          const h = window.H3Studio && window.H3Studio.__loraTestHooks;
          if (!h) throw new Error('LoRA 测试钩子未暴露');
          h.renderLoraList();
          h.renderLoraSelect();
        }""")
    page.wait_for_timeout(800)

    # 把 D1（融合，已验证）和一个分离形态条目放进「已添加」列表
    page.evaluate(
        """() => {
          const h = window.H3Studio.__loraTestHooks;
          const av = h.stores.MOSS.available;
          const d1 = av.find(i => i.name === 'moss-effects/moss_image_sft_look_lora.safetensors');
          const split = av.find(i => i.qkv_shape === 'split');
          const list = window.__h3State.imgMoss.loras;
          list.splice(0, list.length);
          if (d1) list.push({name: d1.name, strength: 1.0, enabled: true, trigger: ''});
          if (split) list.push({name: split.name, strength: 1.0, enabled: true, trigger: ''});
          h.renderLoraList();
        }""")
    page.wait_for_timeout(800)

    result["badges"] = page.evaluate(
        """() => [...document.querySelectorAll('#imgLoraList .img-lora-item')].map(el => ({
            name: el.querySelector('.img-lora-name')?.textContent?.trim() || '',
            tags: [...el.querySelectorAll('.img-lora-tag')].map(t => t.textContent.trim()),
        }))""")

    # 详情面板
    page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('#imgLoraList .img-lora-item')]
            .find(e => e.querySelector('.img-lora-name')?.textContent?.includes('Fizgig'));
          if (el) el.querySelector('.img-lora-info-btn').click();
        }""")
    page.wait_for_timeout(500)
    result["infoBody"] = page.inner_text("#loraInfoBody")

    # 找一个 base_verdict 为空的条目，确认不再显示「原生」
    result["emptyVerdictSample"] = page.evaluate(
        """() => {
          const h = window.H3Studio.__loraTestHooks;
          const it = h.stores.MOSS.available.find(i => !i.base_verdict);
          return it ? {name: it.name, base_verdict: it.base_verdict || ''} : null;
        }""")

    page.screenshot(path="/tmp/d11_r2_live_panel.png", full_page=False)
    browser.close()

result["consoleErrors"] = errors
result["consoleWarnings"] = warnings
result["pageErrors"] = page_errors
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2)[:4500])
print(f"\n结果已存 {OUT}")
