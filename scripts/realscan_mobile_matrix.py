# -*- coding: utf-8 -*-
"""真机条件扫描 —— 全宽度 × 双引擎 × 完整用户路径。

【本轮为什么做这个】
用户真机上现象**仍然存在**，而上一轮 375/390 两档 + 9 个类型全绿。
差距在哪：
  · **宽度**：只测了 375/390。真机还有 320 / 360 / 412 / 428 / 430；
  · **高度**：只测了 812/844（完整视口）。真机有地址栏/工具栏，
    实际可视高常常只有 640~700；
  · **横屏**：完全没测（667×375 那种）；
  · **引擎**：只测了 Chromium。WebKit（iOS Safari）行为可能不同；
  · **类型**：只测了 9 个，**漏了 3 个** ——
    `i2i_qwen` / `i2i_h3image` / `a2a`（现场 `/api/generation-types`
    的 registered_modes 有 12 个，侧栏 DOM 里也有 12 个 data-mode）。

【本文件做什么】
  1) 宽度/高度矩阵：真实 tap + elementFromPoint 采样，跑完整用户路径
  2) 每一步都记「能不能回类型选择 / 媒体库可见可滚 / 参数抽屉出口 / 遮挡」
  3) 遮挡判据用**采样**（对 position:fixed 元素，纯矩形相交会误判）
  4) 双引擎：Chromium + WebKit（WebKit 不可用时如实记缺口）

【每个宽度跑什么】
  ① 打开类型选择 → 选任一生成类型（覆盖全部 12 个 mode）
  ② 回到类型选择（汉堡 / 抽屉 ×  / 遮罩，三条路各试）
  ③ 媒体库可见 + 卡片可点 + 收藏入口可达（记录滚动距离）
  ④ 参数抽屉打开后的出口（汉堡 / 遮罩 / 抽屉头 ×）
  ⑤ 导航控件遮挡采样
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

# 现场 12 个生成类型（来自 /api/generation-types 的 registered_modes 与侧栏 DOM）
ALL_MODES = [
    "t2v", "flf", "r2v", "longfilm",
    "t2i", "t2i_qwen", "i2i_qwen", "t2i_moss", "t2i_h3image", "i2i_h3image",
    "t2a", "a2a",
]

# 真实机型视口矩阵（宽 × 高 × DPR × safe-area）
#   iPhone SE / 8      375×667  DPR2
#   iPhone X/11 Pro   375×812  DPR3
#   iPhone 12/13 mini 375×812  DPR3
#   iPhone 12/13/14   390×844  DPR3
#   iPhone 14 Pro    393×852  DPR3
#   iPhone 14 Pro Max 430×932  DPR3
#   iPhone 15 Pro Max 430×932  DPR3
#   Pixel 5          393×851  DPR2.75
#   小屏 Android      320×568  DPR2
#   常见 Android      360×800  DPR3
#   Pixel 7 Pro      412×892  DPR2.625
#   iPhone 15 Pro Max 横屏 932×430
VIEWPORTS = [
    (320, 568, 2, "iPhone SE1/小屏Android"),
    (320, 640, 2, "320x640 极窄"),
    (360, 640, 3, "Android 360x640"),
    (360, 800, 3, "Android 360x800"),
    (375, 667, 2, "iPhone SE2/8"),
    (375, 812, 3, "iPhone X/11Pro"),
    (390, 844, 3, "iPhone 12/13/14"),
    (393, 852, 3, "iPhone 14 Pro"),
    (412, 892, 3, "Pixel 7 Pro"),
    (428, 926, 3, "iPhone 12 Pro Max"),
    (430, 932, 3, "iPhone 14/15 Pro Max"),
    (667, 375, 2, "横屏 667x375"),
    (932, 430, 3, "横屏 932x430"),
]

# 环境探针：JS 断点 vs CSS 断点 + safe-area + 关键元素是否存在
ENV_JS = """() => {
  const nav = document.querySelector('#navToggle');
  const cs = nav ? getComputedStyle(nav) : null;
  const sb = document.querySelector('#sidebar');
  const sbcs = sb ? getComputedStyle(sb) : null;
  const panel = document.querySelector('#panel');
  const pcs = panel ? getComputedStyle(panel) : null;
  const fab = document.querySelector('#panelFab');
  const fcs = fab ? getComputedStyle(fab) : null;
  const probe = document.createElement('div');
  probe.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;';
  document.body.appendChild(probe);
  const mq = {
    bp700: matchMedia('(max-width: 700px)').matches,
    bp1100: matchMedia('(max-width: 1100px)').matches,
    bp480: matchMedia('(max-width: 480px)').matches,
    bp380: matchMedia('(max-width: 380px)').matches,
  };
  probe.remove();
  return {
    innerWidth: innerWidth, innerHeight: innerHeight,
    dpr: devicePixelRatio,
    mq: mq,
    jsPhone: window.BREAKPOINTS ? (innerWidth <= window.BREAKPOINTS.phone) : null,
    jsTablet: window.BREAKPOINTS ? (innerWidth <= window.BREAKPOINTS.tablet) : null,
    jsBreakpoints: window.BREAKPOINTS || null,
    hamburgerVisible: cs ? (cs.display !== 'none') : false,
    sidebarPosition: sbcs ? sbcs.position : null,
    panelPosition: pcs ? pcs.position : null,
    fabDisplay: fcs ? fcs.display : null,
    fabHiddenAttr: fab ? fab.hasAttribute('hidden') : null,
    safeAreaBottom: getComputedStyle(document.documentElement)
      .getPropertyValue('--sab') || null,
    bodyOverflow: getComputedStyle(document.body).overflow,
    drawerHeadExists: !!document.querySelector('#navDrawerHead'),
    drawerCloseExists: !!document.querySelector('#navDrawerClose'),
    panelCloseExists: !!document.querySelector('#panel .panel-close, #panelClose'),
    jobsAreaOverflow: (() => { const a = document.querySelector('#jobsArea');
      return a ? getComputedStyle(a).overflowY : null; })(),
  };
}"""

NAV_ITEMS_JS = """() => {
  const out = [];
  for (const el of document.querySelectorAll(
        '#sidebar .nav-item[data-mode], #sidebar .nav-model-item[data-mode]')) {
    const b = el.getBoundingClientRect();
    out.push({mode: el.dataset.mode,
              text: String(el.textContent || '').trim().slice(0, 26),
              cls: String(el.className || '').slice(0, 40),
              inDrawer: !!el.closest('#sidebar')});
  }
  return out;
}"""

# 抽屉/媒体库状态
STATE_JS = """() => {
  const sb = document.querySelector('#sidebar');
  // 【F1】参数抽屉有两个：#panel（普通创作）与 #stitchPanel（长片拼接页）
  //   —— 两者 HTML 上都带 .panel 类，窄屏下都是底部抽屉。
  //   只查 #panel 会漏掉长片拼接那一支（实测它功能是好的）。
  const panel = document.querySelector('#panel.open, #stitchPanel.open')
              || document.querySelector('#panel');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));
  const inView = cards.filter(c => { const b = c.getBoundingClientRect();
    return b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth; });
  const clickable = inView.filter(c => {
    const b = c.getBoundingClientRect();
    const x = Math.round(Math.min(innerWidth-1, Math.max(0, b.left+b.width/2)));
    const y = Math.round(Math.min(innerHeight-1, Math.max(0, b.top+b.height/2)));
    const t = document.elementFromPoint(x,y);
    return t && (t === c || c.contains(t));
  });
  return {
    sidebarOpen: sb.classList.contains('open'),
    panelOpen: !!(panel && panel.classList.contains('open')),
    maskOpen: !!document.querySelector('#sidebarMask.open, #panelMask.open'),
    modalOpen: document.body.classList.contains('modal-open')
                || document.body.classList.contains('job-detail-open'),
    cardsTotal: cards.length,
    cardsInViewport: inView.length,
    cardsClickable: clickable.length,
  };
}"""

# 命中测试采样：对每个目标采样 9 点，看命中的是不是它自己
HIT_SAMPLE_JS = """(cfg) => {
  const out = [];
  for (const sel of cfg.selectors) {
    const el = document.querySelector(sel);
    if (!el) { out.push({sel, found: false}); continue; }
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) {
      out.push({sel, found: true, zeroSize: true,
                rect: {t:Math.round(b.top),l:Math.round(b.left),
                       w:Math.round(b.width),h:Math.round(b.height)}});
      continue;
    }
    let own = 0, blocked = 0, other = 0, sampled = 0;
    let blockerInfo = null;
    for (let i = 1; i <= 3; i++) for (let j = 1; j <= 3; j++) {
      const x = Math.round(b.left + b.width*i/4);
      const y = Math.round(b.top + b.height*j/4);
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      sampled++;
      const t = document.elementFromPoint(x,y);
      if (!t) { other++; continue; }
      if (t === el || el.contains(t)) own++;
      else {
        const ctrl = t.closest && t.closest(
          '#navToggle,#panelFab,#sidebar,#sidebarMask,#panelMask,#panel');
        if (ctrl) { blocked++;
          if (!blockerInfo) blockerInfo = {id: ctrl.id,
            cls: String(ctrl.className||'').slice(0,50)}; }
        else other++;
      }
    }
    out.push({sel, found: true,
      rect: {t:Math.round(b.top),l:Math.round(b.left),
             w:Math.round(b.width),h:Math.round(b.height),
             b:Math.round(b.bottom)},
      inViewport: b.bottom > 0 && b.top < innerHeight,
      sampled, own, blocked, other,
      blocker: blockerInfo,
      fullyReachable: sampled > 0 && blocked === 0});
  }
  return out;
}"""


def tap(page, sel, timeout=6000):
    """真实命中测试点击。返回 (ok, method, blocker)。"""
    try:
        page.tap(sel, timeout=timeout)
        return True, "tap", None
    except Exception as e1:
        blocker = page.evaluate(
            """(s) => { const el = document.querySelector(s);
               if (!el) return {reason:'not-found'};
               const b = el.getBoundingClientRect();
               if (!b.width || !b.height) return {reason:'zero-size'};
               const x=Math.round(b.left+b.width/2), y=Math.round(b.top+b.height/2);
               if (x<0||y<0||x>innerWidth||y>innerHeight)
                 return {reason:'offscreen', x, y};
               const t=document.elementFromPoint(x,y);
               return {reason:'hit-other', hitTag:t?t.tagName:null, hitId:t?t.id:null,
                       hitCls:t?String(t.className||'').slice(0,60):null}; }""", sel)
        try:
            page.click(sel, timeout=timeout)
            return True, "click-fallback", blocker
        except Exception:
            return False, "failed", blocker


def reset(page):
    """清干净模态框与抽屉状态。

    【关键】必须走 `window.H3Studio.refreshResponsiveChrome()`。
    直接 `classList.remove('open')` **绕过** setSidebarOpen / setPanelOpen，
    而 FAB 的显隐判据就在 updateResponsiveChrome() 里
    ⇒ 直接摘类会让 FAB 保持 hidden ⇒ 后面「参数抽屉打不开」，
    看起来像产品坏了，其实是**测试脚本自己把状态搞脏了**。

    【实测】
      初始:            {hiddenAttr: false, display: "flex", visible: true}
      开抽屉后:        {hiddenAttr: true,  display: "none", visible: false}
      点 × 关闭后:     {hiddenAttr: false, display: "flex", visible: true}  ✅
      JS 直接摘 class: {hiddenAttr: true,  display: "none", visible: false} ❌ 卡死
    """
    page.evaluate("""() => {
      document.querySelectorAll('.prompt-modal, .job-detail-modal, .lightbox')
        .forEach(m => { m.hidden = true; });
      document.body.classList.remove('modal-open', 'job-detail-open');
      document.querySelectorAll('.sidebar,.panel,.stitch-panel')
        .forEach(e => e.classList.remove('open'));
      document.querySelectorAll('#sidebarMask,#panelMask')
        .forEach(e => e.classList.remove('open'));
      // 通知响应式 chrome 重新算 FAB 显隐（正规出口）
      if (window.H3Studio && window.H3Studio.refreshResponsiveChrome) {
        window.H3Studio.refreshResponsiveChrome();
      }
    }""")
    page.wait_for_timeout(250)


def run_path(page, mode, shot_dir, shot_prefix):
    """跑一遍完整用户路径，返回结构化结果。"""
    r = {"mode": mode}

    # --- 步骤 1：打开类型选择 ---
    reset(page)
    # 【F3】宽度 > 700px 是**平板/桌面档**：侧栏常驻、没有汉堡。
    #   那一档根本没有「打开类型选择」这个动作 —— 侧栏本来就在那儿。
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
            """() => { const sb = document.querySelector('#sidebar');
               if (!sb) return false;
               const b = sb.getBoundingClientRect();
               return b.width > 0 && b.height > 0
                      && getComputedStyle(sb).position !== 'fixed'; }""")

    # --- 步骤 2：选该生成类型 ---
    sel = f'.nav-item[data-mode="{mode}"], .nav-model-item[data-mode="{mode}"]'
    exists = page.evaluate(
        """(s) => !!document.querySelector(s)""", sel)
    if not exists:
        r["skipped"] = "该 mode 不在侧栏 DOM 里"
        return r
    ok, method, blocker = tap(page, sel)
    # 【F4】选之前先把目标项滚进可视区。
    #   侧栏的滚动容器是 #nav-scroll；矮/横屏视口下
    #   「长片拼接」在第 427px（视口高仅 430），点不到。
    #   ⇒ 先 scrollIntoView，再点。
    scrolled = page.evaluate(
        """(s) => {
          const el = document.querySelector(s);
          const sc = document.querySelector('#navScroll');
          if (!el) return {found: false};
          const b = el.getBoundingClientRect();
          const wasInView = b.bottom > 0 && b.top < innerHeight;
          if (!wasInView && sc) {
            el.scrollIntoView({block: 'nearest'});
            return {found: true, scrolled: true};
          }
          return {found: true, scrolled: false, wasInView: wasInView};
        }""", sel)
    if scrolled.get("scrolled"):
        page.wait_for_timeout(400)
        ok, method, blocker = tap(page, sel)
        r["select_type"] = {"ok": ok, "method": method, "blocker": blocker,
                            "scrolledIntoView": True}
    else:
        r["select_type"] = {"ok": ok, "method": method, "blocker": blocker,
                            "scrolledIntoView": False}
    page.wait_for_timeout(800)
    st = page.evaluate(STATE_JS)
    r["after_select"] = st
    r["drawer_auto_closed"] = not st["sidebarOpen"]
    r["media_library_visible_after_select"] = st["cardsInViewport"] > 0
    r["media_library_clickable_after_select"] = st["cardsClickable"] > 0
    active = page.evaluate(
        """() => { const a=document.querySelector('.nav-item.active,.nav-model-item.active');
           return a ? (a.dataset.mode||null) : null; }""")
    r["active_mode"] = active
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-1-after-select.png"))

    # --- 步骤 3：回到类型选择（手机档=汉堡；平板档=侧栏常驻，天然可达）---
    if r.get("is_phone_layout", True):
        ok, method, blocker = tap(page, "#navToggle")
        r["back_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        st = page.evaluate(STATE_JS)
        r["reached_picker"] = st["sidebarOpen"]
    else:
        r["back_via_hamburger"] = {"ok": True, "method": "sidebar-always-visible"}
        r["reached_picker"] = page.evaluate(
            """() => {
               const sc = document.querySelector('#navScroll');
               const el = document.querySelector('#sidebar .nav-item[data-mode]');
               if (!el) return false;
               if (sc) el.scrollIntoView({block: 'nearest'});
               const b = el.getBoundingClientRect();
               if (b.width === 0 || b.height === 0) return false;
               const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
               const t = document.elementFromPoint(x, y);
               return !!(t && (t === el || el.contains(t))); }""")
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-2-back-to-picker.png"))

    # 抽屉内的返回/关闭控件
    r["picker_controls"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#navDrawerHead", "#navDrawerClose", "#navToggle"]})

    # 收藏入口可达性
    fav = page.evaluate(
        """() => { const el=document.querySelector('#navFavorites');
           if(!el) return {present:false};
           const sc=document.querySelector('#navScroll');
           const b=el.getBoundingClientRect();
           const inView = b.bottom>0 && b.top<innerHeight;
           let need=0;
           if(sc && !inView){
             const sr=sc.getBoundingClientRect();
             need=Math.max(0,(b.bottom-sr.bottom)+8);
           }
           return {present:true, inView, rect:{t:Math.round(b.top),b:Math.round(b.bottom)},
                   scrollNeeded: Math.round(need),
                   navScroll: sc?{max:Math.round(sc.scrollHeight-sc.clientHeight),
                                  h:Math.round(sc.clientHeight)}:null}; }""")
    r["favorites_entry"] = fav

    # 关抽屉
    reset(page)
    page.wait_for_timeout(400)

    # --- 步骤 4：媒体库可见 + 卡片可点 + 详情 ---
    st = page.evaluate(STATE_JS)
    # 【重要】矮视口（320×568）下媒体库头部占 558px（标签 + 类型标签 + 筛选器 + 每页），
    #   卡片网格在第 691px 才开始 —— 首屏**必然**看不到卡片，需要滚动 647px。
    #   这不是缺陷（用户本来就要滚），但判据必须是「**滚动后**能点到」。
    need_scroll = page.evaluate(
        """() => {
          const area = document.querySelector('#jobsArea');
          const card = document.querySelector('#jobsGrid .job-card[data-jid]');
          if (!area || !card) return 0;
          const ar = area.getBoundingClientRect();
          const cr = card.getBoundingClientRect();
          return Math.max(0, Math.round(cr.top - ar.top));
        }""")
    r["media_library_scroll_needed"] = need_scroll
    if (st["cardsClickable"] == 0 and need_scroll > 0):
        page.evaluate(
            """() => {
              const area = document.querySelector('#jobsArea');
              const card = document.querySelector('#jobsGrid .job-card[data-jid]');
              if (!area || !card) return;
              area.scrollTop += card.getBoundingClientRect().top
                                - area.getBoundingClientRect().top;
            }""")
        page.wait_for_timeout(500)
        st = page.evaluate(STATE_JS)
    r["media_library"] = {"total": st["cardsTotal"], "inViewport": st["cardsInViewport"],
                           "clickable": st["cardsClickable"],
                           "scrollNeeded": need_scroll}
    try:
        page.tap("#jobsGrid .job-card[data-jid]", timeout=6000)
        page.wait_for_timeout(800)
        r["detail_open"] = page.evaluate(
            """() => { const m=document.querySelector('#detailModal');
               if(!m||m.hasAttribute('hidden')) return false;
               const b=m.getBoundingClientRect();
               return b.width>0&&b.height>0; }""")
        body = page.evaluate(
            """() => { const b=document.querySelector('#detailModalBody');
               return b?String(b.textContent||'').replace(/\\s+/g,' '):''; }""")
        r["detail_has_params"] = {
            "steps": ("步数" in body) or ("steps" in body.lower()),
            "scheduler": ("调度器" in body) or ("scheduler" in body.lower()),
            "seed": ("种子" in body) or ("seed" in body.lower()),
        }
        if shot_prefix:
            page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-{mode}-3-detail.png"))
        page.tap("#detailModalClose", timeout=6000)
        page.wait_for_timeout(500)
    except Exception as e:
        r["detail_open"] = "FAIL: " + str(e).split("\n")[0][:150]

    # --- 步骤 5：参数抽屉的三个出口 ---
    reset(page)
    exits = {}
    # 出口 A：点汉堡
    ok, method, blocker = tap(page, "#panelFab")
    page.wait_for_timeout(700)
    st = page.evaluate(STATE_JS)
    exits["fab_open"] = st["panelOpen"]
    if st["panelOpen"]:
        ok, method, blocker = tap(page, "#navToggle")
        exits["exit_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        st = page.evaluate(STATE_JS)
        exits["panel_closed_after_hamburger"] = not st["panelOpen"]
        exits["sidebar_open_after_hamburger"] = st["sidebarOpen"]
        # 出口 B：关侧栏（× 或遮罩）
        ok, method, blocker = tap(page, "#navDrawerClose")
        exits["exit_via_drawer_x"] = {"ok": ok, "method": method}
        page.wait_for_timeout(500)
    else:
        exits["exit_via_hamburger"] = {"ok": False, "reason": "参数抽屉没打开"}
    reset(page)

    # 出口 C：点遮罩关参数抽屉
    tap(page, "#panelFab")
    page.wait_for_timeout(700)
    st = page.evaluate(STATE_JS)
    if st["panelOpen"]:
        # 遮罩露出的一条：顶栏下沿 + 2px
        geom = page.evaluate(
            """() => { const m=document.querySelector('#panelMask');
               const p=document.querySelector('#panel');
               const tb=document.querySelector('.topbar');
               if(!m||!p||!tb) return null;
               const fb=m.getBoundingClientRect(), pb=p.getBoundingClientRect(),
                     tbb=tb.getBoundingClientRect();
               const x=Math.round(fb.left+fb.width/2);
               for(let y=Math.round(tbb.bottom); y<innerHeight; y+=2){
                 const t=document.elementFromPoint(x,y);
                 if(t&&(t===m||m.contains(t))) return {x,y};
               }
               return null; }""")
        if geom:
            try:
                page.touchscreen.tap(geom["x"], geom["y"])
                page.wait_for_timeout(700)
                exits["exit_via_mask"] = {"ok": True, "tap": geom,
                    "panel_closed": not page.evaluate(
                        "() => !!document.querySelector('#panel.open')")}
            except Exception as e:
                exits["exit_via_mask"] = {"ok": False, "err": str(e).split("\n")[0][:120]}
        else:
            exits["exit_via_mask"] = {"ok": False, "reason": "遮罩没有可点区域"}
    else:
        exits["exit_via_mask"] = {"ok": False, "reason": "参数抽屉没打开"}
    r["params_drawer_exits"] = exits

    # --- 步骤 6：导航控件遮挡采样 ---
    reset(page)
    r["overlap_closed"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#jobsGrid .job-card[data-jid]", "#navToggle", "#panelFab"]})
    # 抽屉打开时（手机档才有抽屉；平板档侧栏常驻，直接测）
    if r.get("is_phone_layout", True):
        tap(page, "#navToggle")
        page.wait_for_timeout(700)
    r["overlap_drawer_open"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#sidebar .nav-item", "#navToggle", "#panelFab"]})
    reset(page)

    # 汇总判据
    r["pass"] = bool(
        r.get("picker_open")
        and r.get("drawer_auto_closed")
        and r.get("reached_picker")
        and r.get("media_library", {}).get("clickable", 0) > 0
        and r.get("detail_open") is True
        and r.get("params_drawer_exits", {}).get("fab_open")
        and (r.get("params_drawer_exits", {}).get("panel_closed_after_hamburger")
             or r.get("params_drawer_exits", {}).get("exit_via_mask", {}).get("panel_closed"))
    )
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "realscan"))
    ap.add_argument("--engines", default="chromium,webkit")
    ap.add_argument("--modes", default="")
    ap.add_argument("--viewports", default="")
    ap.add_argument("--tag", default="live")
    ap.add_argument("--no-shot", dest="shot", action="store_false", default=True)
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    modes = [m.strip() for m in args.modes.split(",") if m.strip()] or ALL_MODES

    if args.viewports:
        vps = []
        for part in args.viewports.split(","):
            w, h = part.lower().split("x")
            vps.append((int(w), int(h), 3, "custom"))
    else:
        vps = VIEWPORTS

    results = {"base": base, "tag": args.tag, "engines": {}}

    with sync_playwright() as pw:
        for eng_name in args.engines.split(","):
            eng_name = eng_name.strip()
            try:
                if eng_name == "webkit":
                    browser = pw.webkit.launch(headless=True)
                else:
                    browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            except Exception as e:
                results["engines"][eng_name] = {
                    "available": False,
                    "error": str(e).split("\n")[0][:300]}
                print(f"[{eng_name}] 引擎不可用: {str(e).split(chr(10))[0][:180]}")
                continue
            results["engines"][eng_name] = {"available": True, "viewports": {}}
            for (w, h, dpr, label) in vps:
                key = f"{w}x{h}"
                print(f"[{eng_name}] {key} ({label}) ...", flush=True)
                ctx = browser.new_context(
                    viewport={"width": w, "height": h},
                    is_mobile=True, has_touch=True, device_scale_factor=dpr)
                pg = ctx.new_page()
                pg.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
                try:
                    pg.wait_for_selector("#jobsGrid .job-card[data-jid]", timeout=45000)
                except Exception:
                    pass
                pg.wait_for_timeout(2200)
                vdir = outdir / f"{eng_name}-{key}"
                vdir.mkdir(parents=True, exist_ok=True)
                vp = {"label": label, "dpr": dpr,
                      "env": pg.evaluate(ENV_JS),
                      "nav_items": pg.evaluate(NAV_ITEMS_JS),
                      "modes": {}}
                for mode in modes:
                    vp["modes"][mode] = run_path(
                        pg, mode, vdir, f"{eng_name}-{key}")
                results["engines"][eng_name]["viewports"][key] = vp
                ctx.close()
            browser.close()

    (outdir / f"realscan-{args.tag}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 汇总 ----
    print("\n===== 汇总 =====")
    for eng, e in results["engines"].items():
        if not e.get("available"):
            print(f"  {eng}: 引擎不可用 ❌")
            continue
        for key, vp in e["viewports"].items():
            env = vp["env"]
            ok = sum(1 for m in vp["modes"].values() if m.get("pass"))
            tot = len(vp["modes"])
            js_ok = (env.get("jsPhone") == env.get("mq", {}).get("bp700"))
            print(f"  {eng:9s} {key:9s} {vp['label']:22s} "
                  f"{ok}/{tot} 通过   JS/CSS断点一致={'✅' if js_ok else '❌ ' + str(env.get('jsPhone')) + ' vs ' + str(env.get('mq'))}")
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
