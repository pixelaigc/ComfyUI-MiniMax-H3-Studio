"""D11 收口轮：线上 8190 的**真实浏览器**验收。

验证三件事（都是用户可见的）：
  1. 页面加载无 console error / pageError
  2. MOSS 面板里 D1（融合 qkv）条目带「融合 qkv · 自动转换」标签
  3. 派生文件不再出现在可选列表里（141 -> 138，且无 h3studio_mossnorm__）

用法：python3 verify_live_d11_final.py <out.json>
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/d11_live_verify.json")

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

    # 页面内 fetch 三族清单（与接口直连同源）
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
              d1: av.filter(i => i.name === 'moss-effects/moss_image_sft_look_lora.safetensors')
                    .map(i => ({qkv_shape: i.qkv_shape,
                                qkv_convertible: i.qkv_convertible,
                                compatible: i.compatible,
                                attention_hit: i.attention_hit})),
              d2Present: av.some(i => i.name.includes('animeEnhance')),
            };
          }
          return out;
        }""")

    # 切到 MOSS 模式，渲染面板，看 D1 条目的标签
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

    # 在「已添加」列表里放入 D1 + 一个分离形态条目，检查标签
    page.evaluate(
        """() => {
          const h = window.H3Studio.__loraTestHooks;
          const d1 = h.stores.MOSS.available.find(
            i => i.name === 'moss-effects/moss_image_sft_look_lora.safetensors');
          const split = h.stores.MOSS.available.find(
            i => i.qkv_shape === 'split');
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

    # 详情面板的「权重形态」行
    page.evaluate(
        """() => {
          const el = [...document.querySelectorAll('#imgLoraList .img-lora-item')]
            .find(e => e.querySelector('.img-lora-name')?.textContent?.includes('Fizgig'));
          if (el) el.querySelector('.img-lora-info-btn').click();
        }""")
    page.wait_for_timeout(500)
    result["infoBody"] = page.inner_text("#loraInfoBody")

    page.screenshot(path="/tmp/d11_live_moss_panel.png", full_page=False)
    browser.close()

result["consoleErrors"] = errors
result["consoleWarnings"] = warnings
result["pageErrors"] = page_errors
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
print(f"\n结果已存 {OUT}")
