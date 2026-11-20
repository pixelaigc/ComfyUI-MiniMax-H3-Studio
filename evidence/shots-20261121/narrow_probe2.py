import json, os
from playwright.sync_api import sync_playwright
from PIL import Image

OUT = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121"
SHOTS = os.path.join(OUT, "shots")
BASE = "http://127.0.0.1:8190/"

PROBE = r"""
(() => {
  const q = s => Array.from(document.querySelectorAll(s));
  const r = (sel) => { const el=document.querySelector(sel); if(!el) return null;
    const b=el.getBoundingClientRect(); const cs=getComputedStyle(el);
    return {rect:[Math.round(b.x),Math.round(b.y),Math.round(b.width),Math.round(b.height)],
            sw: el.scrollWidth, cw: el.clientWidth, sh: el.scrollHeight, ch: el.clientHeight,
            ox: cs.overflowX, oy: cs.overflowY}; };
  // 375 下 jobsArea 内部是否可横向滚动
  const ja = document.querySelector('#jobsArea');
  const inner = q('#jobsArea *').filter(e=>e.scrollWidth > e.clientWidth+2)
     .map(e=>({sel:e.tagName.toLowerCase()+(e.id?'#'+e.id:'')+(typeof e.className==='string'&&e.className?'.'+e.className.trim().split(/\s+/).slice(0,3).join('.'):''),
               sw:e.scrollWidth, cw:e.clientWidth, ox:getComputedStyle(e).overflowX,
               txt:(e.textContent||'').trim().replace(/\s+/g,' ').slice(0,40)}));
  // 底部 120px 内有没有元素被 FAB 覆盖（FAB 区域 x>=275, y>=752）
  const fab = document.querySelector('#panelFab');
  const fb = fab ? fab.getBoundingClientRect() : null;
  const covered = [];
  if (fb) {
    q('#jobsGrid *, #jobsPagination *, .jobs-head *').forEach(el=>{
      const b = el.getBoundingClientRect();
      if (b.width<3||b.height<3) return;
      if (b.bottom < 700 || b.top > 812) return;
      const ox = Math.min(b.right, fb.right)-Math.max(b.left, fb.left);
      const oy = Math.min(b.bottom, fb.bottom)-Math.max(b.top, fb.top);
      if (ox>0 && oy>0) covered.push({sel: el.tagName.toLowerCase()+(el.id?'#'+el.id:'')+(typeof el.className==='string'&&el.className?'.'+el.className.trim().split(/\s+/).slice(0,3).join('.'):''),
        rect:[Math.round(b.x),Math.round(b.y),Math.round(b.width),Math.round(b.height)], ox:Math.round(ox), oy:Math.round(oy),
        txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,44)});
    });
  }
  return {jobsArea: r('#jobsArea'), jobsGrid: r('#jobsGrid'), jobsHead: r('.jobs-head'),
    jobsTabs: r('.jobs-tabs'), pagination: r('#jobsPagination'), panel: r('#panel'),
    innerHScroll: inner.slice(0,15), fabCovered: covered.slice(0,25),
    fabRect: fb?[Math.round(fb.x),Math.round(fb.y),Math.round(fb.width),Math.round(fb.height)]:null};
})()
"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx = b.new_context(viewport={"width":375,"height":812}, device_scale_factor=1, locale="zh-CN", is_mobile=True, has_touch=True)
    pg = ctx.new_page()
    pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3500)
    d = pg.evaluate(PROBE)
    print("== 375 关键盒 ==")
    for k in ["jobsArea","jobsGrid","jobsHead","jobsTabs","pagination","panel"]:
        print("  ", k, d[k])
    print("== FAB rect ==", d["fabRect"])
    print("== #jobsArea 内横向可滚元素 ==")
    for x in d["innerHScroll"]: print("  ", x)
    print("== FAB 覆盖的元素 ==")
    for x in d["fabCovered"]: print("  ", x)
    b.close()

# FAB 区域放大裁切
im = Image.open(os.path.join(SHOTS, "10-narrow375-main.png"))
im.crop((180, 700, 375, 812)).resize((195*3, 112*3), Image.LANCZOS).save(os.path.join(SHOTS, "10g-narrow375-FAB-overlap-zoom.png"))
print("crop saved")
