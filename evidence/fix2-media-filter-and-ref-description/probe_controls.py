
import json, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"
with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.goto(BASE+"/", wait_until="networkidle"); time.sleep(2)
    info = pg.evaluate("""() => {
      const sels = [...document.querySelectorAll('select')].map(s=>({id:s.id, opts:[...s.options].map(o=>o.value+'|'+o.textContent.trim())}));
      const labs = [...document.querySelectorAll('.jobs-head label, .jobs-head span')].map(x=>x.textContent.trim());
      return {sels, labs, genTypeBtns: [...document.querySelectorAll('#genTypeTabs button')].map(b=>b.textContent.trim())};
    }""")
    print(json.dumps(info, ensure_ascii=False, indent=1))
    b.close()
