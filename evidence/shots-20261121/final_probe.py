import os, json
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])

    # --- 1440: 左侧导航哪些项在视口外 ---
    ctx=b.new_context(viewport={"width":1440,"height":900}, locale="zh-CN")
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3200)
    d=pg.evaluate("""() => {
      const q=s=>Array.from(document.querySelectorAll(s));
      const items=q('#sidebar .nav-scroll button, #sidebar .nav-scroll a');
      const ns=document.querySelector('.nav-scroll');
      const nsr=ns.getBoundingClientRect();
      const out=items.map(el=>{const r=el.getBoundingClientRect();
        return {txt:(el.textContent||'').trim().replace(/\\s+/g,' ').slice(0,24),
                y:Math.round(r.y), bottom:Math.round(r.bottom),
                visibleInNav: r.bottom<=nsr.bottom+1 && r.top>=nsr.top-1};});
      return {navScroll:{scrollH:ns.scrollHeight, clientH:ns.clientHeight, rect:[Math.round(nsr.y),Math.round(nsr.height)]},
              items: out, hiddenCount: out.filter(o=>!o.visibleInNav).length};
    }""")
    print("=== 1440x900 左侧导航可见性 ===")
    print("  nav-scroll scrollH/clientH:", d["navScroll"]["scrollH"], "/", d["navScroll"]["clientH"])
    print("  视口外(需滚动才可见)的项:", d["hiddenCount"])
    for it in d["items"]:
        if not it["visibleInNav"]: print("    -", it["txt"], "y=", it["y"])
    ctx.close()

    # --- 375: 抽屉与 FAB 是否重叠 ---
    ctx2=b.new_context(viewport={"width":375,"height":812}, locale="zh-CN", is_mobile=True, has_touch=True)
    pg2=ctx2.new_page(); pg2.goto(BASE, wait_until="networkidle", timeout=90000); pg2.wait_for_timeout(3200)
    pg2.click('#navToggle'); pg2.wait_for_timeout(1000)
    d2=pg2.evaluate("""() => {
      const sb=document.querySelector('#sidebar').getBoundingClientRect();
      const fab=document.querySelector('#panelFab').getBoundingClientRect();
      const ox=Math.min(sb.right,fab.right)-Math.max(sb.left,fab.left);
      const oy=Math.min(sb.bottom,fab.bottom)-Math.max(sb.top,fab.top);
      const ub=document.querySelector('#unloadBtn').getBoundingClientRect();
      const fo=document.querySelector('#sidebarFoot') || document.querySelector('#unloadBtn').parentElement;
      const fr=fo.getBoundingClientRect();
      const ox2=Math.min(fr.right,fab.right)-Math.max(fr.left,fab.left);
      const oy2=Math.min(fr.bottom,fab.bottom)-Math.max(fr.top,fab.top);
      return {sidebar:[Math.round(sb.left),Math.round(sb.top),Math.round(sb.right),Math.round(sb.bottom)],
              fab:[Math.round(fab.left),Math.round(fab.top),Math.round(fab.right),Math.round(fab.bottom)],
              overlapSidebarFab: (ox>0&&oy>0)?{ox:Math.round(ox),oy:Math.round(oy)}:null,
              footRect:[Math.round(fr.left),Math.round(fr.top),Math.round(fr.right),Math.round(fr.bottom)],
              footVsFab: (ox2>0&&oy2>0)?{ox:Math.round(ox2),oy:Math.round(oy2)}:null,
              unloadRect:[Math.round(ub.left),Math.round(ub.top),Math.round(ub.right),Math.round(ub.bottom)]};
    }""")
    print("\n=== 375 抽屉 vs FAB ===")
    for k,v in d2.items(): print("  ",k,"=",v)
    ctx2.close()
    b.close()
