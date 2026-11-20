import json, os
from playwright.sync_api import sync_playwright

OUT = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121"
SHOTS = os.path.join(OUT, "shots")
BASE = "http://127.0.0.1:8190/"

PROBE = r"""
(() => {
  const q = s => Array.from(document.querySelectorAll(s));
  const info = (sel) => {
    const el = document.querySelector(sel); if (!el) return null;
    const cs = getComputedStyle(el); const r = el.getBoundingClientRect();
    return {sel, display: cs.display, position: cs.position, zIndex: cs.zIndex,
      rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],
      overflowY: cs.overflowY, overflowX: cs.overflowX, transform: cs.transform};
  };
  // 媒体库/面板的 DOM 顺序
  const order = [];
  document.querySelectorAll('.layout > *, body > *').forEach(el=>{
    const r = el.getBoundingClientRect();
    order.push({tag: el.tagName.toLowerCase(), id: el.id||'', cls:(typeof el.className==='string'?el.className:'').slice(0,50),
                rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)], display:getComputedStyle(el).display});
  });
  // FAB
  const fab = document.querySelector('#panelFab');
  const fabInfo = fab ? (()=>{const r=fab.getBoundingClientRect();const cs=getComputedStyle(fab);
    return {rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],display:cs.display,position:cs.position,bg:cs.backgroundColor,color:cs.color,label:(fab.textContent||'').trim()};})() : null;
  // FAB 是否压住某个元素
  const fabHits = [];
  if (fab && fabInfo && fabInfo.display!=='none') {
    const fr = fab.getBoundingClientRect();
    q('*').forEach(el=>{
      if (el===fab || fab.contains(el) || el.contains(fab)) return;
      const r = el.getBoundingClientRect();
      if (r.width<2||r.height<2) return;
      const ox = Math.min(r.right,fr.right)-Math.max(r.left,fr.left);
      const oy = Math.min(r.bottom,fr.bottom)-Math.max(r.top,fr.top);
      if (ox>0 && oy>0) {
        const inter = ox*oy, small = Math.min(r.width*r.height, fr.width*fr.height);
        if (inter/small > 0.15) fabHits.push({sel: el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):''),
          rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)], pct:Math.round(inter/small*100),
          txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,40)});
      }
    });
    fabHits.sort((a,b)=>b.pct-a.pct);
  }
  return {vw: innerWidth, vh: innerHeight,
    order, fab: fabInfo, fabHits: fabHits.slice(0,12),
    panel: info('#panel'), jobsArea: info('#jobsArea'), jobsGrid: info('#jobsGrid'),
    assets: info('#assetsPage'), agent: info('#agentPage'),
    bodyScrollW: document.body.scrollWidth, docScrollW: document.documentElement.scrollWidth,
    bodyOverflow: getComputedStyle(document.body).overflow,
  };
})()
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width":375,"height":812}, device_scale_factor=1, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3500)

    # full page shot
    pg.screenshot(path=os.path.join(SHOTS, "10c-narrow375-main-FULLPAGE.png"), full_page=True)
    d = pg.evaluate(PROBE)
    print("== 375 主界面 DOM 顺序 ==")
    for o in d["order"]: print("  ", o)
    print("== FAB ==", d["fab"])
    print("== FAB 压住的元素 ==")
    for h in d["fabHits"]: print("  ", h)
    print("== panel ==", d["panel"]); print("== jobsArea ==", d["jobsArea"]); print("== jobsGrid ==", d["jobsGrid"])
    print("bodyScrollW", d["bodyScrollW"], "docScrollW", d["docScrollW"], "bodyOverflow", d["bodyOverflow"])

    # 滚到媒体库
    pg.evaluate("() => { document.querySelector('#jobsArea').scrollIntoView(); }")
    pg.wait_for_timeout(800)
    pg.screenshot(path=os.path.join(SHOTS, "10d-narrow375-media-scrolled.png"))

    # 点 FAB 打开参数面板
    fab = pg.query_selector('#panelFab')
    if fab and fab.is_visible():
        fab.click(); pg.wait_for_timeout(1200)
        pg.screenshot(path=os.path.join(SHOTS, "10e-narrow375-panel-fab-open.png"))
        d2 = pg.evaluate(PROBE)
        print("\n== FAB 打开后 ==")
        print("panel:", d2["panel"]); print("jobsArea:", d2["jobsArea"])
        pg.screenshot(path=os.path.join(SHOTS, "10f-narrow375-panel-fab-FULLPAGE.png"), full_page=True)

    # 素材库 full page
    tog = pg.query_selector('#navToggle')
    if tog and tog.is_visible():
        tog.click(); pg.wait_for_timeout(700)
    na = pg.query_selector('#navAssets')
    if na:
        na.click(); pg.wait_for_timeout(2000)
        pg.screenshot(path=os.path.join(SHOTS, "13b-narrow375-assets-FULLPAGE.png"), full_page=True)
        d3 = pg.evaluate(PROBE)
        print("\n== 375 素材库 =="); print("assets:", d3["assets"]); print("FAB:", d3["fab"])

    # Agent full page
    tog = pg.query_selector('#navToggle')
    if tog and tog.is_visible():
        tog.click(); pg.wait_for_timeout(700)
    ag = pg.query_selector('#navAgent')
    if ag:
        ag.click(); pg.wait_for_timeout(2500)
        pg.screenshot(path=os.path.join(SHOTS, "14b-narrow375-agent-FULLPAGE.png"), full_page=True)
        d4 = pg.evaluate(PROBE)
        print("\n== 375 Agent =="); print("agent:", d4["agent"])

    # 抽屉（导航展开）
    tog = pg.query_selector('#navToggle')
    if tog and tog.is_visible():
        tog.click(); pg.wait_for_timeout(900)
        pg.screenshot(path=os.path.join(SHOTS, "12b-narrow375-drawer-FULLPAGE.png"))

    b.close()
print("done")
