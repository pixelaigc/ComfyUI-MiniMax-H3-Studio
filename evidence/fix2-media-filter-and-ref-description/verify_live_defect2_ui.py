
import json, sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"; OUT=sys.argv[1]
console, pageerrors = [], []
posted = []   # 抓 /api/jobs 的 POST 体

with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: pageerrors.append(str(e)))
    def on_req(r):
        if r.method == "POST" and "/api/jobs" in r.url:
            try: posted.append({"url": r.url, "body": r.post_data})
            except Exception as e: posted.append({"url": r.url, "err": str(e)})
    pg.on("request", on_req)
    pg.goto(BASE+"/", wait_until="networkidle"); time.sleep(3)

    # 切到 H3-Image 图片编辑
    pg.evaluate("""() => {
        const items=[...document.querySelectorAll('.nav-item[data-mode]')];
        const hit=items.find(x=>x.dataset.mode==='i2i_h3image');
        if(hit) hit.click(); else console.log('NO i2i_h3image NAV');
    }""")
    time.sleep(4)
    mode = pg.evaluate("() => ({mode: window.__h3state ? window.__h3state().mode : '?', promptPanel: !!document.querySelector('#imgRefAdd')})")
    print("MODE", json.dumps(mode, ensure_ascii=False))
    pg.screenshot(path=f"{OUT}/09_h3image_i2i_panel.png")

    # 打开素材库选择器（从素材库添加）
    has_btn = pg.evaluate("() => !!document.querySelector('#imgRefAdd')")
    print("HAS_IMGREF_ADD", has_btn)
    if has_btn:
        pg.click("#imgRefAdd"); time.sleep(3)
        pg.screenshot(path=f"{OUT}/10_asset_picker.png")
        # 选第一张图
        picked = pg.evaluate("""() => {
            const cards=[...document.querySelectorAll('[data-asset-id], .al-card, .picker-item')];
            if(!cards.length) return null;
            cards[0].click();
            return cards[0].getAttribute('data-asset-id') || cards[0].textContent.slice(0,40);
        }""")
        print("PICKED", picked)
        time.sleep(2)
        # 点确定
        pg.evaluate("""() => {
            const btns=[...document.querySelectorAll('button')].filter(b=>/确定|完成|添加|选择/.test(b.textContent||''));
            if(btns.length) btns[btns.length-1].click();
        }""")
        time.sleep(4)
        pg.screenshot(path=f"{OUT}/11_after_add_asset.png")

        # 读 <image1> 描述框的内容与行数
        info = pg.evaluate("""() => {
            const ta=document.querySelector('#imgRefDescList textarea');
            if(!ta) return null;
            return {rows: ta.rows, valueLen: ta.value.length, lineCount: ta.value.split('\\n').length,
                    head: ta.value.slice(0,60), tail: ta.value.slice(-60)};
        }""")
        print("IMG1_TEXTAREA", json.dumps(info, ensure_ascii=False))
    print("CONSOLE", json.dumps(console, ensure_ascii=False))
    print("PAGEERRORS", json.dumps(pageerrors, ensure_ascii=False))
    b.close()
