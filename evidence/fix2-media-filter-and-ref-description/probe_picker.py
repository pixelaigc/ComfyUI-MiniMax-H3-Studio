
import json, sys, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"; OUT=sys.argv[1]
with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.goto(BASE+"/", wait_until="networkidle"); time.sleep(3)
    pg.evaluate("() => { const items=[...document.querySelectorAll('.nav-item[data-mode]')]; const hit=items.find(x=>x.dataset.mode==='i2i_h3image'); if(hit) hit.click(); }")
    time.sleep(4)
    pg.click("#imgRefAdd"); time.sleep(3)
    # 探测 picker 的 DOM 结构
    dom = pg.evaluate("""() => {
      const ids = [...document.querySelectorAll('[id]')].map(e=>e.id).filter(i=>/pick|modal|asset/i.test(i));
      const cls = [...document.querySelectorAll('div,ul')].map(e=>e.className).filter(c=>typeof c==='string' && /pick|modal/i.test(c)).slice(0,20);
      const vis = [...document.querySelectorAll('div')].filter(e=>{const s=getComputedStyle(e); return s.display!=='none' && /picker|modal/i.test(e.className||'')}).map(e=>({cls:e.className, id:e.id, kids:e.children.length}));
      return {ids, cls, vis};
    }""")
    print(json.dumps(dom, ensure_ascii=False, indent=1)[:3000])
    pg.screenshot(path=f"{OUT}/10_asset_picker.png")
    b.close()
