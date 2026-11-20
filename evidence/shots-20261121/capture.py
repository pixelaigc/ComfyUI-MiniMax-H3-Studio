#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
H3 Studio (http://127.0.0.1:8190) 真实渲染截图 + 视觉/响应式测量。
只读：不触碰任何 repo 文件，只 GET 页面 + 点击导航。
"""
import json, os, sys, time
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8190/"
OUT = "/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/shots-20261121"
SHOTS = os.path.join(OUT, "shots")
os.makedirs(SHOTS, exist_ok=True)

# ---------------- JS 测量脚本 ----------------
MEASURE = r"""
(() => {
  const VW = window.innerWidth, VH = window.innerHeight;
  const sig = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cn = (typeof el.className === 'string' ? el.className : '').trim();
    if (cn) s += '.' + cn.split(/\s+/).slice(0,3).join('.');
    return s;
  };
  const txt = (el) => (el.textContent || '').trim().replace(/\s+/g,' ');
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    return true;
  };
  const all = Array.from(document.querySelectorAll('*'));

  // --- 1. 横向溢出 ---
  const hOverflow = {
    docScrollW: document.documentElement.scrollWidth,
    docClientW: document.documentElement.clientWidth,
    bodyScrollW: document.body.scrollWidth,
    innerW: VW,
    hasHScroll: document.documentElement.scrollWidth > VW + 1,
    offenders: []
  };
  all.forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    const overRight = r.right - VW;
    const overLeft = -r.left;
    if (overRight > 1 || overLeft > 1) {
      hOverflow.offenders.push({
        sel: sig(el),
        left: Math.round(r.left), right: Math.round(r.right),
        w: Math.round(r.width), overRight: Math.round(overRight), overLeft: Math.round(overLeft),
        txt: txt(el).slice(0,40)
      });
    }
  });
  hOverflow.offenders.sort((a,b) => (b.overRight+b.overLeft) - (a.overRight+a.overLeft));
  hOverflow.offenders = hOverflow.offenders.slice(0, 25);

  // --- 2. 文本截断/溢出容器 ---
  const clipped = [];
  all.forEach(el => {
    if (!visible(el)) return;
    const hasText = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim().length > 0);
    if (!hasText) return;
    const dx = el.scrollWidth - el.clientWidth;
    const dy = el.scrollHeight - el.clientHeight;
    if (dx > 1) {
      const cs = getComputedStyle(el);
      clipped.push({
        sel: sig(el), dx, dy,
        clientW: el.clientWidth, scrollW: el.scrollWidth, clientH: el.clientHeight, scrollH: el.scrollHeight,
        overflowX: cs.overflowX, whiteSpace: cs.whiteSpace, textOverflow: cs.textOverflow,
        txt: txt(el).slice(0,50)
      });
    }
  });
  clipped.sort((a,b) => b.dx - a.dx);

  // --- 3. 元素重叠（关键区域粗测）---
  const overlapCandidates = [
    {name:'topbar', sel:'.topbar'},
    {name:'sidebar', sel:'#sidebar'},
    {name:'panel', sel:'#panel'},
    {name:'jobsArea', sel:'#jobsArea'},
    {name:'jobsHead', sel:'.jobs-head'},
    {name:'jobsTabs', sel:'.jobs-tabs'},
    {name:'jobsGrid', sel:'#jobsGrid'},
    {name:'jobsPagination', sel:'#jobsPagination'},
    {name:'genBtn', sel:'#genBtn'},
    {name:'promptField', sel:'#promptField'},
  ];
  const boxes = overlapCandidates.map(o => {
    const el = document.querySelector(o.sel);
    if (!el || !visible(el)) return null;
    const r = el.getBoundingClientRect();
    return {name:o.name, sel:o.sel, l:r.left, t:r.top, r:r.right, b:r.bottom, w:r.width, h:r.height};
  }).filter(Boolean);
  const overlaps = [];
  for (let i=0;i<boxes.length;i++) for (let j=i+1;j<boxes.length;j++) {
    const a = boxes[i], b = boxes[j];
    const ox = Math.min(a.r,b.r) - Math.max(a.l,b.l);
    const oy = Math.min(a.b,b.b) - Math.max(a.t,b.t);
    if (ox > 2 && oy > 2) {
      const inter = ox*oy;
      const smaller = Math.min(a.w*a.h, b.w*b.h);
      overlaps.push({a:a.name, b:b.name, overlapPx:Math.round(inter), pctOfSmaller:Math.round(inter/smaller*100),
                    aRect:[Math.round(a.l),Math.round(a.t),Math.round(a.w),Math.round(a.h)],
                    bRect:[Math.round(b.l),Math.round(b.t),Math.round(b.w),Math.round(b.h)]});
    }
  }

  // 卡片内部重叠：job-card 直接子元素互相重叠
  const cardOverlaps = [];
  document.querySelectorAll('.job-card').forEach((card, ci) => {
    if (!visible(card)) return;
    const kids = Array.from(card.children).filter(visible);
    for (let i=0;i<kids.length;i++) for (let j=i+1;j<kids.length;j++) {
      const a = kids[i].getBoundingClientRect(), b = kids[j].getBoundingClientRect();
      const ox = Math.min(a.right,b.right) - Math.max(a.left,b.left);
      const oy = Math.min(a.bottom,b.bottom) - Math.max(a.top,b.top);
      if (ox > 3 && oy > 3) {
        const inter = ox*oy, smaller = Math.min(a.width*a.height, b.width*b.height);
        if (inter/smaller > 0.18)
          cardOverlaps.push({card: ci, a: sig(kids[i]), b: sig(kids[j]),
                             pct: Math.round(inter/smaller*100),
                             aRect:[Math.round(a.left),Math.round(a.top),Math.round(a.width),Math.round(a.height)],
                             bRect:[Math.round(b.left),Math.round(b.top),Math.round(b.width),Math.round(b.height)]});
      }
    }
  });

  // --- 4. 对比度 ---
  function parseRGB(s){ const m = s.match(/rgba?\(([^)]+)\)/); if(!m) return null;
    const p = m[1].split(',').map(x=>parseFloat(x.trim())); return {r:p[0],g:p[1],b:p[2],a:p.length>3?p[3]:1}; }
  function effBg(el){
    let node = el, stack = [];
    while (node && node.nodeType === 1) {
      const cs = getComputedStyle(node);
      const c = parseRGB(cs.backgroundColor);
      if (c && c.a > 0) { stack.push(c); if (c.a >= 0.999) break; }
      node = node.parentElement;
    }
    let base = {r:255,g:255,b:255,a:1};
    for (let i = stack.length-1; i>=0; i--) {
      const c = stack[i];
      base = {r: c.r*c.a + base.r*(1-c.a), g: c.g*c.a + base.g*(1-c.a), b: c.b*c.a + base.b*(1-c.a), a:1};
    }
    return base;
  }
  function lum(c){ const f=(v)=>{v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);};
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); }
  function ratio(fg,bg){ const L1=lum(fg),L2=lum(bg); const a=Math.max(L1,L2),b=Math.min(L1,L2); return (a+0.05)/(b+0.05); }

  const lowContrast = [];
  all.forEach(el => {
    if (!visible(el)) return;
    const own = Array.from(el.childNodes).some(n=>n.nodeType===3 && n.textContent.trim().length>0);
    if (!own) return;
    const t = txt(el); if (!t) return;
    const cs = getComputedStyle(el);
    const fg = parseRGB(cs.color); if (!fg) return;
    const bg = effBg(el);
    const fgC = {r: fg.r*fg.a + bg.r*(1-fg.a), g: fg.g*fg.a + bg.g*(1-fg.a), b: fg.b*fg.a + bg.b*(1-fg.a)};
    const r = ratio(fgC, bg);
    const fs = parseFloat(cs.fontSize), fw = parseInt(cs.fontWeight)||400;
    const large = fs >= 24 || (fs >= 18.66 && fw >= 700);
    const need = large ? 3.0 : 4.5;
    if (r < need) {
      lowContrast.push({sel:sig(el), ratio:+r.toFixed(2), need, fontSize:fs, weight:fw,
        color: cs.color, bg: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`,
        txt: t.slice(0,42)});
    }
  });
  lowContrast.sort((a,b)=>a.ratio-b.ratio);

  // --- 5. 触控目标 < 44px ---
  const smallTargets = [];
  const CLICK = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [onclick], .chip, [class*=chip]';
  document.querySelectorAll(CLICK).forEach(el => {
    if (!visible(el)) return;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return;
    if (r.width < 44 || r.height < 44) {
      smallTargets.push({sel: sig(el), w: Math.round(r.width), h: Math.round(r.height),
        x: Math.round(r.x), y: Math.round(r.y), txt: txt(el).slice(0,30)});
    }
  });

  // --- 6. 额外：导航项是否溢出自身容器 ---
  const navScroll = document.querySelector('.nav-scroll');
  const navOverflow = navScroll ? {scrollW: navScroll.scrollWidth, clientW: navScroll.clientWidth,
      scrollH: navScroll.scrollHeight, clientH: navScroll.clientHeight} : null;

  return {
    url: location.href, viewport:[VW,VH],
    hOverflow, clipped: clipped.slice(0,40), overlaps, cardOverlaps: cardOverlaps.slice(0,20),
    lowContrast: lowContrast.slice(0,30), lowContrastCount: lowContrast.length,
    smallTargets: smallTargets.slice(0,60), smallTargetCount: smallTargets.length,
    navOverflow,
    pageHeights: {doc: document.documentElement.scrollHeight, body: document.body.scrollHeight}
  };
})()
"""

results = {}
shots = []


def snap(page, name, note, full=False, clip_sel=None, measure=True):
    path = os.path.join(SHOTS, name + ".png")
    try:
        if clip_sel:
            el = page.query_selector(clip_sel)
            if el:
                el.screenshot(path=path)
            else:
                page.screenshot(path=path, full_page=full)
        else:
            page.screenshot(path=path, full_page=full)
        shots.append({"name": name, "path": path, "note": note})
        print(f"  [shot] {name} -> {path}")
    except Exception as e:
        print(f"  [shot-FAIL] {name}: {e}")
        shots.append({"name": name, "path": None, "note": note, "error": str(e)})
    if measure:
        try:
            results[name] = {"note": note, "metrics": page.evaluate(MEASURE)}
        except Exception as e:
            results[name] = {"note": note, "metrics_error": str(e)}
            print(f"  [measure-FAIL] {name}: {e}")


def click_nav(page, mode, model_key=None):
    """点击左侧导航的视频模式 / 图片音乐模式"""
    sel = None
    if model_key:
        sel = f'#sidebar button.nav-model-item[data-mode="{mode}"][data-model-key="{model_key}"]'
    else:
        sel = f'#sidebar a.nav-item[data-mode="{mode}"]'
    el = page.query_selector(sel)
    if not el:
        print(f"  [nav-miss] {sel}")
        return False
    el.click()
    page.wait_for_timeout(1400)
    return True


def click_sidebar_id(page, sid, drawer=False):
    if drawer:
        tog = page.query_selector('#navToggle')
        if tog and tog.is_visible():
            tog.click()
            page.wait_for_timeout(600)
    el = page.query_selector(f'#{sid}')
    if not el:
        print(f"  [nav-miss] #{sid}")
        return False
    el.click()
    page.wait_for_timeout(1600)
    return True


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none",
        "--force-color-profile=srgb"])
    ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                              device_scale_factor=1, locale="zh-CN")
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)

    print("== 加载主页 ==")
    page.goto(BASE, wait_until="networkidle", timeout=90000)
    page.wait_for_timeout(3000)

    # ---------- 1. 主界面（默认面板 = 当前 active 的图片模式） ----------
    print("== 主界面 ==")
    snap(page, "01-main-default", "主界面默认面板（初始 active 模式）")
    snap(page, "01b-main-panel-detail", "主界面：左侧生成面板 #panel 局部放大", clip_sel="#panel")
    snap(page, "01c-main-topbar-sidebar", "主界面：顶栏 + 左侧导航局部放大", clip_sel=".layout")

    # ---------- 2. 各模式面板 ----------
    print("== 模式面板 ==")
    for mode, mkey, label in [
        ("t2v", "h3_t2v", "文本生视频 T2V"),
        ("flf", "h3_flf", "首尾帧 FLF"),
        ("r2v", "h3_r2v", "参考图 R2V"),
        ("longfilm", "h3_longfilm", "长片拼接"),
        ("t2i", None, "图片生成 Z-IMAGE"),
        ("t2i_qwen", None, "图片生成 Qwen-Image 2.1"),
        ("t2i_moss", None, "图片生成 MOSS-Image"),
        ("t2i_h3image", None, "图片生成 H3-Image"),
        ("i2i_qwen", None, "图片编辑 Qwen"),
        ("i2i_h3image", None, "图片编辑 H3-Image"),
        ("t2a", None, "音乐 文生音乐"),
        ("a2a", None, "音乐 参考生音乐"),
    ]:
        ok = click_nav(page, mode, mkey)
        if not ok:
            continue
        fn = f"02-mode-{mode}"
        snap(page, fn, f"模式面板：{label}（mode={mode}）")
        snap(page, fn + "-panel", f"模式面板局部：{label} #panel", clip_sel="#panel")

    # 回到 T2V 作为后续基线
    click_nav(page, "t2v", "h3_t2v")

    # ---------- 3. 生成媒体库各标签 ----------
    print("== 生成媒体库 ==")
    for st, label in [("all","全部"),("active","进行中"),("done","已完成"),
                       ("longfilm","长片"),("failed","失败"),("canceled","已取消"),("favorites","收藏")]:
        el = page.query_selector(f'.jobs-tabs button[data-st="{st}"]')
        if not el:
            print(f"  [tab-miss] {st}")
            continue
        el.click()
        page.wait_for_timeout(1500)
        snap(page, f"03-media-tab-{st}", f"生成媒体库标签：{label}（data-st={st}）")
        snap(page, f"03-media-tab-{st}-grid", f"生成媒体库标签局部：{label} #jobsArea", clip_sel="#jobsArea")

    # 回到全部
    el = page.query_selector('.jobs-tabs button[data-st="all"]')
    if el:
        el.click(); page.wait_for_timeout(1200)

    # 每页数量选择器（若有）
    sel = page.query_selector('#jobsPagination select, .jobs-pagination select')
    if sel:
        try:
            print("  找到每页数量选择器，枚举选项")
            opts = page.evaluate("""() => {
              const s = document.querySelector('#jobsPagination select, .jobs-pagination select');
              if(!s) return null;
              return Array.from(s.options).map(o=>o.value+'|'+o.text);
            }""")
            results["_perPageOptions"] = opts
        except Exception as e:
            print("   opts err", e)

    # ---------- 4. 素材库页 ----------
    print("== 素材库 ==")
    if click_sidebar_id(page, "navAssets"):
        snap(page, "04-assets-page", "素材库页（#assetsPage）")
        snap(page, "04b-assets-grid", "素材库网格局部", clip_sel="#assetsPage")

    # ---------- 5. 内置 Agent 面板 ----------
    print("== 内置 Agent ==")
    if click_sidebar_id(page, "navAgent"):
        snap(page, "05-agent-page", "内置 Agent 页（#agentPage）")
        snap(page, "05b-agent-body", "内置 Agent 主体局部", clip_sel="#agentPage")

    # ---------- 6. 收藏页（导航项）----------
    if click_sidebar_id(page, "navFavorites"):
        snap(page, "06-favorites-page", "收藏页（#navFavorites）")

    # ---------- 7. 其它系统页（补充参考）----------
    for sid, nm in [("navSystem","07-system-page"), ("navProviders","08-providers-page"), ("navFlowDocs","09-flowdocs-page")]:
        if click_sidebar_id(page, sid):
            snap(page, nm, f"系统页 {sid}")

    # ---------- 8. 窄断点 375px ----------
    print("== 375px 窄屏 ==")
    ctx2 = browser.new_context(viewport={"width":375,"height":812}, device_scale_factor=1,
                               locale="zh-CN", is_mobile=True, has_touch=True)
    p2 = ctx2.new_page()
    p2.goto(BASE, wait_until="networkidle", timeout=90000)
    p2.wait_for_timeout(3000)
    snap(p2, "10-narrow375-main", "375px 主界面")
    snap(p2, "10b-narrow375-panel", "375px 生成面板局部", clip_sel="#panel")

    # 375 媒体库标签
    for st, label in [("all","全部"),("active","进行中"),("done","已完成"),("favorites","收藏")]:
        el = p2.query_selector(f'.jobs-tabs button[data-st="{st}"]')
        if not el:
            print(f"  [narrow tab-miss] {st}")
            continue
        try:
            el.scroll_into_view_if_needed()
            el.click(timeout=5000)
        except Exception as e:
            print(f"  [narrow tab-click-fail] {st}: {e}")
            continue
        p2.wait_for_timeout(1500)
        snap(p2, f"11-narrow375-media-{st}", f"375px 生成媒体库标签：{label}")

    # 375 抽屉导航
    tog = p2.query_selector('#navToggle')
    if tog and tog.is_visible():
        tog.click(); p2.wait_for_timeout(900)
        snap(p2, "12-narrow375-drawer", "375px 左侧抽屉导航展开")
        try:
            p2.keyboard.press('Escape'); p2.wait_for_timeout(500)
        except Exception: pass

    # 375 素材库 + Agent
    if click_sidebar_id(p2, "navAssets", drawer=True):
        snap(p2, "13-narrow375-assets", "375px 素材库页")
    if click_sidebar_id(p2, "navAgent", drawer=True):
        snap(p2, "14-narrow375-agent", "375px 内置 Agent 页")

    ctx2.close()

    results["_consoleErrors"] = errors[:40]
    results["_shotIndex"] = shots
    browser.close()

with open(os.path.join(OUT, "measurements.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

ok_shots = [s for s in shots if s.get("path")]
print(f"\n== 完成：{len(ok_shots)}/{len(shots)} 张截图 ==")
for s in shots:
    print(f"   {s['name']}: {s.get('path')}")
