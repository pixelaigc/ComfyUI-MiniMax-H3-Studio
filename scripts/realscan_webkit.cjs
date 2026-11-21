#!/usr/bin/env node
/**
 * WebKit 引擎版的真机条件扫描（与 Python 版 scripts/realscan_mobile_matrix.py 同口径）。
 *
 * 【为什么单独写一个 Node 版】
 * 本机 Python playwright（1.60+）要 webkit-2359，而本机已装的是 webkit-2272
 * （对应 playwright 1.59）。Node 侧全局装的正好是 playwright 1.59.1
 * ⇒ 用 Node 跑 webkit-2272 才起得来。
 *
 * 【WebKit 在本机的启动方式】
 *   xvfb-run -a --server-args="-screen 0 1280x1024x24" node scripts/realscan_webkit.mjs
 * 原因：webkit-2272 只有 GTK 通道可用（依赖已补齐），GTK 需要 X display。
 *
 * 【与 Python 版同口径的判据】
 *   · page.tap()（真命中测试 + touch 事件）
 *   · elementFromPoint 采样（9 点/元素）
 *   · 完整用户路径：开类型选择 → 选类型 → 回类型选择 → 媒体库 → 详情
 *     → 参数抽屉三个出口 → 遮挡采样
 */
const { webkit } = require('/usr/local/lib/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE || 'http://127.0.0.1:8190';
const OUT = process.env.OUT || '/root/projects/h3s-favorites/evidence/mobile-nav/realscan';

const ALL_MODES = ['t2v', 'flf', 'r2v', 'longfilm', 't2i', 't2i_qwen', 'i2i_qwen',
  't2i_moss', 't2i_h3image', 'i2i_h3image', 't2a', 'a2a'];

const VIEWPORTS = [
  [320, 568, 2, 'iPhone SE1/小屏Android'],
  [360, 640, 3, 'Android 360x640'],
  [375, 667, 2, 'iPhone SE2/8'],
  [375, 812, 3, 'iPhone X/11Pro'],
  [390, 844, 3, 'iPhone 12/13/14'],
  [412, 892, 3, 'Pixel 7 Pro'],
  [428, 926, 3, 'iPhone 12 Pro Max'],
  [430, 932, 3, 'iPhone 14/15 Pro Max'],
  [667, 375, 2, '横屏 667x375'],
  [932, 430, 3, '横屏 932x430'],
];

const ENV_JS = `(() => {
  const nav = document.querySelector('#navToggle');
  const cs = nav ? getComputedStyle(nav) : null;
  const sb = document.querySelector('#sidebar');
  const sbcs = sb ? getComputedStyle(sb) : null;
  const panel = document.querySelector('#panel');
  const pcs = panel ? getComputedStyle(panel) : null;
  const fab = document.querySelector('#panelFab');
  const fcs = fab ? getComputedStyle(fab) : null;
  return {
    innerWidth, innerHeight, dpr: devicePixelRatio,
    mq: { bp700: matchMedia('(max-width: 700px)').matches,
           bp1100: matchMedia('(max-width: 1100px)').matches,
           bp480: matchMedia('(max-width: 480px)').matches,
           bp380: matchMedia('(max-width: 380px)').matches },
    jsBreakpoints: window.BREAKPOINTS || null,
    jsPhone: window.BREAKPOINTS ? (innerWidth <= window.BREAKPOINTS.phone) : null,
    hamburgerVisible: cs ? (cs.display !== 'none') : false,
    sidebarPosition: sbcs ? sbcs.position : null,
    panelPosition: pcs ? pcs.position : null,
    fabDisplay: fcs ? fcs.display : null,
    drawerHeadExists: !!document.querySelector('#navDrawerHead'),
    drawerCloseExists: !!document.querySelector('#navDrawerClose'),
    bodyOverflow: getComputedStyle(document.body).overflow,
    ua: navigator.userAgent.slice(0, 110),
    supportsTouch: 'ontouchstart' in window || navigator.maxTouchPoints > 0,
  };
})()`;

const STATE_JS = `(() => {
  const sb = document.querySelector('#sidebar');
  // 【F1】参数抽屉有两个：#panel（普通创作）与 #stitchPanel（长片拼接页）
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
  return { sidebarOpen: sb.classList.contains('open'),
           panelOpen: !!(panel && panel.classList.contains('open')),
           cardsTotal: cards.length, cardsInViewport: inView.length,
           cardsClickable: clickable.length };
})()`;

const HIT_SAMPLE_JS = (cfg) => `(() => {
  const selectors = ${JSON.stringify(cfg.selectors)};
  const out = [];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el) { out.push({sel, found: false}); continue; }
    const b = el.getBoundingClientRect();
    if (b.width === 0 || b.height === 0) {
      out.push({sel, found: true, zeroSize: true}); continue;
    }
    let own=0, blocked=0, other=0, sampled=0, blocker=null;
    for (let i=1;i<=3;i++) for (let j=1;j<=3;j++) {
      const x = Math.round(b.left + b.width*i/4);
      const y = Math.round(b.top + b.height*j/4);
      if (x<0||y<0||x>=innerWidth||y>=innerHeight) continue;
      sampled++;
      const t = document.elementFromPoint(x,y);
      if (!t) { other++; continue; }
      if (t === el || el.contains(t)) { own++; continue; }
      const ctrl = t.closest && t.closest('#navToggle,#panelFab,#sidebar,#sidebarMask,#panelMask,#panel');
      if (ctrl) { blocked++; if (!blocker) blocker = {id: ctrl.id, cls: String(ctrl.className||'').slice(0,50)}; }
      else other++;
    }
    out.push({sel, found:true, sampled, own, blocked, other, blocker,
      inViewport: b.bottom>0 && b.top<innerHeight,
      rect:{t:Math.round(b.top),l:Math.round(b.left),w:Math.round(b.width),h:Math.round(b.height)},
      fullyReachable: sampled>0 && blocked===0});
  }
  return out;
})()`;

async function tap(page, sel, timeout = 6000) {
  try {
    await page.tap(sel, { timeout });
    return { ok: true, method: 'tap' };
  } catch (e1) {
    const blocker = await page.evaluate(`((s) => { const el = document.querySelector(s);
      if (!el) return {reason:'not-found'};
      const b = el.getBoundingClientRect();
      if (!b.width||!b.height) return {reason:'zero-size'};
      const x=Math.round(b.left+b.width/2), y=Math.round(b.top+b.height/2);
      if (x<0||y<0||x>innerWidth||y>innerHeight) return {reason:'offscreen',x,y};
      const t=document.elementFromPoint(x,y);
      return {reason:'hit-other', hitTag:t?t.tagName:null, hitId:t?t.id:null,
              hitCls:t?String(t.className||'').slice(0,60):null}; })()`, sel);
    try {
      await page.click(sel, { timeout });
      return { ok: true, method: 'click-fallback', blocker };
    } catch (e2) {
      return { ok: false, method: 'failed', blocker,
               err: String(e2).split('\n')[0].slice(0, 180) };
    }
  }
}

async function reset(page) {
  await page.evaluate(`(() => {
    document.querySelectorAll('.prompt-modal, .job-detail-modal, .lightbox')
      .forEach(m => { m.hidden = true; });
    document.body.classList.remove('modal-open', 'job-detail-open');
    document.querySelectorAll('.sidebar,.panel,.stitch-panel')
      .forEach(e => e.classList.remove('open'));
    document.querySelectorAll('#sidebarMask,#panelMask')
      .forEach(e => e.classList.remove('open'));
    if (window.H3Studio && window.H3Studio.refreshResponsiveChrome) {
      window.H3Studio.refreshResponsiveChrome();
    }
  })()`);
  await page.waitForTimeout(250);
}

async function runPath(page, mode, vdir, prefix) {
  const r = { mode };
  await reset(page);

  // 【F3】宽度 > 700px 是平板/桌面档：侧栏常驻、没有汉堡 ⇒ 没有「打开类型选择」这个动作
  const isPhone = await page.evaluate(`(() => window.BREAKPOINTS
    ? innerWidth <= window.BREAKPOINTS.phone
    : matchMedia('(max-width: 700px)').matches)()`);
  r.is_phone_layout = isPhone;
  if (isPhone) {
    const open = await tap(page, '#navToggle');
    r.open_picker = open;
    await page.waitForTimeout(600);
    let st0 = await page.evaluate(STATE_JS);
    r.picker_open = st0.sidebarOpen;
    if (!st0.sidebarOpen) { r.fatal = '打不开类型选择（汉堡点不动）'; return r; }
  } else {
    r.open_picker = { ok: true, method: 'sidebar-always-visible' };
    r.picker_open = await page.evaluate(`(() => {
      const sb = document.querySelector('#sidebar');
      if (!sb) return false;
      const b = sb.getBoundingClientRect();
      return b.width > 0 && b.height > 0 && getComputedStyle(sb).position !== 'fixed'; })()`);
  }

  const sel = `.nav-item[data-mode="${mode}"], .nav-model-item[data-mode="${mode}"]`;
  const exists = await page.evaluate((s) => !!document.querySelector(s), sel);
  if (!exists) { r.skipped = '该 mode 不在侧栏 DOM 里'; return r; }
  // 【F4】选之前先把目标项滚进可视区（侧栏滚动容器是 #navScroll）
  const scrolled = await page.evaluate(`((s) => {
    const el = document.querySelector(s);
    const sc = document.querySelector('#navScroll');
    if (!el) return {found: false};
    const b = el.getBoundingClientRect();
    const wasInView = b.bottom > 0 && b.top < innerHeight;
    if (!wasInView && sc) { el.scrollIntoView({block: 'nearest'}); return {found:true, scrolled:true}; }
    return {found:true, scrolled:false}; })()`, sel);
  if (scrolled.scrolled) { await page.waitForTimeout(400); }
  r.select_type = await tap(page, sel);
  r.select_type.scrolledIntoView = !!scrolled.scrolled;
  await page.waitForTimeout(800);
  st = await page.evaluate(STATE_JS);
  r.after_select = st;
  r.drawer_auto_closed = !st.sidebarOpen;
  r.media_library_clickable_after_select = st.cardsClickable > 0;
  r.active_mode = await page.evaluate(
    `() => { const a=document.querySelector('.nav-item.active,.nav-model-item.active');
       return a ? (a.dataset.mode||null) : null; }`);
  await page.screenshot({ path: path.join(vdir, `${prefix}-${mode}-1-after-select.png`) });

  // 回类型选择（手机档=汉堡；平板档=侧栏常驻，天然可达）
  if (r.is_phone_layout !== false) {
    r.back_via_hamburger = await tap(page, '#navToggle');
    await page.waitForTimeout(700);
    st = await page.evaluate(STATE_JS);
    r.reached_picker = st.sidebarOpen;
  } else {
    r.back_via_hamburger = { ok: true, method: 'sidebar-always-visible' };
    r.reached_picker = await page.evaluate(`(() => {
      const sc = document.querySelector('#navScroll');
      const el = document.querySelector('#sidebar .nav-item[data-mode]');
      if (!el) return false;
      if (sc) el.scrollIntoView({block: 'nearest'});
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) return false;
      const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
      const t = document.elementFromPoint(x, y);
      return !!(t && (t === el || el.contains(t))); })()`);
  }
  await page.screenshot({ path: path.join(vdir, `${prefix}-${mode}-2-back-to-picker.png`) });
  r.picker_controls = await page.evaluate(HIT_SAMPLE_JS, {
    selectors: ['#navDrawerHead', '#navDrawerClose', '#navToggle'] });

  // 收藏入口可达性
  r.favorites_entry = await page.evaluate(`(() => {
    const el=document.querySelector('#navFavorites');
    if(!el) return {present:false};
    const sc=document.querySelector('#navScroll');
    const b=el.getBoundingClientRect();
    const inView = b.bottom>0 && b.top<innerHeight;
    let need=0;
    if(sc && !inView){ const sr=sc.getBoundingClientRect();
      need=Math.max(0,(b.bottom-sr.bottom)+8); }
    return {present:true, inView, scrollNeeded:Math.round(need),
            rect:{t:Math.round(b.top),b:Math.round(b.bottom)}}; })()`);

  await reset(page);
  await page.waitForTimeout(400);

  // 媒体库 + 详情
  st = await page.evaluate(STATE_JS);
  // 【F2】矮视口下卡片在首屏之外（需滚动）
  const needScroll = await page.evaluate(`(() => {
    const area = document.querySelector('#jobsArea');
    const card = document.querySelector('#jobsGrid .job-card[data-jid]');
    if (!area || !card) return 0;
    const ar = area.getBoundingClientRect(), cr = card.getBoundingClientRect();
    return Math.max(0, Math.round(cr.top - ar.top)); })()`);
  r.media_library_scroll_needed = needScroll;
  if (st.cardsClickable === 0 && needScroll > 0) {
    await page.evaluate(`(() => {
      const area = document.querySelector('#jobsArea');
      const card = document.querySelector('#jobsGrid .job-card[data-jid]');
      if (!area || !card) return;
      area.scrollTop += card.getBoundingClientRect().top - area.getBoundingClientRect().top; })()`);
    await page.waitForTimeout(500);
    st = await page.evaluate(STATE_JS);
  }
  r.media_library = { total: st.cardsTotal, inViewport: st.cardsInViewport,
                      clickable: st.cardsClickable, scrollNeeded: needScroll };
  try {
    await page.tap('#jobsGrid .job-card[data-jid]', { timeout: 6000 });
    await page.waitForTimeout(800);
    r.detail_open = await page.evaluate(`(() => { const m=document.querySelector('#detailModal');
      if(!m||m.hasAttribute('hidden')) return false;
      const b=m.getBoundingClientRect(); return b.width>0&&b.height>0; })()`);
    const body = await page.evaluate(`(() => { const b=document.querySelector('#detailModalBody');
      return b?String(b.textContent||'').replace(/\\s+/g,' '):''; })()`);
    r.detail_has_params = {
      steps: body.includes('步数') || /steps/i.test(body),
      scheduler: body.includes('调度器') || /scheduler/i.test(body),
      seed: body.includes('种子') || /seed/i.test(body) };
    await page.screenshot({ path: path.join(vdir, `${prefix}-${mode}-3-detail.png`) });
    await page.tap('#detailModalClose', { timeout: 6000 });
    await page.waitForTimeout(500);
  } catch (e) { r.detail_open = 'FAIL: ' + String(e).split('\n')[0].slice(0, 150); }

  // 参数抽屉三个出口
  await reset(page);
  const exits = {};
  await tap(page, '#panelFab');
  await page.waitForTimeout(700);
  st = await page.evaluate(STATE_JS);
  exits.fab_open = st.panelOpen;
  if (st.panelOpen) {
    exits.exit_via_hamburger = await tap(page, '#navToggle');
    await page.waitForTimeout(700);
    st = await page.evaluate(STATE_JS);
    exits.panel_closed_after_hamburger = !st.panelOpen;
    exits.sidebar_open_after_hamburger = st.sidebarOpen;
    exits.exit_via_drawer_x = await tap(page, '#navDrawerClose');
    await page.waitForTimeout(500);
  } else { exits.exit_via_hamburger = { ok: false, reason: '参数抽屉没打开' }; }
  await reset(page);

  await tap(page, '#panelFab');
  await page.waitForTimeout(700);
  st = await page.evaluate(STATE_JS);
  if (st.panelOpen) {
    const geom = await page.evaluate(`(() => {
      const m=document.querySelector('#panelMask'), p=document.querySelector('#panel');
      const tb=document.querySelector('.topbar');
      if(!m||!p||!tb) return null;
      const fb=m.getBoundingClientRect(), pb=p.getBoundingClientRect(), tbb=tb.getBoundingClientRect();
      const x=Math.round(fb.left+fb.width/2);
      for(let y=Math.round(tbb.bottom); y<innerHeight; y+=2){
        const t=document.elementFromPoint(x,y);
        if(t&&(t===m||m.contains(t))) return {x,y};
      }
      return null; })()`);
    if (geom) {
      try {
        await page.touchscreen.tap(geom.x, geom.y);
        await page.waitForTimeout(700);
        exits.exit_via_mask = { ok: true, tap: geom,
          panel_closed: !(await page.evaluate(`(() => !!document.querySelector('#panel.open'))()`)) };
      } catch (e) {
        exits.exit_via_mask = { ok: false, err: String(e).split('\n')[0].slice(0, 120) };
      }
    } else { exits.exit_via_mask = { ok: false, reason: '遮罩没有可点区域' }; }
  } else { exits.exit_via_mask = { ok: false, reason: '参数抽屉没打开' }; }
  r.params_drawer_exits = exits;

  // 遮挡采样
  await reset(page);
  r.overlap_closed = await page.evaluate(HIT_SAMPLE_JS, {
    selectors: ['#jobsGrid .job-card[data-jid]', '#navToggle', '#panelFab'] });
  if (r.is_phone_layout !== false) {
    await tap(page, '#navToggle');
    await page.waitForTimeout(700);
  }
  r.overlap_drawer_open = await page.evaluate(HIT_SAMPLE_JS, {
    selectors: ['#sidebar .nav-item', '#navToggle', '#panelFab'] });
  await reset(page);

  r.pass = !!(r.picker_open && r.drawer_auto_closed && r.reached_picker &&
    (r.media_library || {}).clickable > 0 && r.detail_open === true &&
    exits.fab_open && (exits.panel_closed_after_hamburger ||
      (exits.exit_via_mask || {}).panel_closed));
  return r;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await webkit.launch({ headless: true });
  const results = { base: BASE, engine: 'webkit', viewports: {} };
  for (const [w, h, dpr, label] of VIEWPORTS) {
    const key = `${w}x${h}`;
    console.log(`[webkit] ${key} (${label}) ...`, flush=true);
    const ctx = await browser.newContext({
      viewport: { width: w, height: h }, isMobile: true, hasTouch: true,
      deviceScaleFactor: dpr });
    const pg = await ctx.newPage();
    await pg.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
    try { await pg.waitForSelector('#jobsGrid .job-card[data-jid]', { timeout: 45000 }); }
    catch (e) { /* 继续，环境探针会记录 */ }
    await pg.waitForTimeout(2200);
    const vdir = path.join(OUT, `webkit-${key}`);
    fs.mkdirSync(vdir, { recursive: true });
    const vp = { label, dpr, env: await pg.evaluate(ENV_JS), modes: {} };
    for (const mode of ALL_MODES) {
      vp.modes[mode] = await runPath(pg, mode, vdir, `webkit-${key}`);
    }
    results.viewports[key] = vp;
    await ctx.close();
  }
  await browser.close();
  fs.writeFileSync(path.join(OUT, 'realscan-webkit.json'),
    JSON.stringify(results, null, 2));
  console.log('\n===== WebKit 汇总 =====');
  for (const [key, vp] of Object.entries(results.viewports)) {
    const ok = Object.values(vp.modes).filter(m => m.pass).length;
    const env = vp.env;
    const jsOk = env.jsPhone === env.mq.bp700;
    console.log(`  ${key.padEnd(9)} ${vp.label.padEnd(24)} ${ok}/${Object.keys(vp.modes).length} 通过  JS/CSS断点一致=${jsOk ? '✅' : '❌'}`);
  }
  console.log('\nOUT:', OUT);
})().catch(e => { console.error('FAIL', e); process.exit(1); });
