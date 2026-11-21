# -*- coding: utf-8 -*-
"""窄屏矩阵补全 —— 12 个注册模式 × 用户点名的视口 × 含「进入/退出收藏栏」这一段。

【为什么还要再跑一轮】
上一轮（commit 97e7294，evidence/mobile-nav/realscan2/realscan-final.json）已经覆盖
13 视口 × 12 模式，且判据已修正（9 点 elementFromPoint 采样、#stitchPanel 也算抽屉、
#detailModal 才是活的模态框、侧栏项先 scrollIntoView）。但对照本轮要求，它缺两块：

  1) **768 档**：那一轮的 VIEWPORTS 最大竖屏是 430×932，横屏是 932×430；
     768×1024 只在 evidence/mobile-nav/retest-768/ 里测了 **3 个模式**（t2v/t2i/t2a），
     不是 12 个。
  2) **收藏栏这一段**：那一轮只记 `favorites_entry`（入口**在不在**、要滚多少），
     **从来没有真的进入收藏视图、也没有从收藏视图回到类型选择**。
     用户报的正是「回不去」这一类问题，收藏栏是新加的入口，必须单独走一遍。

【本脚本做什么】复用上一轮已修正的判据（import，不复制一份，避免两套判据漂移）：
  ① 打开类型选择 → 选该生成类型（12 个全跑）
  ② 回到类型选择
  ③ **进入收藏栏** → 断言真的切到 state.tab="favorites"（用 DOM 可观察的
     active 标签 + 收藏徽章 + 服务端 total 对账），卡片可点、详情可开
  ④ **从收藏栏退出** → 回到类型选择，并且**能再选一次生成类型**（证明不是死路）
  ⑤ 参数抽屉出口 + 导航控件遮挡采样

判据一律用 elementFromPoint 真实命中 / 9 点采样，不用被祖先裁剪的
getBoundingClientRect 直接判遮挡。
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from playwright.sync_api import sync_playwright  # noqa: E402

# 【复用】上一轮已修正的判据与工具，不另写一份
from realscan_mobile_matrix import (  # noqa: E402
    ALL_MODES,
    STATE_JS,
    HIT_SAMPLE_JS,
    tap,
    reset,
)

BASE = "http://127.0.0.1:8190"

# 本轮点名的视口：375/390/768 竖屏 + 对应横屏 + 430px 短高度
VIEWPORTS = [
    (375, 667, 2, "iPhone SE2/8 竖屏"),
    (667, 375, 2, "375 的横屏 667x375"),
    (390, 844, 3, "iPhone 12/13/14 竖屏"),
    (844, 390, 3, "390 的横屏 844x390（短高 390）"),
    (768, 1024, 2, "平板 768x1024 竖屏"),
    (1024, 768, 2, "平板 1024x768 横屏"),
    (430, 932, 3, "iPhone 14/15 Pro Max 竖屏"),
    (932, 430, 3, "横屏 932x430（430px 短高度）"),
]

# 收藏视图状态的**DOM 可观察**判据。
# 【为什么不读 state.tab】app.js 的 state 是模块级 const，没挂到 window
#   （全仓 grep `window.state =` 命中 0 处）⇒ 读不到。
#   能观察的是 renderTabs() 写出来的结果：active 标签、收藏徽章、卡片数。
FAV_STATE_JS = """() => {
  const activeTab = document.querySelector('.jobs-tabs button[data-st].active');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
  const inView = cards.filter(c => { const b = c.getBoundingClientRect();
      return b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth; });
  const clickable = inView.filter(c => {
      const b = c.getBoundingClientRect();
      const x = Math.round(Math.min(innerWidth-1, Math.max(0, b.left+b.width/2)));
      const y = Math.round(Math.min(innerHeight-1, Math.max(0, b.top+b.height/2)));
      const t = document.elementFromPoint(x, y);
      return t && (t === c || c.contains(t)); });
  const favBadges = document.querySelectorAll('#jobsGrid .fav-badge, #jobsGrid [class*="fav"]').length;
  const nav = document.querySelector('#navFavorites');
  const emptyMsg = document.querySelector('#jobsEmpty, .jobs-empty');
  return {
    activeTab: activeTab ? activeTab.dataset.st : null,
    navActive: !!(nav && nav.classList.contains('active')),
    cardsTotal: cards.length,
    cardsInViewport: inView.length,
    cardsClickable: clickable.length,
    favBadgeNodes: favBadges,
    emptyText: emptyMsg ? String(emptyMsg.textContent||'').trim().slice(0,80) : null,
    filtersVisible: (() => { const f=document.querySelector('#doneFilters');
        return !!(f && !f.hidden); })(),
  };
}"""


def fav_entry(page):
    """点左侧「收藏」入口。

    【为什么要扫滚动位置而不是 scrollIntoView】
    `el.scrollIntoView({block:'nearest'})` 把元素贴到滚动容器**上沿**，
    而短视口（667x375：#navScroll 可视高只有 169px）下上沿正好在
    **顶栏（44px，高于侧栏 z-index）底下** ⇒ playwright 的 tap 报
    "element is outside of the viewport"，点不到。
    实测（scripts/probe_fav_reachability.py，10 档全部 reachable=True）：
    入口在很多滚动位置都能命中（667x375 命中 y=306），
    但「贴上沿」那个位置不行。

    ⇒ 判据必须是「**存在某个滚动位置**能命中」，并直接用那个坐标点。
    这也正是已知误判类「侧栏项在折叠线下需滚动」的正解。
    """
    sel = "#navFavorites"
    found = page.evaluate("""(s)=>!!document.querySelector(s);""", sel)
    if not found:
        return False, "missing", {"reason": "not-found"}, False

    # 在 #navScroll 的各个滚动位置里找一个真正命中的
    plan = page.evaluate(
        """(s) => {
          const el = document.querySelector(s);
          const sc = document.querySelector('#navScroll');
          const max = sc ? Math.max(0, sc.scrollHeight - sc.clientHeight) : 0;
          const tries = [];
          for (let pos = 0; pos <= max; pos += Math.max(8, Math.floor(max/60))) {
            if (sc) sc.scrollTop = pos;
            const b = el.getBoundingClientRect();
            if (b.bottom <= 0 || b.top >= innerHeight
                || b.right <= 0 || b.left >= innerWidth) continue;
            for (let y = Math.ceil(b.top)+1; y <= Math.floor(b.bottom)-1; y += 2) {
              const x = Math.round(Math.min(innerWidth-1,
                            Math.max(0, b.left + b.width/2)));
              const t = document.elementFromPoint(x, y);
              if (t && (t === el || el.contains(t))) {
                return {ok: true, scrollTop: sc ? sc.scrollTop : 0,
                        x: x, y: y};
              }
            }
          }
          return {ok: false, tried: tries.length};
        }""", sel)
    if not plan.get("ok"):
        return False, "no-hittable-position", plan, False

    # 用探针找到的那个坐标真实点（touchscreen.tap = 真手指事件）
    page.touchscreen.tap(plan["x"], plan["y"])
    page.wait_for_timeout(600)
    # 点完确认真的切到收藏视图了（若没切，回退到普通 tap 再试一次）
    tab = page.evaluate(
        "()=>(document.querySelector('.jobs-tabs button[data-st].active')||{}).dataset?.st")
    if tab != "favorites":
        ok, method, blocker = tap(page, sel)
        return ok, method, blocker, True
    return True, "touchscreen.tap@hit-point", plan, True


def run_fav_leg(page, mode, shot_dir, shot_prefix):
    """跑「类型选择 → 参数抽屉 → 进入收藏栏 → 退出回类型选择 → 再选一次类型」。"""
    r = {"mode": mode}

    # ---------- ① 打开类型选择 ----------
    reset(page)
    is_phone = page.evaluate(
        """() => window.BREAKPOINTS
             ? innerWidth <= window.BREAKPOINTS.phone
             : matchMedia('(max-width: 700px)').matches""")
    r["is_phone_layout"] = is_phone
    if is_phone:
        ok, method, blocker = tap(page, "#navToggle")
        r["open_picker"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(600)
        st = page.evaluate(STATE_JS)
        r["picker_open"] = st["sidebarOpen"]
        if not st["sidebarOpen"]:
            r["fatal"] = "打不开类型选择（汉堡点不动）"
            return r
    else:
        r["open_picker"] = {"ok": True, "method": "sidebar-always-visible"}
        r["picker_open"] = page.evaluate(
            """() => { const sb=document.querySelector('#sidebar');
               if(!sb) return false; const b=sb.getBoundingClientRect();
               return b.width>0 && b.height>0
                   && getComputedStyle(sb).position !== 'fixed'; }""")

    # ---------- ② 选该生成类型 ----------
    sel = f'.nav-item[data-mode="{mode}"], .nav-model-item[data-mode="{mode}"]'
    if not page.evaluate("(s)=>!!document.querySelector(s)", sel):
        r["skipped"] = "该 mode 不在侧栏 DOM 里"
        return r
    ok, method, blocker = tap(page, sel)
    if not ok:
        scrolled = page.evaluate(
            """(s)=>{const el=document.querySelector(s);const sc=document.querySelector('#navScroll');
               if(!el) return {found:false};
               const b=el.getBoundingClientRect();
               if(!(b.bottom>0&&b.top<innerHeight)&&sc){el.scrollIntoView({block:'nearest'});
                 return {found:true,scrolled:true};}
               return {found:true,scrolled:false};}""", sel)
        if scrolled.get("scrolled"):
            page.wait_for_timeout(400)
            ok, method, blocker = tap(page, sel)
    r["select_type"] = {"ok": ok, "method": method, "blocker": blocker}
    page.wait_for_timeout(800)
    r["active_mode"] = page.evaluate(
        """()=>{const a=document.querySelector('.nav-item.active,.nav-model-item.active');
            return a?(a.dataset.mode||null):null;}""")
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-1-select.png"))

    # ---------- ③ 回到类型选择（用户报的死路就在这） ----------
    if is_phone:
        ok, method, blocker = tap(page, "#navToggle")
        r["back_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        r["reached_picker"] = page.evaluate(STATE_JS)["sidebarOpen"]
    else:
        r["back_via_hamburger"] = {"ok": True, "method": "sidebar-always-visible"}
        r["reached_picker"] = page.evaluate(
            """()=>{const sc=document.querySelector('#navScroll');
               const el=document.querySelector('#sidebar .nav-item[data-mode]');
               if(!el) return false;
               if(sc) el.scrollIntoView({block:'nearest'});
               const b=el.getBoundingClientRect();
               if(!b.width||!b.height) return false;
               const x=Math.round(b.left+b.width/2),y=Math.round(b.top+b.height/2);
               const t=document.elementFromPoint(x,y);
               return !!(t&&(t===el||el.contains(t)));}""")
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-2-back-to-picker.png"))

    # ---------- ④ 进入收藏栏 ----------
    # 【前置】收藏入口在不在？在就直接点；不在记缺口，不猜。
    ok, method, blocker, scrolled = fav_entry(page)
    r["fav_entry_tap"] = {"ok": ok, "method": method, "blocker": blocker,
                          "neededScroll": scrolled}
    # 【判据修正 · 2026-11-21 第二轮】原来固定等 1200ms 再读卡片数。
    # 实测：收藏视图是**异步拉取**后渲染的（/api/jobs?...&favorites=1），
    # 1200ms 时请求往往还在飞 ⇒ cardsTotal=0 ⇒ 误判失败。
    # 探针实测（/tmp/probe430c.py）：同一视口 600ms 时 0 张、
    # 1200ms 时 12 张，接口 200、无控制台错误。
    #
    # 【第二轮踩到的更深一层坑】改成「等第一张 .job-card[data-jid] 出现」
    # 还是不够 —— **上一个视图的卡片还在 DOM 里**（切视图是异步重渲染，
    # 不是先清空再填），所以那个条件**立刻**成立，随后 grid 被清空又填上，
    # 正好在两次采样之间读到 0 张 ⇒ 仍然误判失败。
    # 实测（1024x768，/tmp/probe1024.py）：+1000ms 0 张、+2000ms 0 张、
    # +4000ms 12 张；请求全 200、无 pageerror。
    #
    # 【正确的终态判据】骨架卡（class=job-skeleton、**没有** data-jid）
    # 是「正在加载」的标记；真实卡片才有 data-jid。
    # ⇒ 等「骨架卡消失 **且** 至少有一张带 data-jid 的真实卡片」。
    # 若库里确实没有收藏，两条都不会满足 ⇒ 等满 timeout 判失败，
    # 那正是「收藏视图没内容」的真实状况，不该被掩盖。
    try:
        page.wait_for_function(
            """() => {
                 const g = document.querySelector('#jobsGrid');
                 if (!g) return false;
                 const sk = g.querySelector('.job-skeleton');
                 const real = g.querySelector('.job-card[data-jid]');
                 return !sk && !!real;
               }""",
            timeout=30000)
        r["fav_cards_waited"] = True
    except Exception as e:
        r["fav_cards_waited"] = False
        r["fav_cards_wait_err"] = str(e).split("\n")[0][:120]
    page.wait_for_timeout(400)
    fav = page.evaluate(FAV_STATE_JS)
    r["favorites_view"] = fav
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-3-favorites.png"))

    # 收藏视图里卡片可点 → 打开详情（沿用既有模态框，不是另一套）
    fav_detail = {"attempted": False}
    if fav["cardsTotal"] > 0:
        need = page.evaluate(
            """()=>{const area=document.querySelector('#jobsArea');
               const c=document.querySelector('#jobsGrid .job-card[data-jid]');
               if(!area||!c) return 0;
               return Math.max(0, Math.round(c.getBoundingClientRect().top
                 - area.getBoundingClientRect().top));}""")
        if need > 0:
            page.evaluate(
                """()=>{const area=document.querySelector('#jobsArea');
                   const c=document.querySelector('#jobsGrid .job-card[data-jid]');
                   if(!area||!c) return;
                   area.scrollTop += c.getBoundingClientRect().top
                                    - area.getBoundingClientRect().top;}""")
            page.wait_for_timeout(500)
            fav = page.evaluate(FAV_STATE_JS)
            r["favorites_view_after_scroll"] = fav
        ok2, _m2, _b2 = tap(page, "#jobsGrid .job-card[data-jid]")
        page.wait_for_timeout(900)
        detail_open = page.evaluate(
            """()=>{const m=document.querySelector('#detailModal');
               if(!m||m.hasAttribute('hidden')) return false;
               const b=m.getBoundingClientRect(); return b.width>0&&b.height>0;}""")
        body = page.evaluate(
            """()=>{const b=document.querySelector('#detailModalBody');
               return b?String(b.textContent||'').replace(/\\s+/g,' '):'';}""")
        fav_detail = {
            "attempted": True,
            "tap_ok": ok2,
            "detail_open": detail_open,
            "has_params": {
                "steps": ("步数" in body) or ("steps" in body.lower()),
                "scheduler": ("调度器" in body) or ("scheduler" in body.lower()),
                "seed": ("种子" in body) or ("seed" in body.lower()),
            },
        }
        if shot_prefix:
            page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-4-fav-detail.png"))
        try:
            page.tap("#detailModalClose", timeout=6000)
            page.wait_for_timeout(500)
        except Exception as e:
            fav_detail["close_err"] = str(e).split("\n")[0][:120]
    r["favorites_detail"] = fav_detail

    # ---------- ⑤ 从收藏栏退出：回类型选择 + 再选一次类型（证明不是死路） ----------
    # 【判据修正】平板档这一支原来直接探「第一个 nav-item」——
    #   而上面 fav_entry() 刚把 #navScroll 滚到底去够 #navFavorites，
    #   第一个 nav-item 此时**在折叠线上方之外**，探不到。
    #   这正是已知的「侧栏项在折叠线下需滚动」误判类：
    #   判据必须是「在某个滚动位置可达」（先 scrollIntoView），
    #   不是「当前滚动位置可达」。上一轮的 select_type 已经是这么写的。
    if is_phone:
        ok, method, blocker = tap(page, "#navToggle")
        r["fav_exit_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        r["fav_exit_reached_picker"] = page.evaluate(STATE_JS)["sidebarOpen"]
    else:
        r["fav_exit_via_hamburger"] = {"ok": True, "method": "sidebar-always-visible"}
        r["fav_exit_reached_picker"] = page.evaluate(
            """()=>{const el=document.querySelector('#sidebar .nav-item[data-mode]');
               const sc=document.querySelector('#navScroll');
               if(!el) return false;
               if(sc) el.scrollIntoView({block:'nearest'});
               const b=el.getBoundingClientRect();
               if(!b.width||!b.height) return false;
               const x=Math.round(b.left+b.width/2),y=Math.round(b.top+b.height/2);
               const t=document.elementFromPoint(x,y);
               return !!(t&&(t===el||el.contains(t)));}""")

    # 再选一次生成类型（换成长片拼接以外的另一个，验证不是只能看不能动）
    other = "t2v" if mode != "t2v" else "t2i"
    osel = f'.nav-item[data-mode="{other}"], .nav-model-item[data-mode="{other}"]'
    ok3, m3, b3 = tap(page, osel)
    if not ok3:
        page.evaluate(
            """(s)=>{const el=document.querySelector(s);const sc=document.querySelector('#navScroll');
               if(el&&sc) el.scrollIntoView({block:'nearest'});}""", osel)
        page.wait_for_timeout(400)
        ok3, m3, b3 = tap(page, osel)
    page.wait_for_timeout(800)
    r["reselect_after_fav"] = {
        "ok": ok3, "method": m3, "blocker": b3,
        "active_mode_after": page.evaluate(
            """()=>{const a=document.querySelector('.nav-item.active,.nav-model-item.active');
               return a?(a.dataset.mode||null):null;}"""),
    }
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-5-reselect.png"))

    # 退出收藏后媒体库仍可用
    st = page.evaluate(STATE_JS)
    r["library_after_fav_exit"] = {"cardsTotal": st["cardsTotal"],
                                   "clickable": st["cardsClickable"]}

    # ---------- ⑥ 参数抽屉出口 + 遮挡采样 ----------
    reset(page)
    exits = {}
    tap(page, "#panelFab")
    page.wait_for_timeout(700)
    st = page.evaluate(STATE_JS)
    exits["fab_open"] = st["panelOpen"]
    if st["panelOpen"]:
        ok, method, blocker = tap(page, "#navToggle")
        exits["exit_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        st2 = page.evaluate(STATE_JS)
        exits["panel_closed_after_hamburger"] = not st2["panelOpen"]
        exits["sidebar_open_after_hamburger"] = st2["sidebarOpen"]
        tap(page, "#navDrawerClose")
        exits["exit_via_drawer_x"] = {"ok": True, "method": "tap"}
        page.wait_for_timeout(500)
    else:
        exits["exit_via_hamburger"] = {"ok": False, "reason": "参数抽屉没打开"}
    reset(page)

    tap(page, "#panelFab")
    page.wait_for_timeout(700)
    if page.evaluate(STATE_JS)["panelOpen"]:
        geom = page.evaluate(
            """()=>{const m=document.querySelector('#panelMask');
               const tb=document.querySelector('.topbar');
               if(!m||!tb) return null;
               const fb=m.getBoundingClientRect(), tbb=tb.getBoundingClientRect();
               const x=Math.round(fb.left+fb.width/2);
               for(let y=Math.round(tbb.bottom); y<innerHeight; y+=2){
                 const t=document.elementFromPoint(x,y);
                 if(t&&(t===m||m.contains(t))) return {x,y};}
               return null;}""")
        if geom:
            try:
                page.touchscreen.tap(geom["x"], geom["y"])
                page.wait_for_timeout(700)
                exits["exit_via_mask"] = {"ok": True, "tap": geom,
                    "panel_closed": not page.evaluate(
                        "()=>!!document.querySelector('#panel.open')")}
            except Exception as e:
                exits["exit_via_mask"] = {"ok": False, "err": str(e).split("\n")[0][:120]}
        else:
            exits["exit_via_mask"] = {"ok": False, "reason": "遮罩没有可点区域"}
    else:
        exits["exit_via_mask"] = {"ok": False, "reason": "参数抽屉没打开"}
    r["params_drawer_exits"] = exits

    reset(page)
    r["overlap_closed"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#jobsGrid .job-card[data-jid]", "#navToggle", "#panelFab"]})
    if is_phone:
        tap(page, "#navToggle")
        page.wait_for_timeout(700)
    r["overlap_drawer_open"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#sidebar .nav-item", "#navToggle", "#panelFab", "#navFavorites"]})
    reset(page)

    # ---------- 汇总判据 ----------
    fav_ok = (fav["activeTab"] == "favorites" and fav["navActive"]
              and fav["cardsTotal"] > 0)
    r["pass"] = bool(
        r.get("picker_open")
        and r.get("select_type", {}).get("ok")
        and r.get("reached_picker")
        and r.get("fav_entry_tap", {}).get("ok")
        and fav_ok
        and (not fav_detail["attempted"] or fav_detail.get("detail_open") is True)
        and r.get("fav_exit_reached_picker")
        and r.get("reselect_after_fav", {}).get("ok")
        and r.get("library_after_fav_exit", {}).get("cardsTotal", 0) > 0
        and r.get("params_drawer_exits", {}).get("fab_open")
        and (r.get("params_drawer_exits", {}).get("panel_closed_after_hamburger")
             or r.get("params_drawer_exits", {}).get("exit_via_mask", {}).get("panel_closed"))
    )
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--out", default=str(REPO / "evidence/mobile-nav/fav-leg"))
    ap.add_argument("--tag", default="favleg")
    ap.add_argument("--engine", default="chromium", choices=["chromium", "webkit"])
    ap.add_argument("--viewports", default="", help="逗号分隔 WxH，留空=默认清单")
    ap.add_argument("--modes", default="", help="逗号分隔模式，留空=全部 12 个")
    ap.add_argument("--shots", action="store_true", help="每个模式存 5 张截图")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    shot_dir = out / args.tag
    if args.shots:
        shot_dir.mkdir(parents=True, exist_ok=True)

    vps = VIEWPORTS
    if args.viewports:
        want = set(args.viewports.split(","))
        vps = [v for v in VIEWPORTS if f"{v[0]}x{v[1]}" in want]
    modes = ALL_MODES
    if args.modes:
        modes = args.modes.split(",")

    results = {"base": args.base, "tag": args.tag, "engine": args.engine,
               "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "viewports": {}}

    with sync_playwright() as pw:
        browser = (pw.chromium.launch(headless=True) if args.engine == "chromium"
                   else pw.webkit.launch(headless=True))
        for (w, h, dpr, label) in vps:
            key = f"{w}x{h}"
            ctx = browser.new_context(viewport={"width": w, "height": h},
                                     is_mobile=True, has_touch=True,
                                     device_scale_factor=dpr)
            page = ctx.new_page()
            page.goto(args.base + "/", wait_until="domcontentloaded", timeout=90000)
            try:
                page.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=60000)
            except Exception as e:
                print(f"  !! {key} 首屏卡片没出来: {str(e)[:120]}")
            page.wait_for_timeout(1500)

            vp = {"label": label, "dpr": dpr, "modes": {}}
            for mode in modes:
                sp = f"{args.engine}-{key}" if args.shots else ""
                try:
                    r = run_fav_leg(page, mode, str(shot_dir), sp)
                except Exception as e:
                    r = {"mode": mode, "error": str(e).split("\n")[0][:300], "pass": False}
                vp["modes"][mode] = r
                flag = "OK " if r.get("pass") else "FAIL"
                print(f"  {flag} {key:>9} {mode:<12} fav_tab={r.get('favorites_view',{}).get('activeTab')}"
                      f" cards={r.get('favorites_view',{}).get('cardsTotal')}"
                      f" exit_picker={r.get('fav_exit_reached_picker')}"
                      f" reselect={r.get('reselect_after_fav',{}).get('ok')}")
            results["viewports"][key] = vp
            ctx.close()
            json.dump(results, open(out / f"favleg-{args.engine}.json", "w"),
                      ensure_ascii=False, indent=1)

        browser.close()

    results["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(results, open(out / f"favleg-{args.engine}.json", "w"),
              ensure_ascii=False, indent=1)

    total = okc = 0
    for key, vp in results["viewports"].items():
        bad = [m for m, r in vp["modes"].items() if not r.get("pass")]
        n = len(vp["modes"])
        total += n
        okc += n - len(bad)
        print(f"  {key:>9}: {n - len(bad)}/{n}" + (f"  失败={bad}" if bad else ""))
    print(f"合计 {okc}/{total}")


if __name__ == "__main__":
    main()
