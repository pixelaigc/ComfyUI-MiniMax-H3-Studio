import os, json
from playwright.sync_api import sync_playwright
from PIL import Image
BASE = "http://127.0.0.1:8190/"
SHOTS = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots"

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx=b.new_context(viewport={"width":1440,"height":900}, locale="zh-CN")
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3200)

    # longfilm: #stitchPanel 局部
    pg.click('#sidebar button.nav-model-item[data-mode="longfilm"][data-model-key="h3_longfilm"]'); pg.wait_for_timeout(1800)
    sp=pg.query_selector('#stitchPanel')
    if sp: sp.screenshot(path=os.path.join(SHOTS,"02-mode-longfilm-panel.png")); print("stitchPanel captured")

    # 采样 up-slot 文字像素 & stitchBtn
    samples = pg.evaluate("""() => {
      const out={};
      // stitchBtn disabled
      const sb=document.querySelector('#stitchBtn');
      if(sb){const cs=getComputedStyle(sb); out.stitchBtn={color:cs.color,bg:cs.backgroundColor,disabled:sb.disabled,text:sb.textContent.trim()};}
      return out;
    }""")
    print("samples:", samples)

    # 1440 下 media 卡片网格局部（已完成）
    pg.click('.jobs-tabs button[data-st="done"]'); pg.wait_for_timeout(1800)
    jg=pg.query_selector('#jobsGrid')
    if jg:
        bb=pg.evaluate("""() => { const r=document.querySelector('#jobsGrid').getBoundingClientRect();
            return {x:r.x,y:r.y,width:Math.min(r.width,700),height:Math.min(r.height,500)}; }""")
        pg.screenshot(path=os.path.join(SHOTS,"x-jobsgrid-detail.png"), clip=bb)
    b.close()

print("done")
