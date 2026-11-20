import os, json
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"
SHOTS = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121/shots"

# 找出 FAB 覆盖的最严重场景：让卡片的操作按钮行滚到 FAB 位置
PROBE = r"""
(() => {
  const q=s=>Array.from(document.querySelectorAll(s));
  const fab=document.querySelector('#panelFab');
  const fb=fab.getBoundingClientRect();
  const ja=document.querySelector('#jobsArea');
  const worst=[];
  // 扫描滚动位置，找 FAB 覆盖最多的元素
  const maxScroll = ja.scrollHeight - ja.clientHeight;
  for (let st=0; st<=maxScroll; st+=Math.max(60, Math.floor(maxScroll/30))) {
    ja.scrollTop = st;
    ja.getBoundingClientRect();
    const hits=[];
    q('#jobsGrid .job-card *').forEach(el=>{
      const r=el.getBoundingClientRect();
      if(r.width<3||r.height<3) return;
      const ox=Math.min(r.right,fb.right)-Math.max(r.left,fb.left);
      const oy=Math.min(r.bottom,fb.bottom)-Math.max(r.top,fb.top);
      if(ox>0&&oy>0){ const inter=ox*oy, small=Math.min(r.width*r.height,fb.width*fb.height);
        hits.push({sel:el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):''),
          rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)], ox:Math.round(ox), oy:Math.round(oy),
          pct:Math.round(inter/small*100), txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,26)});}
    });
    hits.sort((a,b)=>b.pct-a.pct);
    if(hits.length) worst.push({scrollTop:st, top:hits.slice(0,6)});
  }
  worst.sort((a,b)=>b.top[0].pct-a.top[0].pct);
  return {fab:[Math.round(fb.x),Math.round(fb.y),Math.round(fb.width),Math.round(fb.height)], worst: worst.slice(0,6)};
})()
"""

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx=b.new_context(viewport={"width":375,"height":812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3500)
    d=pg.evaluate(PROBE)
    print("FAB rect:", d["fab"])
    print("=== 最严重覆盖场景 ===")
    for w in d["worst"]:
        print(f"  scrollTop={w['scrollTop']}")
        for h in w["top"][:4]:
            print(f"     {h['pct']}% overlap ox={h['ox']} oy={h['oy']} {h['sel']} rect={h['rect']} :: {h['txt']}")
    # 重新定位到最严重场景并截图
    if d["worst"]:
        st=d["worst"][0]["scrollTop"]
        pg.evaluate(f"() => {{ document.querySelector('#jobsArea').scrollTop = {st}; }}")
        pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(SHOTS,"15c-narrow375-fab-worst-overlap.png"))
        print("worst shot saved at scrollTop", st)
    b.close()
