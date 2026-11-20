import os, time
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"
SHOTS = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots"

def state(pg):
    return pg.evaluate("""() => {
      const g=document.querySelector('.al-grid');
      const cards=document.querySelectorAll('.al-grid > *');
      const sub=document.querySelector('#alSub') || document.querySelector('.al-title-sub');
      return {gridExists: !!g, gridChildren: cards.length,
              subText: sub ? sub.textContent.trim().slice(0,60) : null,
              loading: (document.body.textContent||'').includes('加载中')};
    }""")

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx=b.new_context(viewport={"width":375,"height":812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg=ctx.new_page(); pg.goto(BASE, wait_until="domcontentloaded", timeout=90000); pg.wait_for_timeout(2500)
    tog=pg.query_selector('#navToggle'); tog.click(); pg.wait_for_timeout(800)
    pg.query_selector('#navAssets').click()
    for t in [1,3,6,10,15]:
        pg.wait_for_timeout(t*1000 if t==1 else (t - prev)*1000 if False else 0)
        pg.wait_for_timeout(0)
        prev=t
        print(f"  +{t}s ->", state(pg))
    pg.wait_for_timeout(8000)
    print("  after wait:", state(pg))
    pg.screenshot(path=os.path.join(SHOTS,"13c-narrow375-assets-loaded.png"))
    pg.screenshot(path=os.path.join(SHOTS,"13d-narrow375-assets-loaded-FULLPAGE.png"), full_page=True)
    print("saved 13c/13d")
    ctx.close()

    # 桌面同样对比
    ctx2=b.new_context(viewport={"width":1440,"height":900}, locale="zh-CN")
    pg2=ctx2.new_page(); pg2.goto(BASE, wait_until="domcontentloaded", timeout=90000); pg2.wait_for_timeout(2500)
    pg2.click('#navAssets'); pg2.wait_for_timeout(6000)
    print("  desktop assets:", state(pg2))
    b.close()
