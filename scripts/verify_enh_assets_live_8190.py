#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
线上（8190）真实浏览器取证：引用素材 -> 输入提示词 -> 点「增强」。

【为什么要有这个脚本】自动化测试是在**临时实例**上跑的；
交付要求是「必须用真实浏览器在 8190 实际打开界面走一遍」——
所以这里对着**线上服务**跑，并把
  * 截图（面板状态 + 增强结果弹窗）
  * 网络请求体（证明 asset_refs 真的进了请求）
  * 网络响应体（证明服务端真的把素材解析并回显了）
全部落盘成证据文件。

用法：python3 scripts/verify_enh_assets_live_8190.py
"""
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = Path(os.environ.get("H3_EVIDENCE_DIR",
                          "/root/projects/h3studio-fix-enhance-assets/evidence/enh-assets-live-8190"))
OUT.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    calls = []
    responses = []
    console_errors = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 950})
        page = ctx.new_page()

        def on_request(req):
            if req.method == "POST" and req.url.endswith("/api/enhance"):
                try:
                    body = json.loads(req.post_data or "{}")
                except Exception:
                    body = {"_raw": req.post_data}
                calls.append({"url": req.url, "body": body})
                log(f"--> POST /api/enhance  asset_refs={json.dumps(body.get('asset_refs'), ensure_ascii=False)}")

        def on_response(resp):
            if resp.request.method == "POST" and resp.url.endswith("/api/enhance"):
                try:
                    body = resp.json()
                except Exception:
                    body = {"_raw": "<非 JSON>"}
                responses.append({"status": resp.status, "body": body})
                log(f"<--  POST /api/enhance  status={resp.status}")

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: console_errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        log(f"打开 {BASE}")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # ---- 1. 切到「参考生视频」(r2v)：有素材槽位的面板 ----
        log("切到 r2v（参考生视频）面板")
        page.click('.nav-item[data-mode="r2v"]')
        page.wait_for_timeout(1500)

        # ---- 2. 从素材库选一张真实图片填进「参考 1」 ----
        log("打开素材库选择器")
        slot = page.query_selector('#uploadRow .up-slot[data-key="ref0"]')
        if slot is None:
            raise SystemExit("找不到 r2v 的 ref0 槽位")
        page.evaluate(
            """() => {
              const el = document.querySelector('#uploadRow .up-slot[data-key="ref0"]');
              const btn = el && el.querySelector('.pick-asset');
              if (btn) btn.click();
            }""")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "01-picker-open.png"), full_page=False)

        # 点第一张素材卡 -> 确定
        # 【为什么要等卡片真的出现】/api/assets/picker 在本机要几秒
        # （素材库在 /mnt/usb2t 外接盘，200 条要逐个 stat）。
        # 卡片没渲染出来时 querySelector 返回 null -> 「素材库里没有可选素材」
        # 是**假象**，实测第一版脚本就踩了这个。
        try:
            page.wait_for_selector("#alPickerBody .al-card", timeout=30000)
        except Exception as exc:
            raise SystemExit(f"素材库卡片没渲染出来：{exc}")
        picked = page.evaluate(
            """() => {
              const card = document.querySelector('#alPickerBody .al-card');
              if (!card) return null;
              const id = card.dataset.id;
              card.click();
              return id;
            }""")
        log(f"选中素材：{picked}")
        if not picked:
            raise SystemExit("素材库里没有可选素材（先往素材库上传一张）")
        page.wait_for_timeout(1200)
        # 「确定使用」按钮
        page.evaluate(
            """() => {
              const btns = [...document.querySelectorAll('#alPickerMask button')];
              const ok = btns.find(b => /确定|使用|完成/.test(b.textContent || ''));
              if (ok) ok.click();
            }""")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / "02-asset-referenced.png"), full_page=False)

        state_snapshot = page.evaluate(
            """() => ({
              mode: state.mode,
              assetRefs: JSON.parse(JSON.stringify(state.assetRefs || {})),
            })""")
        log(f"面板素材状态：{json.dumps(state_snapshot, ensure_ascii=False)}")

        # ---- 3. 输入提示词 ----
        prompt = "一个女孩站在雪地里回头微笑"
        page.evaluate(
            """(t) => {
              const el = document.querySelector('#prompt');
              el.value = t;
              el.dispatchEvent(new Event('input', {bubbles: true}));
            }""", prompt)
        page.wait_for_timeout(600)

        # ---- 4. 点「增强」 ----
        log("点「增强」按钮（真实调用本地多模态模型）")
        t0 = time.time()
        page.evaluate(
            """() => {
              const btn = document.querySelector('#enhanceBtn');
              if (!btn) throw new Error('找不到增强按钮');
              btn.click();
            }""")

        # 等增强结果弹窗出现（本地模型可能排队，给足时间）
        try:
            page.wait_for_function(
                """() => {
                  const m = document.querySelector('#enhanceModal');
                  return m && !m.hidden;
                }""", timeout=600000)
            log(f"增强完成，用时 {time.time() - t0:.1f}s")
        except Exception as exc:
            log(f"等待增强结果超时：{exc}")

        page.wait_for_timeout(1500)
        page.screenshot(path=str(OUT / "03-enhance-result.png"), full_page=False)

        result = page.evaluate(
            """() => {
              const m = document.querySelector('#enhanceModal');
              const r = document.querySelector('#enhanceResult');
              const meta = document.querySelector('#enhanceMeta');
              const hint = document.querySelector('#enhanceHint');
              return {
                modal_visible: !!(m && !m.hidden),
                enhanced: r ? r.value : "",
                meta: meta ? meta.innerText : "",
                hint: hint ? hint.innerText : "",
              };
            }""")
        log(f"结果弹窗可见={result['modal_visible']}  "
            f"增强长度={len(result['enhanced'])}")
        log(f"meta：{result['meta'][:300]}")

        evidence = {
            "base": BASE,
            "prompt": prompt,
            "picked_asset": picked,
            "panel_state": state_snapshot,
            "requests": calls,
            "responses": responses,
            "ui_result": result,
            "console_errors": console_errors,
        }
        (OUT / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"证据已写入 {OUT / 'evidence.json'}")

        browser.close()

    # ---- 判定 ----
    ok = True
    if not calls:
        print("!! 没有发出 /api/enhance 请求")
        ok = False
    else:
        refs = calls[-1]["body"].get("asset_refs")
        print(f"请求体 asset_refs = {json.dumps(refs, ensure_ascii=False)}")
        if not refs:
            print("!! 请求体里没有素材")
            ok = False
    if not responses:
        print("!! 没有拿到 /api/enhance 响应")
        ok = False
    else:
        assets = responses[-1]["body"].get("assets")
        print(f"响应体 assets = {json.dumps(assets, ensure_ascii=False)}")
        if not assets or not assets.get("image_count"):
            print("!! 响应里没有素材回显")
            ok = False
    if console_errors:
        print(f"!! 控制台报错：{console_errors[:5]}")
    print("=== 线上取证", "通过" if ok else "失败", "===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
