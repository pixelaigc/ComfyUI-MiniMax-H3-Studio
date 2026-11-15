
import json, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"
with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.goto(BASE+"/", wait_until="domcontentloaded", timeout=60000); time.sleep(6)
    pg.evaluate("() => { const items=[...document.querySelectorAll('.nav-item[data-mode]')]; const hit=items.find(x=>x.dataset.mode==='i2i_h3image'); if(hit) hit.click(); }")
    time.sleep(4)
    btns = pg.evaluate("""() => [...document.querySelectorAll('button')].map(b=>({
        id:b.id, cls:(b.className||'').slice(0,40), txt:(b.textContent||'').trim().slice(0,30),
        vis: b.offsetParent!==null, dis:b.disabled})).filter(b=>b.vis && /生成|提交|开始/.test(b.txt))""")
    print("GEN_BUTTONS", json.dumps(btns, ensure_ascii=False, indent=1))
    allbtns = pg.evaluate("""() => [...document.querySelectorAll('button')].filter(b=>b.offsetParent!==null).map(b=>b.id+'|'+b.className+'|'+(b.textContent||'').trim().slice(0,20)).slice(0,40)""")
    print("ALL_VISIBLE", json.dumps(allbtns, ensure_ascii=False, indent=1))
    b.close()
