#!/usr/bin/env python3
"""8190 线上真机验证 —— Qwen-Image 2.1 参考图素材库化。

真实 Chromium → 真实 8190 服务，逐项点击 + 截图 + 结构化断言。
不是「看代码对不对」，是「用户在界面上能不能真的用」。
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-live")
OUT.mkdir(parents=True, exist_ok=True)

result = {"base": BASE, "steps": {}, "console_errors": [], "screenshots": []}


def shot(page, name):
    p = OUT / name
    page.screenshot(path=str(p))
    result["screenshots"].append(str(p))
    print(f"  [shot] {p}")
    return str(p)


def js(page, expr):
    return page.evaluate(expr)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(viewport={"width": 1600, "height": 1100})
    page = ctx.new_page()

    page.on("console", lambda m: result["console_errors"].append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
    page.on("pageerror", lambda e: result["console_errors"].append(f"pageerror: {e}"))

    # ---------- 1. 打开 + 切到「图片编辑」 ----------
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_timeout(1200)
    page.click('a.nav-item[data-mode="i2i_qwen"]')
    page.wait_for_timeout(1500)

    info = js(page, """() => {
        const fi = document.querySelector('#imgRefField');
        return {
            mode: (window.__h3state && window.__h3state.mode) || null,
            refFieldVisible: fi ? getComputedStyle(fi).display !== 'none' : false,
            addBtnText: (document.querySelector('#imgRefAdd')||{}).textContent || null,
            hasFileInput: !!document.querySelector('#imgRefInput'),
            anyFileInput: !!document.querySelector('#imgRefField input[type=file]'),
            sortBtnExists: !!document.querySelector('#imgRefSort'),
            hasQwenRefs: typeof window.QwenRefs === 'object',
        };
    }""")
    result["steps"]["panel"] = info
    print("[1] 图片编辑面板:", json.dumps(info, ensure_ascii=False))
    shot(page, "60-live-i2i-panel-asset-library.png")

    # ---------- 2. 点「+ 从素材库添加」→ 打开素材库 → 多选 3 张 ----------
    page.click("#imgRefAdd")
    page.wait_for_timeout(1500)
    picker_open = js(page, """() => {
        const m = document.querySelector('#alPickerMask');
        return {open: !!m && m.classList.contains('open'),
                cards: document.querySelectorAll('#alPickerBody .al-card').length};
    }""")
    result["steps"]["picker"] = picker_open
    print("[2] 素材库选择器:", json.dumps(picker_open, ensure_ascii=False))
    shot(page, "61-live-asset-library-picker.png")

    # 多选前 3 张卡片（真实点击）
    page.evaluate("""() => {
        const cards = [...document.querySelectorAll('#alPickerBody .al-card')];
        cards.slice(0, 3).forEach(c => c.click());
    }""")
    page.wait_for_timeout(600)
    picked = js(page, """() => document.querySelectorAll('#alPickerBody .al-card.picked').length""")
    result["steps"]["picker_picked"] = picked
    print(f"[2] 已选 {picked} 张")
    shot(page, "62-live-picker-3-selected.png")

    page.click("#alPickerOk")
    page.wait_for_timeout(2000)

    refs_after = js(page, """() => {
        const r = (window.QwenRefs ? window.QwenRefs.getRefs() : []);
        return {
            count: r.length,
            items: r.map(x => ({asset_id: x.asset_id, name: x.name,
                                 desc: x.description, src: x.description_source,
                                 tags: x.tags, missing: !!x.missing})),
            domItems: document.querySelectorAll('#imgRefList .img-ref-item').length,
            idxBadges: [...document.querySelectorAll('#imgRefList .img-ref-idx')].map(e => e.textContent.trim()),
            hint: (document.querySelector('#imgRefHint')||{}).textContent || null,
            descRows: document.querySelectorAll('#imgRefDescList .img-ref-desc-row').length,
            descTags: [...document.querySelectorAll('#imgRefDescList .img-ref-desc-tag')].map(e => e.textContent.trim()),
            pendingBadge: [...document.querySelectorAll('#imgRefList .img-ref-badge')].map(e => e.textContent.trim()),
            markerNote: (document.querySelector('#imgRefMarkerNote')||{}).textContent || null,
            detailOpen: (document.querySelector('#imgRefDetail')||{}).open,
        };
    }""")
    result["steps"]["refs_added"] = refs_after
    print("[3] 已加入参考图:", json.dumps(refs_after, ensure_ascii=False)[:900])
    shot(page, "63-live-3-refs-from-library.png")

    # ---------- 3. 手写补充一条描述（验证可编辑 + 标为 user） ----------
    ta = page.locator('#imgRefDescList textarea').first
    ta.click()
    ta.fill("低角度广角镜头，画面右侧耸立着一座白色圆柱形灯塔，塔身表面有风化痕迹。")
    ta.dispatch_event("change")
    page.wait_for_timeout(800)
    after_man = js(page, """() => {
        const r = window.QwenRefs.getRefs();
        return {desc: r[0] && r[0].description, src: r[0] && r[0].description_source,
                pending: [...document.querySelectorAll('#imgRefList .img-ref-badge')].map(e=>e.textContent.trim())};
    }""")
    result["steps"]["manual_desc"] = after_man
    print("[4] 手写补充后:", json.dumps(after_man, ensure_ascii=False))
    shot(page, "64-live-manual-desc-filled.png")

    # ---------- 4. 提示词写入 <imageN> 占位 ----------
    page.fill("#prompt", "把这三张图合成一张：保持 <image1> 的灯塔与黄昏光线，"
                          "把 <image2> 的雪山倒影放进 <image1> 的海面，"
                          "把 <image3> 的传感器模块放在前景沙滩上。")
    page.wait_for_timeout(500)

    # ---------- 5. 智能排序 ----------
    before = js(page, "() => window.QwenRefs.getRefs().map(r => r.asset_id).join(',')")
    prompt_text = page.input_value("#prompt")
    page.click("#imgRefSort")
    page.wait_for_timeout(2500)
    after = js(page, """() => ({
        ids: window.QwenRefs.getRefs().map(r => r.asset_id),
        idx: [...document.querySelectorAll('#imgRefList .img-ref-idx')].map(e=>e.textContent.trim()),
        prompt: document.querySelector('#prompt').value,
        sortBtnText: (document.querySelector('#imgRefSort')||{}).textContent||null,
    })""")
    result["steps"]["smart_sort"] = {"before": before, "after": after}
    print(f"[5] 智能排序 before={before[:40]}... after={','.join(after['ids'])[:40]}...")
    shot(page, "65-live-smart-sort.png")

    # ---------- 6. 移除中间一张 → 重编号 ----------
    del_idx = 1 if len(after["ids"]) >= 2 else 0
    page.evaluate(f"""() => {{
        const btns = document.querySelectorAll('#imgRefList [data-del]');
        if (btns[{del_idx}]) btns[{del_idx}].click();
    }}""")
    page.wait_for_timeout(1200)
    after_rm = js(page, """() => ({
        ids: window.QwenRefs.getRefs().map(r => r.asset_id),
        idx: [...document.querySelectorAll('#imgRefList .img-ref-idx')].map(e=>e.textContent.trim()),
        descTags: [...document.querySelectorAll('#imgRefDescList .img-ref-desc-tag')].map(e=>e.textContent.trim()),
        prompt: document.querySelector('#prompt').value,
        hint: (document.querySelector('#imgRefHint')||{}).textContent||null,
    })""")
    result["steps"]["remove_renumber"] = after_rm
    print("[6] 移除后:", json.dumps(after_rm, ensure_ascii=False)[:600])
    shot(page, "66-live-after-remove-renumbered.png")

    # ---------- 7. 校验：故意只留 <image1> 而有 2 张图 → 应报缺 <image2> ----------
    page.fill("#prompt", "只引用 <image1> 的灯塔")
    page.wait_for_timeout(400)
    page.evaluate("""() => {
        // 触发 app.js 的提交前校验路径：直接调用 QwenRefs.validate
        window.__validateResult = window.QwenRefs.validate(document.querySelector('#prompt').value);
    }""")
    val = js(page, "() => window.__validateResult")
    result["steps"]["validate_mismatch"] = val
    print("[7] 校验结果:", json.dumps(val, ensure_ascii=False))
    shot(page, "67-live-validate-mismatch.png")

    # ---------- 8. 恢复 3 张（用于后续实跑） ----------
    # 先清空再加 —— 同一素材不重复加（addAssets 会去重）
    page.evaluate("""async () => {
        window.QwenRefs.clear();
        const data = await fetch('/api/refs/assets?category=image&page_size=3').then(r => r.json());
        const ids = (data.assets||[]).map(a => a.id);
        await window.QwenRefs.addAssets(ids);
    }""")
    page.wait_for_timeout(2000)
    restored = js(page, """() => ({
        count: window.QwenRefs.getRefs().length,
        idx: [...document.querySelectorAll('#imgRefList .img-ref-idx')].map(e=>e.textContent.trim()),
        descTags: [...document.querySelectorAll('#imgRefDescList .img-ref-desc-tag')].map(e=>e.textContent.trim()),
    })""")
    result["steps"]["restored_3"] = restored
    print("[8] 恢复:", json.dumps(restored, ensure_ascii=False))
    shot(page, "68-live-restored-3-refs.png")

    # ---------- 9. 失效素材标红 ----------
    # 用 API 造一个「素材已删除」的引用（前端 missing 路径）
    js(page, """() => {
        const r = window.QwenRefs.getRefs();
        window.QwenRefs.setRefs(r.map((x, i) => i === 0 ? Object.assign({}, x, {missing: true}) : x));
    }""")
    page.wait_for_timeout(800)
    miss = js(page, """() => ({
        badges: [...document.querySelectorAll('#imgRefList .img-ref-badge')].map(e=>e.textContent.trim()),
        missingCls: document.querySelectorAll('#imgRefList .img-ref-item.missing').length,
        note: (document.querySelector('#imgRefNote')||{}).textContent||null,
        validate: window.QwenRefs.validate(document.querySelector('#prompt').value),
    })""")
    result["steps"]["missing_badge"] = miss
    print("[9] 失效素材:", json.dumps(miss, ensure_ascii=False)[:500])
    shot(page, "69-live-missing-asset-red.png")

    browser.close()

(OUT / "live-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n=== console errors:", len(result["console_errors"]))
for e in result["console_errors"][:10]:
    print("  ", e)
print("=== JSON saved:", OUT / "live-verification.json")
