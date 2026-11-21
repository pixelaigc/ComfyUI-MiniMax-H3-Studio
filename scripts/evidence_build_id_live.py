"""线上 8190 的顶栏「构建版本标识」取证 —— 375px 窄屏。

【为什么单独取证】上一轮遗留一个未确认项：顶栏构建版本标识 + /api/version
+ 系统页完整版本信息，到底是否已在线。这一支不碰 8190 的数据，
只做只读截图 + 只读接口调用。
"""
import json
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/root/projects/h3s-favorites/evidence/mobile-nav/build-id-live")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190"

api = json.loads(urllib.request.urlopen(BASE + "/api/version", timeout=20).read())
print("  /api/version ->", json.dumps({k: api[k] for k in
      ("commit", "assets_id", "served_from") if k in api}, ensure_ascii=False))

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 375, "height": 812},
                        is_mobile=True, has_touch=True, device_scale_factor=2)
    p = ctx.new_page()
    p.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_selector("#buildIdValue", timeout=30000)
    # 【判据修正】#buildIdValue 是 #buildId 的**子 span**，初始内容是「…」。
    #   填不填得进去取决于 /api/version 是否回来，所以等它的 textContent
    #   变成真实 commit（不是「…」也不是「—」）。
    p.wait_for_function(
        """()=>{const e=document.querySelector('#buildIdValue');
             const t=String(e?.textContent||'').trim();
             return t && t!=='…' && t!=='—';}""", timeout=30000)
    p.wait_for_timeout(800)

    fact = p.evaluate("""()=>{
      const e=document.querySelector('#buildIdValue');
      const b=e.getBoundingClientRect();
      const ov=(a,c)=>{const x=Math.max(0,Math.min(a.right,c.right)-Math.max(a.left,c.left));
        const y=Math.max(0,Math.min(a.bottom,c.bottom)-Math.max(a.top,c.top));
        return Math.round(x*y);};
      const brand=document.querySelector('.brand-version').getBoundingClientRect();
      const status=document.querySelector('#topStatus').getBoundingClientRect();
      const cx=Math.round(b.left+b.width/2), cy=Math.round(b.top+b.height/2);
      const hit=document.elementFromPoint(cx,cy);
      return {text:String(e.textContent||'').trim(),
              title:e.title||'',
              inViewport: b.left>=0 && b.right<=innerWidth && b.width>0,
              overlapWithBrand: ov(b,brand),
              overlapWithStatus: ov(b,status),
              hitIsSelf: !!(hit && (hit===e||e.contains(hit))),
              fontFamily: getComputedStyle(e).fontFamily}; }""")
    print("  顶栏版本标识:", json.dumps(fact, ensure_ascii=False))

    p.screenshot(path=str(OUT / "live-375-topbar-buildid.png"), clip={
        "x": 0, "y": 0, "width": 375, "height": 52})

    # 系统页完整版本信息
    p.tap("#navToggle", timeout=8000)
    p.wait_for_timeout(900)
    p.evaluate("""()=>{const all=Array.from(document.querySelectorAll('#sidebar .nav-item'));
      const sys=all.find(e=>e.textContent.includes('系统')||e.textContent.includes('硬件'));
      if(sys) sys.click();}""")
    p.wait_for_timeout(1500)
    detail = p.evaluate("""()=>{
      const el=document.querySelector('#buildIdDetail');
      if(!el) return {found:false};
      const b=el.getBoundingClientRect();
      return {found:true, visible:b.width>0&&b.height>0,
              text:String(el.textContent||'').replace(/\\s+/g,' ').trim()};}""")
    print("  系统页版本卡片:", json.dumps(detail, ensure_ascii=False))
    p.screenshot(path=str(OUT / "live-375-system-buildid.png"), full_page=False)

    json.dump({"api_version": api, "topbar": fact, "system_page": detail},
              open(OUT / "build-id-live.json", "w"), ensure_ascii=False, indent=1)
    ctx.close()
    b.close()
print("  证据目录:", OUT)
