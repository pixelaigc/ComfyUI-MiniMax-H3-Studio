"""宽屏（桌面）下的「收藏」功能取证 —— 8190 线上。

【为什么补这一支】用户要求「8190 真实浏览器截图（宽屏 + 375px）」。
375px 那支已有（evidence/mobile-nav/build-id-live/ + fav-leg/ 矩阵），
这一支补宽屏：左侧「收藏」入口、收藏视图、卡片详情、右键菜单。

只读浏览，不新增任何生成作业。
"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/root/projects/h3s-favorites/evidence/favorites-desktop")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190"

JS_STATE = """()=>{
  const tab=(document.querySelector('.jobs-tabs button[data-st].active')||{}).dataset?.st;
  const nav=document.querySelector('#navFavorites');
  const cards=document.querySelectorAll('#jobsGrid .job-card[data-jid]');
  const badges=document.querySelectorAll('#jobsGrid [class*="fav"], #jobsGrid .fav-badge');
  const sels=document.querySelectorAll('#jobsGrid .fav-sel');
  return {activeTab:tab,
          navActive: nav?nav.classList.contains('active'):false,
          navText: nav?nav.textContent.replace(/\\s+/g,' ').trim():null,
          cards: cards.length,
          favBadgeNodes: badges.length,
          favCheckboxes: sels.length,
          filtersVisible: (()=>{const f=document.querySelector('#doneFilters');return !!(f&&!f.hidden);})(),
          batchBar: !!document.querySelector('#favBatchBar, .fav-batch-bar, #jobsArea [class*="batch"]')};}"""

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    # 宽屏：1440x900
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    p = ctx.new_page()
    p.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
    p.wait_for_timeout(2000)

    facts = {}
    facts["initial"] = p.evaluate(JS_STATE)
    p.screenshot(path=str(OUT / "desktop-1440-1-media-library.png"))

    # 左侧「收藏」入口
    nav = p.query_selector("#navFavorites")
    facts["navFavorites_present"] = nav is not None
    if nav:
        nav.click()
        p.wait_for_timeout(2500)
        p.wait_for_function(
            "()=>!!document.querySelector('#jobsGrid .job-card[data-jid]')", timeout=30000)
        facts["favorites_view"] = p.evaluate(JS_STATE)
        p.screenshot(path=str(OUT / "desktop-1440-2-favorites-view.png"))

        # 卡片详情模态框
        card = p.query_selector("#jobsGrid .job-card[data-jid]")
        if card:
            card.click()
            p.wait_for_timeout(1200)
            facts["detail_open"] = p.evaluate(
                """()=>{const m=document.querySelector('#detailModal');
                   if(!m||m.hasAttribute('hidden'))return false;
                   const b=m.getBoundingClientRect();return b.width>0&&b.height>0;}""")
            facts["detail_has_params"] = p.evaluate(
                """()=>{const b=document.querySelector('#detailModalBody');
                   const s=b?String(b.textContent||''):'';
                   return {steps: s.includes('步数')||s.toLowerCase().includes('steps'),
                           scheduler: s.includes('调度器')||s.toLowerCase().includes('scheduler'),
                           seed: s.includes('种子')||s.toLowerCase().includes('seed'),
                           sample: s.replace(/\\s+/g,' ').slice(0,160)};}""")
            p.screenshot(path=str(OUT / "desktop-1440-3-card-detail.png"))
            p.evaluate("""()=>{const c=document.querySelector('#detailModalClose');
                            if(c) c.click();}""")
            p.wait_for_timeout(700)

        # 右键菜单
        # 【关键】菜单绑定在 .job-preview 里的 img/video/audio 上，
        #   **不是**卡片本体 —— 点卡片中心可能落在文本行上，菜单不开。
        #   （实测：右键卡片中心 => #mediaContext display:none、items=[]。）
        card = p.query_selector("#jobsGrid .job-card[data-jid]")
        if card:
            target = card.query_selector(".job-preview img, .job-preview video, .job-preview audio")
            facts["ctx_target_kind"] = target.evaluate("e => e.tagName") if target else None
            if target:
                target.scroll_into_view_if_needed()
                p.wait_for_timeout(400)
                bb = target.bounding_box()
                p.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2,
                              button="right")
                p.wait_for_timeout(900)
                facts["context_menu"] = p.evaluate(
                    """()=>{const m=document.querySelector('#mediaContext');
                       if(!m) return {found:false};
                       const cs=getComputedStyle(m);
                       const items=Array.from(m.querySelectorAll('button[data-act]'))
                         .map(e=>({act:e.dataset.act,
                                   text:String(e.textContent||'').replace(/\\s+/g,' ').trim()}));
                       return {found:true, display:cs.display, visible:m.hidden===false,
                               items:items};}""")
                p.screenshot(path=str(OUT / "desktop-1440-4-context-menu.png"))
                p.keyboard.press("Escape")
                p.wait_for_timeout(500)

    # 「全部 / 进行中 / 已完成 / 收藏」筛选在宽屏可用
    facts["status_tabs"] = p.evaluate(
        """()=>Array.from(document.querySelectorAll('.jobs-tabs button[data-st]'))
             .map(b=>({st:b.dataset.st, text:b.textContent.trim(),
                       visible: b.getBoundingClientRect().width>0}));""")

    # 素材库入口（对照：左侧导航约定一致）
    facts["nav_items"] = p.evaluate(
        """()=>Array.from(document.querySelectorAll('#sidebar .nav-item, #sidebar .nav-group'))
             .slice(0,20).map(e=>e.textContent.replace(/\\s+/g,' ').trim()).filter(Boolean);""")

    json.dump(facts, open(OUT / "desktop-facts.json", "w"), ensure_ascii=False, indent=1)
    ctx.close()
    b.close()

for k, v in facts.items():
    print("  %-22s %s" % (k, json.dumps(v, ensure_ascii=False)[:220]))
print("  证据目录:", OUT)
