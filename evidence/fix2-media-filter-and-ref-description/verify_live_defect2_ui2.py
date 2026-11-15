
import json, sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"; OUT=sys.argv[1]
console, pageerrors, posted = [], [], []

with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
    pg.on("pageerror", lambda e: pageerrors.append(str(e)))
    def on_req(r):
        if r.method=="POST" and "/api/jobs" in r.url:
            try: posted.append({"url":r.url, "body":r.post_data})
            except Exception as e: posted.append({"url":r.url, "err":str(e)})
    pg.on("request", on_req)
    pg.goto(BASE+"/", wait_until="networkidle"); time.sleep(3)

    for MODE, LABEL in [("i2i_h3image","H3-IMAGE 图片编辑"), ("i2i_qwen","Qwen 图片编辑")]:
        print(f"\n########## {LABEL} ({MODE}) ##########")
        posted.clear()
        pg.evaluate(f"""() => {{
            const items=[...document.querySelectorAll('.nav-item[data-mode]')];
            const hit=items.find(x=>x.dataset.mode==='{MODE}');
            if(hit) hit.click();
        }}""")
        time.sleep(4)
        pg.click("#imgRefAdd"); time.sleep(3)

        # 选前 2 张图片素材
        picked = pg.evaluate("""() => {
            const body=document.querySelector('.al-picker-body');
            if(!body) return {err:'no body'};
            const cards=[...body.querySelectorAll('[data-id], .al-pick-item, [data-asset-id]')];
            if(!cards.length) return {err:'no cards', html: body.innerHTML.slice(0,400)};
            const out=[];
            cards.slice(0,2).forEach(c=>{ out.push(c.getAttribute('data-id')||c.getAttribute('data-asset-id')); c.click(); });
            return {n: cards.length, picked: out};
        }""")
        print("PICK", json.dumps(picked, ensure_ascii=False))
        time.sleep(1)
        # 确认
        confirm = pg.evaluate("""() => {
            const foot=document.querySelector('#alPickerFoot');
            if(!foot) return null;
            const btns=[...foot.querySelectorAll('button')];
            return btns.map(b=>({t:(b.textContent||'').trim(), id:b.id, dis:b.disabled}));
        }""")
        print("FOOT", json.dumps(confirm, ensure_ascii=False))
        pg.evaluate("""() => {
            const foot=document.querySelector('#alPickerFoot');
            const btns=[...(foot?foot.querySelectorAll('button'):[])];
            const ok=btns.find(b=>/确定|添加|完成|选择/.test(b.textContent||''));
            if(ok) ok.click();
        }""")
        time.sleep(5)
        pg.screenshot(path=f"{OUT}/12_{MODE}_after_add.png")

        info = pg.evaluate("""() => {
            const ta=document.querySelector('#imgRefDescList textarea');
            const tags=[...document.querySelectorAll('.img-ref-desc-tag')].map(t=>t.textContent.trim());
            if(!ta) return {err:'no textarea', tags};
            return {tags, rows: ta.rows, len: ta.value.length,
                    lines: ta.value.split('\\n').length,
                    head: ta.value.slice(0,80), tail: ta.value.slice(-90),
                    hasAllSections: ['【主体人物】','【服装与配饰】','【场景与环境】','【光线与色彩】','【其他值得注意的细节】'].every(s=>ta.value.includes(s))};
        }""")
        print("IMG1_DESC", json.dumps(info, ensure_ascii=False))

        # 抓 window.QwenRefs.getRefs() 的实际 description
        refs = pg.evaluate("""() => {
            if(!window.QwenRefs) return null;
            return window.QwenRefs.getRefs().map(r=>({asset_id:r.asset_id, len:(r.description||'').length,
                lines:(r.description||'').split('\\n').length, src:r.description_source,
                tail:(r.description||'').slice(-60)}));
        }""")
        print("QWENREFS", json.dumps(refs, ensure_ascii=False))

    print("\nCONSOLE", json.dumps(console, ensure_ascii=False))
    print("PAGEERRORS", json.dumps(pageerrors, ensure_ascii=False))
    b.close()
