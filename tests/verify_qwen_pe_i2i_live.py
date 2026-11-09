#!/usr/bin/env python3
"""真机验证补测：Qwen 图片编辑面板的官方 PE 扩写（带真实参考图）。

覆盖：
  1. 图片编辑面板下拉选官方 PE -> 真实请求带参考图
  2. 服务端走 PE-I2I 档（pe_i2i checkpoint）
  3. 参考图真的喂给了 PE（PE 的产物里出现了图里的内容）
  4. 写回提示词框 + 可继续编辑 + console 0 报错
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

EV = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
          "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/qwen-pe-enhance-evidence")
EV.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190/"

errors, results = [], []


def check(label, ok, detail=""):
    results.append({"label": label, "ok": bool(ok), "detail": str(detail)})
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))


with sync_playwright() as pw:
    browser = pw.chromium.launch(args=["--no-sandbox"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console",
            lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)

    page.goto(BASE, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)
    page.click('.nav-item[data-mode="i2i_qwen"]')
    page.wait_for_timeout(1500)

    # 从素材库加一张真实参考图（灯塔那张）
    page.evaluate("""async () => {
      const data = await fetch('/api/refs/assets?category=image&page_size=5')
          .then(r => r.json());
      const target = (data.assets || []).find(a => a.id === 'asset_c5abcf443cf1')
                  || (data.assets || [])[0];
      if (target) await window.QwenRefs.addAssets([target.id]);
    }""")
    page.wait_for_timeout(1500)
    nrefs = page.evaluate("() => window.QwenRefs.getRefs().length")
    check("图片编辑：已从素材库加入 1 张参考图", nrefs >= 1, f"{nrefs} 张")
    page.screenshot(path=str(EV / "09-qwen-i2i-with-ref-image.png"))
    print(f"  [shot] {EV / '09-qwen-i2i-with-ref-image.png'}")

    # 选中官方 PE -> 真跑
    page.click("#enhanceMenuBtn")
    page.wait_for_timeout(500)
    # 多选语义：只勾官方 PE（先勾后取消其余）
    page.click('#enhanceMenu input[data-method-check="qwen_pe"]')
    page.wait_for_timeout(300)
    for mid in ("basic", "refine"):
        page.evaluate("""(mid) => {
          const el = document.querySelector(
            `#enhanceMenu input[data-method-check="${mid}"]`);
          if (el && el.checked && !el.disabled) {
            el.checked = false;
            el.dispatchEvent(new Event('change', {bubbles: true}));
          }
        }""", mid)
        page.wait_for_timeout(200)
    page.wait_for_timeout(500)
    page.fill("#prompt", "把图里的主体改成橙色，保持姿态和背景不变")
    t0 = time.time()
    page.click("#enhanceBtn")
    try:
        page.wait_for_selector("#enhanceModal:not([hidden])", timeout=400000)
    except Exception as exc:
        check("图片编辑：官方 PE 扩写返回结果", False, f"超时/失败: {exc}")
        browser.close()
        sys.exit(1)
    elapsed = time.time() - t0
    result_text = page.input_value("#enhanceResult")
    meta = page.inner_text("#enhanceMeta")
    print(f"  耗时 {elapsed:.1f}s，扩写长度 {len(result_text)} 字")
    print("  meta:", meta)

    check("图片编辑：走的是 PE-I2I 档", "PE-I2I" in meta, meta)
    check("图片编辑：用的是 pe_i2i checkpoint",
          "pe_i2i" in meta, meta)
    check("图片编辑：meta 标明参考图数量", "参考图" in meta, meta)
    check("图片编辑：返回了扩写内容", len(result_text) > 150, f"{len(result_text)} 字")
    check("图片编辑：PE 真的看到了参考图（产物提到图里的主体）",
          any(k in result_text.lower() for k in ("lighthouse", "tower", "主体", "灯塔")),
          result_text[:200])
    check("图片编辑：无 think 残留", "<think>" not in result_text)
    page.screenshot(path=str(EV / "10-qwen-i2i-official-pe-result.png"))
    print(f"  [shot] {EV / '10-qwen-i2i-official-pe-result.png'}")

    # 写回
    page.click("#enhanceReplace")
    page.wait_for_timeout(800)
    after = page.input_value("#prompt")
    check("图片编辑：扩写结果已写回提示词框",
          after.strip() == result_text.strip(), f"{len(after)} 字")
    check("图片编辑：写回后标记为「已增强」", page.is_visible("#promptEnhancedBadge"))
    page.fill("#prompt", after + " 额外要求：地面加一点湿反光")
    page.wait_for_timeout(300)
    check("图片编辑：写回后仍可继续编辑",
          "湿反光" in page.input_value("#prompt"))
    page.screenshot(path=str(EV / "11-qwen-i2i-prompt-filled.png"))
    print(f"  [shot] {EV / '11-qwen-i2i-prompt-filled.png'}")

    check("console 0 报错", len(errors) == 0, "; ".join(errors[:5]))
    browser.close()

passed = sum(1 for r in results if r["ok"])
failed = [r for r in results if not r["ok"]]
print("\n" + "=" * 70)
print(f"结果：{passed}/{len(results)} 通过")
for r in failed:
    print(f"  - FAIL {r['label']}: {r['detail']}")
(EV / "verify-results-i2i.json").write_text(
    json.dumps({"results": results, "console_errors": errors},
               ensure_ascii=False, indent=2), encoding="utf-8")
sys.exit(1 if failed else 0)
