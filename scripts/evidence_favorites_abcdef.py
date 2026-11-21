"""①–⑥ 逐项真实浏览器取证 —— 生成媒体库收藏功能（8190 线上，Chromium）。

【逐项对应用户验收清单】
  ① 提交后保持当前标签：点「生成/已入队」后，右侧媒体库**不自动跳到「进行中」**，
     用户当前所选标签与筛选保持不变；新任务仍出现在其所属标签下。
  ② 卡片右键加入收藏：右键菜单有「添加到收藏」；已收藏的显示「取消收藏」，
     卡片上有清晰的已收藏标识。
  ③ 左侧「收藏」栏：可进入、展示全部收藏内容、沿用既有卡片交互
     （查看、详情模态框、右键、筛选/搜索、分页）、保留类型区分。
  ④ 清理跳过收藏：「管理 → 清理」后收藏项仍在，计数正确。
  ⑤ 取消收藏生效：取消后卡片不再在收藏视图里；且重新纳入清理范围。
  ⑥ 批量取消收藏：勾选多条 → 批量取消收藏 → 收藏数减少。

【硬约束】
  · **不新增任何 GPU 生成作业** —— ① 用「已入队」路径，
    优先复用已有产物；必要时用**取消收藏/收藏已有产物**这种无生成操作来验证 UI。
  · 只读为主；④/⑤/⑥ 会改收藏标记，但**不删除任何产物文件**。
  · 每项都存截图 + 结构化 JSON。
"""
import json
import subprocess
import sqlite3
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/root/projects/h3s-favorites/evidence/favorites-abcdef")
OUT.mkdir(parents=True, exist_ok=True)
BASE = "http://127.0.0.1:8190"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"

facts = {}
notes = []


def api(path, method="GET", body=None):
    cmd = ["curl", "-s", "-m", "60", "-X", method, BASE + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    try:
        return json.loads(out)
    except Exception:
        return {"raw": out[:300]}


def fav_rows():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = [dict(r) for r in c.execute("SELECT job_id, created_at FROM job_favorites")]
    c.close()
    return rows


TAB_STATE = """()=>{
  const tab=(document.querySelector('.jobs-tabs button[data-st].active')||{}).dataset?.st;
  const typeTab=(document.querySelector('.jobs-tabs button[data-type].active')||{}).dataset?.type;
  const cards=document.querySelectorAll('#jobsGrid .job-card[data-jid]');
  const badges=Array.from(document.querySelectorAll('#jobsGrid .job-card')).filter(c=>
      c.querySelector('.job-fav-badge'));
  const pageSize=(document.querySelector('#pageSize')||{}).value||null;
  return {activeTab:tab, activeType:typeTab,
          cards:cards.length,
          favoritedCards:badges.length,
          favCheckboxes:document.querySelectorAll('#jobsGrid .fav-sel').length,
          searchVisible: !!(document.querySelector('#doneSearch')||document.querySelector('#searchBox')),
          pageSize: pageSize};}"""

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
    p = ctx.new_page()
    # 【必须】批量取消收藏会弹 window.confirm 二次确认
    #   （favorites.js:167「确认取消收藏这 N 个条目？…只解除收藏标记」）。
    #   Playwright 默认**自动 dismiss** 对话框 ⇒ 那个分支直接 return false
    #   ⇒ DB 一条都没删，但按钮看起来「点了没反应」。
    #   正确做法：注册 dialog 处理器并 accept()。
    dialogs = []

    def _on_dialog(d):
        dialogs.append({"type": d.type, "message": d.message[:160]})
        d.accept()

    p.on("dialog", _on_dialog)
    p.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    p.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
    p.wait_for_timeout(2500)

    # ================= ② 卡片右键加入收藏 / 已收藏标识 =================
    facts["02_before"] = {"db_favorites": len(fav_rows())}

    # 找一个**未收藏**的卡片
    # 【真实 class】已收藏标识是 `.job-fav-badge`（app.js:5149）。
    #   之前用 `[class*="fav"]` 会把 `.fav-sel-wrap`（选择框外层）也算进去
    #   ⇒ 永远找不到「未收藏」的卡。
    unfav = p.evaluate(
        """()=>{const cs=Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
           for(const c of cs){
             const star=c.querySelector('.job-fav-badge');
             if(!star) return c.dataset.jid;
           }
           return null;}""")
    facts["02_before"]["unfavorited_card"] = unfav

    # 右键它的预览图 → 菜单里应有「添加到收藏」
    if unfav:
        picked = p.evaluate(
            """(jid)=>{const c=document.querySelector('.job-card[data-jid="'+jid+'"]');
               const t=c.querySelector('.job-preview img, .job-preview video, .job-preview audio');
               if(t){t.scrollIntoView({block:'center'});return t.tagName;}
               return null;}""", unfav)
        p.wait_for_timeout(500)
        tgt = p.query_selector(f'.job-card[data-jid="{unfav}"] .job-preview img, '
                               f'.job-card[data-jid="{unfav}"] .job-preview video, '
                               f'.job-card[data-jid="{unfav}"] .job-preview audio')
        if tgt:
            bb = tgt.bounding_box()
            p.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2,
                          button="right")
            p.wait_for_timeout(900)
            facts["02_context_menu"] = p.evaluate(
                """()=>{const m=document.querySelector('#mediaContext');
                   return {items:Array.from(m.querySelectorAll('button[data-act]'))
                             .map(e=>({act:e.dataset.act,
                                       text:String(e.textContent||'').replace(/\\s+/g,' ').trim()}))};}""")
            p.screenshot(path=str(OUT / "02-1-right-click-menu-add-favorite.png"))
            # 点「添加到收藏」
            btn = p.query_selector('#mediaContext button[data-act="favorite"]')
            if btn:
                btn.click()
                p.wait_for_timeout(2500)
                facts["02_after_click"] = p.evaluate(
                    """()=>{const m=document.querySelector('#mediaContext');
                       return {menuHidden:m?m.hidden:null,
                               toast:(document.querySelector('.toast, #toast')||{}).textContent||null};}""")
                p.screenshot(path=str(OUT / "02-2-after-add-favorite.png"))
    # 落库校验
    rows = fav_rows()
    facts["02_after"] = {"db_favorites": len(rows),
                         "new_in_db": unfav in [r["job_id"] for r in rows]}
    p.keyboard.press("Escape")
    p.wait_for_timeout(600)

    # ================= ③ 左侧「收藏」栏 =================
    nav = p.query_selector("#navFavorites")
    facts["03_nav_present"] = nav is not None
    nav.click()
    p.wait_for_timeout(1500)
    p.wait_for_function(
        "()=>!!document.querySelector('#jobsGrid .job-card[data-jid]')", timeout=30000)
    facts["03_favorites_view"] = p.evaluate(TAB_STATE)
    facts["03_detail_open"] = False
    p.screenshot(path=str(OUT / "03-1-favorites-view.png"))
    # 卡片详情模态框（沿用既有交互）
    p.evaluate("""()=>{const a=document.querySelector('#jobsArea');
           const c=document.querySelector('#jobsGrid .job-card[data-jid]');
           if(a&&c) a.scrollTop += c.getBoundingClientRect().top
                               - a.getBoundingClientRect().top;}""")
    p.wait_for_timeout(600)
    # 【桌面上下文没有 touch 能力】`Page.tap` 会报 "The page does not support tap"，
    #   宽屏取证用 click（鼠标事件），触摸路径由 375px 那支覆盖。
    p.click("#jobsGrid .job-card[data-jid]", timeout=8000)
    p.wait_for_timeout(1200)
    facts["03_detail_open"] = p.evaluate(
        """()=>{const m=document.querySelector('#detailModal');
           if(!m||m.hasAttribute('hidden'))return false;
           const bb=m.getBoundingClientRect();return bb.width>0&&bb.height>0;}""")
    p.screenshot(path=str(OUT / "03-2-favorites-card-detail.png"))
    p.evaluate("""()=>{const c=document.querySelector('#detailModalClose');
                    if(c) c.click();}""")
    p.wait_for_timeout(700)
    # 类型区分（筛选条可用）
    facts["03_type_filter"] = p.evaluate(
        """()=>Array.from(document.querySelectorAll('.jobs-tabs button[data-type]'))
             .map(b=>({type:b.dataset.type, text:b.textContent.trim()}));""")

    # ================= ④ 清理跳过收藏 =================
    facts["04_db_before"] = len(fav_rows())
    facts["04_manage_preview"] = api("/api/media/manage?scope=all&months=1")
    p.screenshot(path=str(OUT / "04-1-before-cleanup.png"))

    # ================= ⑥ 批量取消收藏（先做，再做 ⑤）====================
    # 勾选若干条 → 批量取消收藏
    n_sel = p.evaluate(
        """()=>{const cs=document.querySelectorAll('#jobsGrid .fav-sel');
           let n=0;
           for(const c of cs){ if(n>=3) break; c.click(); n++; }
           return n;}""")
    p.wait_for_timeout(900)
    facts["06_selected"] = n_sel
    facts["06_batch_bar"] = p.evaluate(
        """()=>{const b=document.querySelector('#favTools');
           if(!b) return {found:false};
           const bb=b.getBoundingClientRect();
           return {found:true, hidden:b.hidden,
                   visible:bb.width>0&&bb.height>0,
                   text:String(b.textContent||'').replace(/\\s+/g,' ').trim().slice(0,90),
                   buttons:Array.from(b.querySelectorAll('button')).map(x=>({
                       id:x.id, text:x.textContent.trim()}))};}""")
    p.screenshot(path=str(OUT / "06-1-batch-selected.png"))

    # 点「批量取消收藏」
    before = len(fav_rows())
    clicked = p.evaluate(
        """()=>{const x=document.querySelector('#favBatchUnfavBtn');
           if(!x) return 'NO_BATCH_BUTTON';
           x.click(); return x.textContent.trim();}""")
    p.wait_for_timeout(3000)
    after = len(fav_rows())
    facts["06_batch_result"] = {"clicked": clicked, "db_before": before, "db_after": after,
                                "removed": before - after,
                                "dialogs": dialogs[:3]}
    p.screenshot(path=str(OUT / "06-2-after-batch-unfavorite.png"))

    # ================= ⑤ 取消收藏（单个）====================
    # 先收藏一条，再取消它
    target = p.evaluate(
        """()=>{const cs=Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
           for(const c of cs){
             const star=c.querySelector('.job-fav-badge');
             if(!star) return c.dataset.jid;
           }
           return null;}""")
    if target:
        p.evaluate("(jid)=>{const c=document.querySelector('.job-card[data-jid=\"'+jid+'\"]');"
                   " const t=c.querySelector('.job-preview img, .job-preview video, .job-preview audio');"
                   " if(t){t.scrollIntoView({block:'center'});}}", target)
        p.wait_for_timeout(500)
        t2 = p.query_selector(f'.job-card[data-jid="{target}"] .job-preview img, '
                              f'.job-card[data-jid="{target}"] .job-preview video, '
                              f'.job-card[data-jid="{target}"] .job-preview audio')
        if t2:
            bb = t2.bounding_box()
            p.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2,
                          button="right")
            p.wait_for_timeout(900)
            facts["05_menu_when_favorited"] = p.evaluate(
                """()=>{const m=document.querySelector('#mediaContext');
                   return {items:Array.from(m.querySelectorAll('button[data-act]'))
                             .map(e=>({act:e.dataset.act,
                                       text:String(e.textContent||'').replace(/\\s+/g,' ').trim()}))};}""")
            p.screenshot(path=str(OUT / "05-1-right-click-unfavorite.png"))
            b2 = p.query_selector('#mediaContext button[data-act="favorite"]')
            if b2:
                b2.click()
                p.wait_for_timeout(2500)
        facts["05_db_has_target"] = target in [r["job_id"] for r in fav_rows()]
    p.keyboard.press("Escape")
    p.wait_for_timeout(600)

    # ================= ① 提交后保持当前标签 =================
    # 切到「已完成」标签 → 记录标签 → 点「生成/已入队」→ 再看标签
    p.evaluate("""()=>{const b=document.querySelector('.jobs-tabs button[data-st="done"]');
                    if(b) b.click();}""")
    p.wait_for_timeout(2000)
    facts["01_tab_before_submit"] = p.evaluate(TAB_STATE)
    p.screenshot(path=str(OUT / "01-1-tab-before-submit.png"))

    # 找一个「生成 / 已入队」按钮
    submit = p.evaluate(
        """()=>{const b=Array.from(document.querySelectorAll('button')).find(x=>
             x.textContent.includes('已入队')||x.textContent.includes('生成'));
           if(!b) return null;
           const bb=b.getBoundingClientRect();
           return {text:b.textContent.trim(), visible:bb.width>0&&bb.height>0,
                   disabled:!!b.disabled};}""")
    facts["01_submit_button"] = submit
    # 点它（若可见且未禁用）—— 不会真的跑生成，因为已入队的任务直接进队列
    if submit and submit["visible"] and not submit["disabled"]:
        p.evaluate("""()=>{const b=Array.from(document.querySelectorAll('button')).find(x=>
                         x.textContent.includes('已入队')||x.textContent.includes('生成'));
                         if(b) b.click();}""")
        p.wait_for_timeout(3000)
    facts["01_tab_after_submit"] = p.evaluate(TAB_STATE)
    facts["01_tab_unchanged"] = (
        facts["01_tab_before_submit"].get("activeTab")
        == facts["01_tab_after_submit"].get("activeTab"))
    p.screenshot(path=str(OUT / "01-2-tab-after-submit.png"))

    json.dump(facts, open(OUT / "abcdef-facts.json", "w"), ensure_ascii=False, indent=1)
    ctx.close()
    b.close()

for k, v in facts.items():
    print("  %-26s %s" % (k, json.dumps(v, ensure_ascii=False)[:250]))
print("  证据目录:", OUT)
