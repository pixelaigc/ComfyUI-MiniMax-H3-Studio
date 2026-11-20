import json, os
from playwright.sync_api import sync_playwright
BASE = "http://127.0.0.1:8190/"

PROBE = r"""
(() => {
  const q = s => Array.from(document.querySelectorAll(s));
  function parseRGB(s){ const m=s.match(/rgba?\(([^)]+)\)/); if(!m) return null;
    const p=m[1].split(',').map(x=>parseFloat(x.trim())); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; }
  function effBg(el){
    let node=el, stack=[];
    while(node && node.nodeType===1){
      const cs=getComputedStyle(node);
      // 渐变背景也算
      const bi = cs.backgroundImage;
      if (bi && bi !== 'none') { stack.push({grad:true, bi:bi}); break; }
      const c=parseRGB(cs.backgroundColor);
      if(c && c.a>0){ stack.push(c); if(c.a>=0.999) break; }
      node=node.parentElement;
    }
    if (stack.length && stack[stack.length-1].grad) return {grad:stack[stack.length-1].bi};
    let base={r:255,g:255,b:255,a:1};
    for(let i=stack.length-1;i>=0;i--){const c=stack[i]; base={r:c.r*c.a+base.r*(1-c.a),g:c.g*c.a+base.g*(1-c.a),b:c.b*c.a+base.b*(1-c.a),a:1};}
    return base;
  }
  function lum(c){const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);}
  function ratio(a,b){const L1=lum(a),L2=lum(b),x=Math.max(L1,L2),y=Math.min(L1,L2);return (x+0.05)/(y+0.05);}

  const out = {musicChips: [], jobMetaB: [], errText: [], upSlot: [], stitchBtn: null};
  // 音乐 chips
  q('#musicPanel button, #musicPanel .chip').forEach(el=>{
    const cs=getComputedStyle(el);
    const own = Array.from(el.childNodes).some(n=>n.nodeType===3&&n.textContent.trim());
    if(!own) return;
    const fg=parseRGB(cs.color); if(!fg) return;
    const bg=effBg(el);
    if(bg.grad){ out.musicChips.push({sel:'#musicPanel', txt:(el.textContent||'').trim().slice(0,30), color:cs.color, bg:'GRADIENT', grad:bg.grad.slice(0,90)}); return; }
    const fgC={r:fg.r*fg.a+bg.r*(1-fg.a),g:fg.g*fg.a+bg.g*(1-fg.a),b:fg.b*fg.a+bg.b*(1-fg.a)};
    out.musicChips.push({txt:(el.textContent||'').trim().slice(0,30), color:cs.color, ratio:+ratio(fgC,bg).toFixed(2),
      bg:`rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`, fs:cs.fontSize});
  });
  // 卡片 .job-meta b 截断
  q('.job-meta b, .job-meta .row b').forEach(el=>{
    if(el.scrollWidth>el.clientWidth+1)
      out.jobMetaB.push({txt:(el.textContent||'').trim().slice(0,40), sw:el.scrollWidth, cw:el.clientWidth,
        rect:(()=>{const r=el.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)];})(),
        parentSel: el.parentElement.tagName.toLowerCase()+'.'+(el.parentElement.className||''),
        gridCol: getComputedStyle(el.parentElement).gridTemplateColumns});
  });
  // err-text
  q('.err-text').forEach(el=>{
    const cs=getComputedStyle(el); const bg=effBg(el);
    out.errText.push({txt:(el.textContent||'').trim().slice(0,50), color:cs.color, bg: bg.grad?('GRADIENT '+bg.grad.slice(0,80)):('rgb('+Math.round(bg.r)+','+Math.round(bg.g)+','+Math.round(bg.b)+')'),
      parentCls: el.parentElement.className, parentBg: getComputedStyle(el.parentElement).backgroundColor, parentImg: getComputedStyle(el.parentElement).backgroundImage.slice(0,80)});
  });
  // up-slot
  q('.up-slot').forEach(el=>{
    const cs=getComputedStyle(el);
    out.upSlot.push({txt:(el.textContent||'').trim().replace(/\s+/g,' ').slice(0,24), color:cs.color, bg:cs.backgroundColor,
      border:cs.borderTopColor, fs:cs.fontSize,
      rect:(()=>{const r=el.getBoundingClientRect();return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)];})()});
  });
  const sb=document.querySelector('#stitchBtn');
  if(sb){const cs=getComputedStyle(sb);out.stitchBtn={txt:(sb.textContent||'').trim(), color:cs.color, bg:cs.backgroundColor, disabled:sb.disabled, fs:cs.fontSize, fw:cs.fontWeight};}
  return out;
})()
"""

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=["--no-sandbox","--disable-dev-shm-usage"])
    ctx=b.new_context(viewport={"width":1440,"height":900}, locale="zh-CN")
    pg=ctx.new_page(); pg.goto(BASE, wait_until="networkidle", timeout=90000); pg.wait_for_timeout(3000)

    pg.click('#sidebar a.nav-item[data-mode="t2a"]'); pg.wait_for_timeout(1500)
    print("=== 音乐面板 chips (t2a) ===")
    d=pg.evaluate(PROBE)
    for c in d["musicChips"][:14]: print("  ",c)

    pg.click('.jobs-tabs button[data-st="failed"]'); pg.wait_for_timeout(1500)
    print("\n=== err-text ===")
    d=pg.evaluate(PROBE)
    for c in d["errText"]: print("  ",c)

    pg.click('#sidebar button.nav-model-item[data-mode="flf"][data-model-key="h3_flf"]'); pg.wait_for_timeout(1500)
    print("\n=== up-slot (flf) ===")
    d=pg.evaluate(PROBE)
    for c in d["upSlot"][:6]: print("  ",c)

    pg.click('#sidebar button.nav-model-item[data-mode="longfilm"][data-model-key="h3_longfilm"]'); pg.wait_for_timeout(1500)
    print("\n=== stitchBtn ===")
    d=pg.evaluate(PROBE); print("  ", d["stitchBtn"])

    print("\n=== job-meta b 截断（1440）===")
    d=pg.evaluate(PROBE)
    for c in d["jobMetaB"][:6]: print("  ",c)
    b.close()
