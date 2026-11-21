"""375px 窄屏下的「收藏」功能取证 —— 8190 线上（用户明确要求的 375px 这一支）。

【覆盖用户的移动端硬约束】
  ① 进入任一生成类型面板后，仍可重新打开生成类型选择
  ② 任何状态下都能访问生成媒体库（含收藏栏）与卡片详情
  ③ 「全部/进行中/已完成/按类型/收藏」筛选在移动端可用
  ④ 导航控件不遮挡卡片正文或抽屉

全部用 elementFromPoint 真实命中 + 可达性判据，不用被祖先裁剪的矩形。
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/root/projects/h3s-favorites/evidence/favorites-mobile-375")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190"

HIT = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {found:false};
  const b = el.getBoundingClientRect();
  if (!b.width || !b.height) return {found:true, zeroSize:true};
  let own=0, blocked=0, other=0;
  const blockers=[];
  for (let i=1;i<=3;i++) for (let j=1;j<=3;j++){
    const x=Math.round(b.left+b.width*i/4), y=Math.round(b.top+b.height*j/4);
    if (x<0||y<0||x>=innerWidth||y>=innerHeight) continue;
    const t=document.elementFromPoint(x,y);
    if (!t) { other++; continue; }
    if (t===el||el.contains(t)) { own++; continue; }
    const c=t.closest && t.closest('#navToggle,#panelFab,#sidebar,#sidebarMask,#panelMask,#panel,#topbar');
    if (c) { blocked++; blockers.push(c.id||String(c.className||'').slice(0,30)); }
    else other++;
  }
  return {found:true, own, blocked, other, blockers,
          fullyReachable: own>0 && blocked===0};
}"""

facts = {}

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 375, "height": 667},
                        is_mobile=True, has_touch=True, device_scale_factor=2)
    p = ctx.new_page()
    p.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
    p.wait_for_timeout(2000)

    # ---- ① 选一个生成类型后，仍能重新打开生成类型选择 ----
    facts["phone_layout"] = p.evaluate(
        "()=>window.BREAKPOINTS?innerWidth<=window.BREAKPOINTS.phone"
        ":matchMedia('(max-width:700px)').matches")
    p.tap("#navToggle", timeout=8000)
    p.wait_for_timeout(900)
    facts["picker_open"] = p.evaluate(
        "()=>document.querySelector('#sidebar').classList.contains('open')")
    p.screenshot(path=str(OUT / "375-1-type-picker.png"))

    # 选 t2i（一个图片类型）
    p.evaluate("""()=>{const el=document.querySelector('.nav-item[data-mode="t2i"],'
                   + '.nav-model-item[data-mode="t2i"]');
                   const sc=document.querySelector('#navScroll');
                   if(el&&sc) el.scrollIntoView({block:'nearest'});}""")
    p.wait_for_timeout(500)
    p.tap('.nav-item[data-mode="t2i"], .nav-model-item[data-mode="t2i"]', timeout=8000)
    p.wait_for_timeout(1500)
    facts["active_mode_after_select"] = p.evaluate(
        """()=>{const a=document.querySelector('.nav-item.active,.nav-model-item.active');
           return a?a.dataset.mode:null;}""")
    facts["panel_open_after_select"] = p.evaluate(
        "()=>!!document.querySelector('#panel.open')")
    p.screenshot(path=str(OUT / "375-2-after-select-type.png"))

    # ---- ② 回到类型选择（用户报的死路）----
    facts["hamburger_hit"] = p.evaluate(HIT, "#navToggle")
    p.tap("#navToggle", timeout=8000)
    p.wait_for_timeout(900)
    facts["reached_picker_again"] = p.evaluate(
        "()=>document.querySelector('#sidebar').classList.contains('open')")
    facts["picker_controls"] = p.evaluate(
        """(s)=>{const out=[];for(const sel of s){
             const el=document.querySelector(sel);
             if(!el){out.push({sel,found:false});continue;}
             const b=el.getBoundingClientRect();
             const x=Math.round(b.left+b.width/2), y=Math.round(b.top+b.height/2);
             const t=document.elementFromPoint(x,y);
             out.push({sel,found:true,hitSelf:!!(t&&(t===el||el.contains(t))),
                       hitId:t?t.id:null});}return out;}""",
        ["#navToggle", "#navDrawerClose", "#navDrawerHead"])
    p.screenshot(path=str(OUT / "375-3-back-to-picker.png"))

    # ---- 进入收藏栏（扫滚动位置找可命中坐标）----
    plan = p.evaluate(
        """()=>{const el=document.querySelector('#navFavorites');
           const sc=document.querySelector('#navScroll');
           const max=sc?Math.max(0,sc.scrollHeight-sc.clientHeight):0;
           for(let pos=0;pos<=max;pos+=Math.max(8,Math.floor(max/60))){
             if(sc) sc.scrollTop=pos;
             const b=el.getBoundingClientRect();
             if(b.bottom<=0||b.top>=innerHeight) continue;
             for(let y=Math.ceil(b.top)+1;y<=Math.floor(b.bottom)-1;y+=2){
               const x=Math.round(b.left+b.width/2);
               const t=document.elementFromPoint(x,y);
               if(t&&(t===el||el.contains(t))) return {ok:true,x:x,y:y,scrollTop:sc?sc.scrollTop:0};
             }
           }
           return {ok:false};}""")
    facts["fav_entry_hit_point"] = plan
    if plan.get("ok"):
        p.touchscreen.tap(plan["x"], plan["y"])
        p.wait_for_timeout(1000)
        try:
            p.wait_for_function(
                "()=>!!document.querySelector('#jobsGrid .job-card[data-jid]')", timeout=25000)
        except Exception as e:
            facts["fav_wait_err"] = str(e).split("\n")[0][:100]
        facts["favorites_view"] = p.evaluate(
            """()=>{const tab=(document.querySelector('.jobs-tabs button[data-st].active')||{}).dataset?.st;
               const nav=document.querySelector('#navFavorites');
               return {activeTab:tab,
                       navActive:nav?nav.classList.contains('active'):false,
                       cards:document.querySelectorAll('#jobsGrid .job-card[data-jid]').length,
                       checkboxes:document.querySelectorAll('#jobsGrid .fav-sel').length,
                       filtersVisible:(()=>{const f=document.querySelector('#doneFilters');
                                            return !!(f&&!f.hidden);})()};}""")
        p.screenshot(path=str(OUT / "375-4-favorites-view.png"))

        # ---- ③ 卡片详情 ----
        p.evaluate("""()=>{const a=document.querySelector('#jobsArea');
               const c=document.querySelector('#jobsGrid .job-card[data-jid]');
               if(a&&c) a.scrollTop += c.getBoundingClientRect().top
                                   - a.getBoundingClientRect().top;}""")
        p.wait_for_timeout(600)
        facts["card_hit"] = p.evaluate(HIT, "#jobsGrid .job-card[data-jid]")
        p.tap("#jobsGrid .job-card[data-jid]", timeout=8000)
        p.wait_for_timeout(1200)
        facts["detail_open"] = p.evaluate(
            """()=>{const m=document.querySelector('#detailModal');
               if(!m||m.hasAttribute('hidden'))return false;
               const bb=m.getBoundingClientRect();return bb.width>0&&bb.height>0;}""")
        facts["detail_params"] = p.evaluate(
            """()=>{const b=document.querySelector('#detailModalBody');
               const s=b?String(b.textContent||''):'';
               return {steps:s.includes('步数')||s.toLowerCase().includes('steps'),
                       scheduler:s.includes('调度器')||s.toLowerCase().includes('scheduler'),
                       seed:s.includes('种子')||s.toLowerCase().includes('seed')};}""")
        p.screenshot(path=str(OUT / "375-5-card-detail.png"))
        p.evaluate("""()=>{const c=document.querySelector('#detailModalClose');
                        if(c) c.click();}""")
        p.wait_for_timeout(700)

        # ---- ④ 筛选标签可点 ----
        # 【为什么要先 scrollIntoView】标签行在收藏视图里可能被滚出可视区
        # （375x667 下媒体库头部很高）。判据必须是「某个滚动位置能命中」，
        # 不是「当前滚动位置能命中」—— 同 §3.1 的判据修正。
        p.evaluate("""()=>{const t=document.querySelector('.jobs-tabs');
               if(t) t.scrollIntoView({block:'center'});}""")
        p.wait_for_timeout(700)
        facts["status_tabs"] = p.evaluate(
            """()=>Array.from(document.querySelectorAll('.jobs-tabs button[data-st]')).map(b=>{
                 const bb=b.getBoundingClientRect();
                 const x=Math.round(bb.left+bb.width/2), y=Math.round(bb.top+bb.height/2);
                 const t=document.elementFromPoint(x,y);
                 return {st:b.dataset.st, text:b.textContent.trim(),
                         visible:bb.width>0, inView: bb.top>=0&&bb.bottom<=innerHeight,
                         hitSelf:!!(t&&(t===b||b.contains(t)))};})""")
        facts["type_tabs"] = p.evaluate(
            """()=>Array.from(document.querySelectorAll('.jobs-tabs button[data-type]')).map(b=>{
                 const bb=b.getBoundingClientRect();
                 const x=Math.round(bb.left+bb.width/2), y=Math.round(bb.top+bb.height/2);
                 const t=document.elementFromPoint(x,y);
                 return {type:b.dataset.type, text:b.textContent.trim(),
                         visible:bb.width>0, inView: bb.top>=0&&bb.bottom<=innerHeight,
                         hitSelf:!!(t&&(t===b||b.contains(t)))};})""")
        p.screenshot(path=str(OUT / "375-6-filters.png"))

        # ---- ⑤ 导航控件不遮挡卡片正文 ----
        facts["overlap"] = p.evaluate(
            """()=>{const out=[];
                 for(const sel of ['#jobsGrid .job-card[data-jid]','#navToggle','#panelFab']){
                   const el=document.querySelector(sel);
                   if(!el){out.push({sel,found:false});continue;}
                   const b=el.getBoundingClientRect();
                   let own=0,blocked=0;
                   for(let i=1;i<=3;i++) for(let j=1;j<=3;j++){
                     const x=Math.round(b.left+b.width*i/4), y=Math.round(b.top+b.height*j/4);
                     if(x<0||y<0||x>=innerWidth||y>=innerHeight) continue;
                     const t=document.elementFromPoint(x,y);
                     if(!t) continue;
                     if(t===el||el.contains(t)) own++;
                     else {const c=t.closest&&t.closest('#navToggle,#panelFab,#sidebar,#panelMask,#panel');
                           if(c) blocked++;}
                   }
                   out.push({sel,found:true,own,blocked,fullyReachable:own>0&&blocked===0});
                 } return out;}""")

    json.dump(facts, open(OUT / "mobile-375-facts.json", "w"), ensure_ascii=False, indent=1)
    ctx.close()
    b.close()

for k, v in facts.items():
    print("  %-24s %s" % (k, json.dumps(v, ensure_ascii=False)[:230]))
print("  证据目录:", OUT)
