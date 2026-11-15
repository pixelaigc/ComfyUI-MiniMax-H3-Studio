
import json, time
from playwright.sync_api import sync_playwright
BASE="http://127.0.0.1:8190"
alerts=[]
with sync_playwright() as p:
    b=p.chromium.launch(args=["--no-sandbox"]); ctx=b.new_context(viewport={"width":1600,"height":950}); pg=ctx.new_page()
    pg.on("dialog", lambda d: (alerts.append(d.message), d.dismiss()))
    posted=[]
    pg.on("request", lambda r: posted.append((r.method, r.url)) if "/api/jobs" in r.url else None)
    pg.goto(BASE+"/", wait_until="domcontentloaded", timeout=60000); time.sleep(6)
    pg.evaluate("() => { const items=[...document.querySelectorAll('.nav-item[data-mode]')]; const hit=items.find(x=>x.dataset.mode==='i2i_h3image'); if(hit) hit.click(); }")
    time.sleep(4)
    pg.evaluate("() => { const m=document.querySelector('#enhanceGateModal'); if(m){m.classList.remove('open','show'); m.hidden=true;} document.querySelector('#imgRefAdd')?.click(); }")
    time.sleep(3)
    pg.evaluate("() => { const body=document.querySelector('.al-picker-body'); const cards=[...body.querySelectorAll('[data-id]')]; cards.slice(0,2).forEach(c=>c.click()); }")
    time.sleep(1)
    pg.evaluate("() => { const m=document.querySelector('#enhanceGateModal'); if(m){m.classList.remove('open','show'); m.hidden=true;} document.querySelector('#alPickerOk')?.click(); }")
    time.sleep(5)
    # 写 prompt
    pg.evaluate("""() => { const p=document.querySelector('#prompt'); p.value='<Picture 1> 换成雪豹'; p.dispatchEvent(new Event('input',{bubbles:true})); }""")
    time.sleep(1)
    posted.clear()
    pg.evaluate("() => { const m=document.querySelector('#enhanceGateModal'); if(m){m.classList.remove('open','show'); m.hidden=true;} document.querySelector('#genBtn')?.click(); }")
    time.sleep(4)
    print("ALERTS", json.dumps(alerts, ensure_ascii=False))
    print("POSTED", json.dumps(posted, ensure_ascii=False))
    # 检查 enhanceGateModal 是否又弹出来
    modal = pg.evaluate("""() => { const m=document.querySelector('#enhanceGateModal'); if(!m) return null; const s=getComputedStyle(m); return {display:s.display, vis:s.visibility, op:s.opacity, cls:m.className, hidden:m.hidden, txt:(m.textContent||'').trim().slice(0,150)}; }""")
    print("MODAL", json.dumps(modal, ensure_ascii=False))
    # 校验状态
    v = pg.evaluate("""() => {
        const refs = window.QwenRefs ? window.QwenRefs.getRefs() : [];
        const problems = window.QwenRefs ? window.QwenRefs.validate(document.querySelector('#prompt')?.value||'') : 'no qwenrefs';
        return {n:refs.length, problems, prompt:(document.querySelector('#prompt')?.value||'').slice(0,80)};
    }""")
    print("VALIDATE", json.dumps(v, ensure_ascii=False))
    b.close()
