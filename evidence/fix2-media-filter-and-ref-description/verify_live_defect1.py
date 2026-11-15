
import json, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190"
OUT = sys.argv[1]
console, pageerrors, reqs = [], [], []

def snap(page, name):
    page.screenshot(path=f"{OUT}/{name}.png", full_page=False)
    print("SHOT", name)

def media_state(page):
    return page.evaluate("""() => ({
        doneMedia: document.querySelector('#doneMedia')?.value,
        doneSearch: document.querySelector('#doneSearch')?.value,
        genTypeActive: document.querySelector('#genTypeTabs button.active')?.textContent?.trim(),
        tab: document.querySelector('.jobs-tabs button[data-st].active')?.textContent?.trim(),
        hash: location.hash,
        cards: document.querySelectorAll('#jobsGrid [data-jid]').length,
        kinds: (() => {
          const out = {};
          document.querySelectorAll('#jobsGrid [data-jid]').forEach(c => {
            const k = c.querySelector('.job-preview video') ? 'video'
                    : c.querySelector('.job-preview img') ? 'image'
                    : c.querySelector('.job-preview audio') ? 'audio' : 'other';
            out[k] = (out[k]||0)+1;
          });
          return out;
        })(),
    })""")

with sync_playwright() as p:
    b = p.chromium.launch(args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width":1600,"height":950})
    page = ctx.new_page()
    page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: pageerrors.append(str(e)))
    page.on("request", lambda r: reqs.append(r.url) if "/api/jobs" in r.url else None)

    page.goto(BASE + "/", wait_until="networkidle"); time.sleep(3)
    print("=== STEP1 初始（全部 + 全部类型）===")
    s0 = media_state(page); print("STATE0", json.dumps(s0, ensure_ascii=False))
    snap(page, "01_initial_all_types")

    # 点左侧「minimax-h3 长片拼接」
    print("=== STEP2 点长片拼接 ===")
    clicked = page.evaluate("""() => {
        const items = [...document.querySelectorAll('.nav-item, .nav-model-item')];
        const hit = items.find(x => (x.textContent||'').includes('长片拼接'));
        if (hit) { hit.click(); return hit.textContent.trim(); }
        return null;
    }""")
    print("CLICKED", clicked); time.sleep(4)
    s1 = media_state(page); print("STATE1_STITCH", json.dumps(s1, ensure_ascii=False))
    snap(page, "02_stitch_panel_all_types")
    reqs.clear()

    # 在长片拼接页等一轮轮询，看请求里有没有 media=video
    time.sleep(3)
    print("REQS_ON_STITCH", json.dumps(reqs[-4:], ensure_ascii=False))

    # 切回「生成」页
    print("=== STEP3 切回生成页 ===")
    page.evaluate("""() => {
        const items = [...document.querySelectorAll('.nav-item[data-mode]')];
        const hit = items.find(x => x.dataset.mode === 't2i');
        if (hit) hit.click();
    }""")
    time.sleep(4)
    s2 = media_state(page); print("STATE2_BACK", json.dumps(s2, ensure_ascii=False))
    snap(page, "03_back_to_create_all_types")
    print("REQS_BACK", json.dumps(reqs[-4:], ensure_ascii=False))

    # 按类型筛选：文生图
    print("=== STEP4 按「文生图」筛选 ===")
    reqs.clear()
    page.evaluate("""() => {
        const btns = [...document.querySelectorAll('#genTypeTabs button')];
        const hit = btns.find(b => b.textContent.includes('Z-IMAGE 文生图'));
        if (hit) hit.click();
    }""")
    time.sleep(4)
    s3 = media_state(page); print("STATE3_T2I", json.dumps(s3, ensure_ascii=False))
    snap(page, "04_filter_t2i")
    print("REQS_T2I", json.dumps(reqs[-3:], ensure_ascii=False))

    # 用「作品类型」下拉选图片
    print("=== STEP5 下拉选「图片」 ===")
    reqs.clear()
    page.select_option("#doneMedia", "image"); time.sleep(4)
    s4 = media_state(page); print("STATE5_MEDIA_IMAGE", json.dumps(s4, ensure_ascii=False))
    snap(page, "05_filter_media_image")
    print("REQS_MEDIA_IMAGE", json.dumps(reqs[-3:], ensure_ascii=False))

    # 重置
    print("=== STEP6 重置筛选 ===")
    page.click("#doneFilterReset"); time.sleep(3)
    s5 = media_state(page); print("STATE6_RESET", json.dumps(s5, ensure_ascii=False))
    snap(page, "06_after_reset")

    print("CONSOLE", json.dumps(console, ensure_ascii=False))
    print("PAGEERRORS", json.dumps(pageerrors, ensure_ascii=False))
    b.close()
