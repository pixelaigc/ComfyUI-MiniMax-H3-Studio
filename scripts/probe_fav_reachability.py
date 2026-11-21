"""短视口下「收藏」入口的**可达性**测量 —— 必须先打开抽屉。

【为什么要单独测】短视口（667x375 / 932x430 / 844x390）下侧栏是抽屉，
且 #navScroll 的可视高度只有 169~280px，而导航内容有 1124px。
「某个滚动位置能命中」这条判据必须模拟真实路径：先点汉堡打开抽屉，
再在 #navScroll 的各个滚动位置用 elementFromPoint 扫。

【上一次踩的坑】第一版探针没开抽屉就测 ⇒ 侧栏 translate  off-screen
⇒ elementFromPoint 返回 null ⇒ 误报「不可达」。
"""
import json

from playwright.sync_api import sync_playwright

REACH_JS = """()=>{
  const el=document.querySelector('#navFavorites');
  if(!el) return {found:false};
  const sc=document.querySelector('#navScroll');
  const max = sc ? Math.max(0, sc.scrollHeight - sc.clientHeight) : 0;
  const steps = [];
  for(let pos=0; pos<=max; pos+=Math.max(8, Math.floor(max/60))){
    if(sc) sc.scrollTop=pos;
    const b=el.getBoundingClientRect();
    const inView = b.bottom>0 && b.top<innerHeight && b.right>0 && b.left<innerWidth;
    if(!inView) { steps.push({pos, inView:false}); continue; }
    let hitY=null, hitInfo=null;
    for(let y=Math.ceil(b.top)+1; y<=Math.floor(b.bottom)-1; y+=2){
      const x=Math.round(Math.min(innerWidth-1, Math.max(0, b.left+b.width/2)));
      const t=document.elementFromPoint(x, y);
      if(t&&(t===el||el.contains(t))){ hitY=y; hitInfo={tag:t.tagName, cls:String(t.className||'').slice(0,40)}; break; }
    }
    steps.push({pos, inView:true, rect:{t:Math.round(b.top),b:Math.round(b.bottom)},
                hitY:hitY, hitInfo:hitInfo});
    if(hitY!==null) return {found:true, reachable:true, at:steps[steps.length-1],
                            navScrollH: sc?sc.clientHeight:0,
                            scrollH: sc?sc.scrollHeight:0, innerH: innerHeight};
  }
  return {found:true, reachable:false, last:steps[steps.length-1]||null,
          navScrollH: sc?sc.clientHeight:0, scrollH: sc?sc.scrollHeight:0,
          innerH: innerHeight, sidebarPos: getComputedStyle(document.querySelector('#sidebar')).position};
}"""

VPS = [(667, 375), (932, 430), (844, 390), (430, 932), (375, 667),
       (375, 812), (390, 844), (768, 1024), (1024, 768), (320, 568)]

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    for w, h in VPS:
        ctx = b.new_context(viewport={"width": w, "height": h},
                            is_mobile=True, has_touch=True, device_scale_factor=2)
        p = ctx.new_page()
        p.goto("http://127.0.0.1:8190/", wait_until="domcontentloaded", timeout=60000)
        p.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
        p.wait_for_timeout(1500)
        is_phone = p.evaluate(
            "()=>window.BREAKPOINTS?innerWidth<=window.BREAKPOINTS.phone"
            ":matchMedia('(max-width:700px)').matches")
        if is_phone:
            p.tap("#navToggle", timeout=8000)
            p.wait_for_timeout(900)
        r = p.evaluate(REACH_JS)
        at = r.get("at") or {}
        print("  %4dx%-4d phone=%-5s reachable=%-5s navScrollH=%-4s scrollH=%-5s hitY=%s" % (
            w, h, is_phone, r.get("reachable"), r.get("navScrollH"), r.get("scrollH"),
            at.get("hitY")))
        if not r.get("reachable"):
            print("        最后一个扫描点:", json.dumps(r.get("last"), ensure_ascii=False))
        ctx.close()
    b.close()
