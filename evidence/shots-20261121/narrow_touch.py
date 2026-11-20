import json, os
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"
OUT = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121"

PROBE = r"""
(() => {
  const q=s=>Array.from(document.querySelectorAll(s));
  const sig=el=>el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):'');
  const vis=el=>{const cs=getComputedStyle(el); if(cs.display==='none'||cs.visibility==='hidden')return false;
    const r=el.getBoundingClientRect(); return r.width>0&&r.height>0;};
  const CLICK='a[href], button, input:not([type=hidden]), select, textarea, [role=button], [onclick]';
  const small=[];
  q(CLICK).forEach(el=>{
    if(!vis(el))return; const r=el.getBoundingClientRect();
    if(r.width<44||r.height<44){
      // 排除被父级完全遮挡的
      small.push({sel:sig(el), w:Math.round(r.width), h:Math.round(r.height),
        area:Math.round(r.width*r.height), txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,26),
        inViewport: r.top<innerHeight && r.bottom>0});
    }
  });
  small.sort((a,b)=>a.area-b.area);
  // 375 下 jobsHead 高度占比
  const jh=document.querySelector('.jobs-head');
  const ja=document.querySelector('#jobsArea');
  const jhr=jh?jh.getBoundingClientRect():null;
  const jar=ja?ja.getBoundingClientRect():null;
  // 卡片第几张开始可见
  const cards=q('#jobsGrid .job-card');
  const firstVisible=cards.findIndex(c=>{const r=c.getBoundingClientRect(); return r.bottom>0&&r.top<innerHeight;});
  return {small: small.slice(0,40), smallTotal: small.length,
    jobsHeadH: jhr?Math.round(jhr.height):null, jobsAreaH: jar?Math.round(jar.height):null,
    jobsHeadPct: (jhr&&jar)?Math.round(jhr.height/jar.height*100):null,
    cardCount: cards.length, firstVisibleCardIdx: firstVisible,
    bodySW: document.body.scrollWidth, docSW: document.documentElement.scrollWidth, innerW: innerWidth};
})()
"""

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx=b.new_context(viewport={"width":375,"height":812}, device_scale_factor=1, locale="zh-CN", is_mobile=True, has_touch=True)
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3500)

    print("=== 375 主界面 ===")
    d=pg.evaluate(PROBE)
    print(f"  small targets total={d['smallTotal']}  jobsHead高={d['jobsHeadH']} / jobsArea高={d['jobsAreaH']} = {d['jobsHeadPct']}%")
    print(f"  卡片数={d['cardCount']} 首个可见卡片 index={d['firstVisibleCardIdx']}  docSW={d['docSW']} innerW={d['innerW']}")
    for s in d["small"][:28]: print("   ", s)

    for nm, sid in [("素材库","navAssets"), ("内置Agent","navAgent")]:
        tog=pg.query_selector('#navToggle')
        if tog and tog.is_visible(): tog.click(); pg.wait_for_timeout(700)
        el=pg.query_selector('#'+sid)
        if el:
            el.click(); pg.wait_for_timeout(2200)
            print(f"\n=== 375 {nm} ===")
            d=pg.evaluate(PROBE)
            print(f"  small targets total={d['smallTotal']} docSW={d['docSW']} innerW={d['innerW']}")
            for s in d["small"][:18]: print("   ", s)
    b.close()
