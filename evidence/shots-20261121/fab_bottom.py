import os, json
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"
SHOTS = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots"

PROBE = r"""
(() => {
  const q=s=>Array.from(document.querySelectorAll(s));
  const fab=document.querySelector('#panelFab');
  const fb=fab?fab.getBoundingClientRect():null;
  const hits=[];
  if(fb && getComputedStyle(fab).display!=='none'){
    q('#jobsPagination *, .jobs-head *, #jobsGrid *').forEach(el=>{
      const r=el.getBoundingClientRect();
      if(r.width<3||r.height<3) return;
      if(r.bottom<0||r.top>innerHeight) return;
      const ox=Math.min(r.right,fb.right)-Math.max(r.left,fb.left);
      const oy=Math.min(r.bottom,fb.bottom)-Math.max(r.top,fb.top);
      if(ox>0&&oy>0){ const inter=ox*oy, small=Math.min(r.width*r.height,fb.width*fb.height);
        if(inter/small>0.12) hits.push({sel:el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):''),
          rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)], pct:Math.round(inter/small*100),
          txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,30)});}
    });
  }
  const pag=document.querySelector('#jobsPagination');
  const pr=pag?pag.getBoundingClientRect():null;
  const ja=document.querySelector('#jobsArea');
  return {fabRect: fb?[Math.round(fb.x),Math.round(fb.y),Math.round(fb.width),Math.round(fb.height)]:null,
    hits:hits.sort((a,b)=>b.pct-a.pct).slice(0,10),
    paginationRect: pr?[Math.round(pr.x),Math.round(pr.y),Math.round(pr.width),Math.round(pr.height)]:null,
    paginationBottom: pr?Math.round(pr.bottom):null,
    viewportH: innerHeight,
    jobsAreaScrollH: ja?ja.scrollHeight:null, jobsAreaClientH: ja?ja.clientHeight:null,
    scrollY: Math.round(window.scrollY)};
})()
"""

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])

    # 375 滚到底
    ctx=b.new_context(viewport={"width":375,"height":812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3500)
    pg.evaluate("() => { const ja=document.querySelector('#jobsArea'); ja.scrollTop = ja.scrollHeight; }")
    pg.wait_for_timeout(1200)
    pg.screenshot(path=os.path.join(SHOTS,"15-narrow375-media-bottom.png"))
    d=pg.evaluate(PROBE)
    print("=== 375 媒体库滚到底 ===")
    print("  fab:", d["fabRect"], "paginationRect:", d["paginationRect"], "viewportH:", d["viewportH"])
    print("  pagination bottom:", d["paginationBottom"])
    for h in d["hits"]: print("   HIT", h)
    # 分页条底边 vs FAB 顶边
    if d["paginationRect"]:
        print("  FAB 顶边 y=%s / 分页条底边 y=%s" % (d["fabRect"][1], d["paginationRect"][1]+d["paginationRect"][3]))
    ctx.close()

    # 1440: Agent 管理面板（点击「管理」）
    ctx2=b.new_context(viewport={"width":1440,"height":900}, device_scale_factor=1, locale="zh-CN")
    pg2=ctx2.new_page(); pg2.goto(BASE, wait_until="networkidle", timeout=90000); pg2.wait_for_timeout(3200)
    pg2.click('#navAgent'); pg2.wait_for_timeout(2200)
    mb = pg2.query_selector('#agManageBtn')
    if mb:
        mb.click(); pg2.wait_for_timeout(2000)
        pg2.screenshot(path=os.path.join(SHOTS,"16-agent-manage-modal.png"))
        am = pg2.query_selector('#agManageModal')
        if am: am.screenshot(path=os.path.join(SHOTS,"16b-agent-manage-modal-el.png"))
        print("  agent manage modal captured")
    else:
        print("  agManageBtn missing")
    b.close()
