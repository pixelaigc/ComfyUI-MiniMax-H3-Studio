# -*- coding: utf-8 -*-
"""移动端导航死路 —— 全量复测台（验收清单 1)-6) 逐条取证）。

【为什么必须重测而不是引用上一轮结论】
用户 2026-11-21 仍然报同一个现象。上一轮的结论建立在
`page.evaluate("...click()")` + Playwright click 的混合体上，
且只测了 375px 一档、只测了部分类型。本轮要求：
  · 两档视口（375 / 390）
  · 覆盖**全部**生成类型面板
  · **真实 touch/pointer 事件**（不是 JS .click()）
  · **命中测试** elementFromPoint 权威判定
  · headed（Xvfb）与 headless 交叉确认

【真实触摸事件 vs JS click()】
  · `element.click()`  —— 直接派发 click，**绕过命中测试**，能点中任何元素。
    上一轮矩阵里「抽屉打开了」就是这么来的，不等于用户手指点得到。
  · `page.tap()`      —— Playwright 走真��命中测试（elementFromPoint），
    且发的是 touchstart/touchend，is_mobile+has_touch 上下文里最接近真手指。
  · 本台优先用 page.tap()；tap 不可用时降级 page.click()（同样带命中测试）
    并在结果里标注 fallback，不假装两者等价。

【判据】
  · 遮挡面积必须**为 0** 才算通过（不是「小到可以接受」）
  · 每个可点目标都要记录 elementFromPoint 命中的是不是它自己
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from playwright.sync_api import sync_playwright  # noqa: E402

# 现场实际存在的生成类型（来自 /api/generation-types + /api/video-categories）
# key -> (显示名, 定位用 selector 策略)
GEN_TYPES = [
    ("t2v",       "文生视频",            "nav-item[data-mode='t2v'], .nav-model-item[data-mode='t2v']"),
    ("flf",       "首尾帧生成",          "nav-item[data-mode='flf'], .nav-model-item[data-mode='flf']"),
    ("r2v",       "参考图生成",          "nav-item[data-mode='r2v'], .nav-model-item[data-mode='r2v']"),
    ("longfilm",  "长片拼接",            "nav-item[data-mode='longfilm'], .nav-model-item[data-mode='longfilm']"),
    ("t2i",       "Z-IMAGE 文生图",      "nav-item[data-mode='t2i']"),
    ("t2i_qwen",  "Qwen-Image 2.1",      "nav-item[data-mode='t2i_qwen']"),
    ("t2i_moss",  "MOSS-Image",          "nav-item[data-mode='t2i_moss']"),
    ("t2i_h3image", "H3-Image",          "nav-item[data-mode='t2i_h3image']"),
    ("t2a",       "文生音乐",            "nav-item[data-mode='t2a']"),
]


# ---------------------------------------------------------------- 命中测试

HIT_JS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {found: false, reason: 'element-not-found'};
  const b = el.getBoundingClientRect();
  if (b.width === 0 || b.height === 0) return {found: true, reason: 'zero-size', rect: b};
  const x = Math.round(b.left + b.width / 2);
  const y = Math.round(b.top + b.height / 2);
  const cx = Math.min(innerWidth - 1, Math.max(0, x));
  const cy = Math.min(innerHeight - 1, Math.max(0, y));
  const t = document.elementFromPoint(cx, cy);
  return {
    found: true,
    rect: {top: Math.round(b.top), left: Math.round(b.left),
           w: Math.round(b.width), h: Math.round(b.height),
           bottom: Math.round(b.bottom), right: Math.round(b.right)},
    probe: {x: cx, y: cy},
    inViewport: b.top >= 0 && b.left >= 0 && b.bottom <= innerHeight && b.right <= innerWidth,
    partiallyInViewport: b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth,
    hitTag: t ? t.tagName : null,
    hitId: t ? t.id : null,
    hitCls: t ? String(t.className || '').slice(0, 70) : null,
    hitText: t ? String(t.textContent || '').trim().slice(0, 40) : null,
    hitIsSelf: t ? (t === el || el.contains(t)) : false,
    hitIsAncestor: t ? (el.contains(t) || t.contains(el)) : false,
  };
}"""

# 导航控件与「卡片正文 / 抽屉内容」的重叠面积
#
# 【三重陷阱 —— 上一轮连续两次量出假阳性，都是这里】
#  1) 祖先裁剪：getBoundingClientRect() 不报告被祖先 overflow:hidden
#     裁掉的部分。侧栏抽屉是 `position:fixed; top:44px; overflow:hidden` ——
#     抽屉滚动时导航项的 rect.top 会变负数（跑到 topbar 底下），
#     但那些部分被抽屉裁掉了，根本看不见。
#  2) 动画中途：抽屉有 `transition: transform .22s ease`。
#     JS 强制加类后立刻量，量到的是滑到一半的位置（left=-193）。
#  3) **兄弟层叠**：#sidebarMask 是**兄弟**元素（不是祖先），
#     它的 rect 天然覆盖整个视口 —— 但抽屉 z-index 940 > 遮罩 939，
#     遮罩**在抽屉下面**。拿 rect 相交算出「遮罩盖住抽屉 142840px²」，
#     完全是 z-index 语义没被尊重。
#
# 【权威判据 = elementFromPoint 采样】
# 「遮挡」在用户层面的定义是：**手指点在目标上，命中���不是目标**。
# 所以正确做法是：对目标的可见区域采样若干点，
# 用 elementFromPoint 看命中的到底是谁。
# 命中目标（或其后代）= 没被遮挡；命中导航控件 = 被它遮挡。
# 这与 Playwright 自己的 actionability 检查用的是同一套语义。
OVERLAP_JS = """(cfg) => {
  const rectOf = (el) => { const b = el.getBoundingClientRect();
    return {l:b.left, t:b.top, r:b.right, b:b.bottom, w:b.width, h:b.height}; };

  // 可见矩形 = 自身 rect ∩ 所有**裁剪祖先**的 rect
  const visibleRect = (el) => {
    let r = rectOf(el);
    let p = el.parentElement;
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (cs.overflow !== 'visible' || cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
        const pr = rectOf(p);
        r = { l: Math.max(r.l, pr.l), t: Math.max(r.t, pr.t),
              r: Math.min(r.r, pr.r), b: Math.min(r.b, pr.b), w: 0, h: 0 };
        r.w = Math.max(0, r.r - r.l);
        r.h = Math.max(0, r.b - r.t);
      }
      p = p.parentElement;
    }
    return r;
  };

  const ctrlEls = [];
  for (const sel of cfg.controls) {
    const c = document.querySelector(sel);
    if (!c) { ctrlEls.push({sel, el: null}); continue; }
    const cs = getComputedStyle(c);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') {
      ctrlEls.push({sel, el: null, invisible: true}); continue;
    }
    ctrlEls.push({sel, el: c});
  }

  const out = [];
  for (const ctl of ctrlEls) {
    if (!ctl.el) {
      out.push({control: ctl.sel, present: ctl.el !== undefined,
                visible: false, coveredArea: 0, covered: []});
      continue;
    }
    const c = ctl.el;
    const cr = visibleRect(c);
    const covered = [];
    for (const targetSel of cfg.targets) {
      for (const el of document.querySelectorAll(targetSel)) {
        const tr = visibleRect(el);
        if (tr.w <= 0 || tr.h <= 0) continue;   // 完全被裁掉 = 不可见
        // 在目标可见区域采样 9 个点，看命中的是谁
        let blocked = 0, sampled = 0;
        let hitInfo = null;
        for (let i = 1; i <= 3; i++) {
          for (let j = 1; j <= 3; j++) {
            const x = Math.round(tr.l + tr.w * i / 4);
            const y = Math.round(tr.t + tr.h * j / 4);
            if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
            sampled++;
            const t = document.elementFromPoint(x, y);
            if (!t) continue;
            if (t === el || el.contains(t)) continue;         // 命中自己 = 没被挡
            if (c === t || c.contains(t)) {                     // 命中这个导航控件 = 被挡
              blocked++;
              if (!hitInfo) hitInfo = {tag: t.tagName, id: t.id || null,
                                       cls: String(t.className||'').slice(0,60)};
            }
          }
        }
        if (blocked > 0 && sampled > 0) {
          covered.push({target: targetSel,
                        text: String(el.textContent || '').trim().slice(0, 40),
                        cls: String(el.className || '').slice(0, 50),
                        visible_rect: {l:Math.round(tr.l), t:Math.round(tr.t),
                                       w:Math.round(tr.w), h:Math.round(tr.h)},
                        blocked_samples: blocked, sampled_samples: sampled,
                        blocked_ratio: Math.round(blocked * 100 / sampled),
                        hit: hitInfo});
        }
      }
    }
    out.push({control: ctl.sel, present: true, visible: true,
              visible_rect: {l:Math.round(cr.l), t:Math.round(cr.t),
                             w:Math.round(cr.w), h:Math.round(cr.h)},
              covered: covered,
              coveredArea: covered.reduce((s, x) => s + x.blocked_samples, 0)});
  }
  return out;
}"""


def tap(page, selector, timeout=6000, force=False):
    """真实命中测试的点击。优先 tap（真触摸），降级 click（仍带命中测试）。

    失败时额外记录「是什么挡住的」—— 用 elementFromPoint 看点击点命中的
    到底是谁。这样「产品缺陷」和「脚本残留」能一眼分开。
    """
    try:
        page.tap(selector, timeout=timeout, force=force)
        return {"method": "tap", "ok": True}
    except Exception as e1:
        blocker = page.evaluate(
            """(s) => {
              const el = document.querySelector(s);
              if (!el) return {reason: 'element-not-found'};
              const b = el.getBoundingClientRect();
              if (b.width === 0 || b.height === 0) return {reason: 'zero-size'};
              const x = Math.round(b.left + b.width/2);
              const y = Math.round(b.top + b.height/2);
              if (x < 0 || y < 0 || x > innerWidth || y > innerHeight)
                return {reason: 'offscreen', x, y};
              const t = document.elementFromPoint(x, y);
              return {reason: 'hit-other',
                      probe: {x, y},
                      hitTag: t ? t.tagName : null,
                      hitId: t ? t.id : null,
                      hitCls: t ? String(t.className||'').slice(0,70) : null,
                      hitText: t ? String(t.textContent||'').trim().slice(0,40) : null,
                      bodyModalOpen: document.body.classList.contains('modal-open')
                                     || document.body.classList.contains('job-detail-open')};
            }""", selector)
        try:
            page.click(selector, timeout=timeout, force=force)
            return {"method": "click-fallback", "ok": True,
                    "tap_error": str(e1).split("\n")[0][:160],
                    "blocker": blocker}
        except Exception as e2:
            return {"method": "tap+click-failed", "ok": False,
                    "tap_error": str(e1).split("\n")[0][:200],
                    "click_error": str(e2).split("\n")[0][:200],
                    "blocker": blocker}


def close_all_drawers(page):
    page.evaluate("""() => {
      document.querySelectorAll('.sidebar,.panel,.stitch-panel')
        .forEach(e => e.classList.remove('open'));
      document.querySelectorAll('#sidebarMask,#panelMask')
        .forEach(e => e.classList.remove('open'));
    }""")


def reset_modals(page):
    """把残留的模态框全部关掉 —— 否则它会挡住后续步骤，
    让「上一个类型的失败」级联到「下一个类型也失败」。

    【为什么要这一步】上一轮跑 375px 全类型时：
      t2v 的详情检查用错了 modal id（#jobDetailModal，已停用），
      于是 modal 没被关掉 → body 带上 modal-open →
      下一个类型 t2i 的所有 tap 全部命中失败 →
      报告看起来像「t2i 也坏了」，其实是**脚本残留**。
    ⇒ 每个类型开始前必须清干净。
    """
    page.evaluate("""() => {
      document.querySelectorAll('.prompt-modal, .job-detail-modal, .lightbox')
        .forEach(m => { m.hidden = true; });
      document.body.classList.remove('modal-open', 'job-detail-open');
    }""")
    page.wait_for_timeout(200)


def drawer_state(page):
    return page.evaluate("""() => ({
      sidebarOpen: document.querySelector('#sidebar').classList.contains('open'),
      panelOpen: !!document.querySelector('#panel.open, #stitchPanel.open'),
      maskOpen: !!document.querySelector('#sidebarMask.open, #panelMask.open'),
      bodyModalOpen: document.body.classList.contains('modal-open'),
    })""")


def nav_item_present(page, mode):
    return page.evaluate(
        """(m) => {
          const el = document.querySelector(
            `.nav-item[data-mode="${m}"], .nav-model-item[data-mode="${m}"]`);
          if (!el) return {present: false};
          const b = el.getBoundingClientRect();
          return {present: true, visible: b.width > 0 && b.height > 0,
                  text: String(el.textContent || '').trim().slice(0, 40),
                  cls: String(el.className)};
        }""", mode)


def open_type_picker(page, shot_prefix, shot_dir):
    """打开「生成类型选择」（= 侧栏抽屉）。返回点击记录。"""
    rec = {}
    rec["hamburger_hit"] = page.evaluate(HIT_JS, "#navToggle")
    rec["hamburger_tap"] = tap(page, "#navToggle")
    page.wait_for_timeout(500)
    st = drawer_state(page)
    rec["sidebar_open"] = st["sidebarOpen"]
    rec["drawer_head_visible"] = page.evaluate(
        """() => { const h = document.querySelector('#navDrawerHead');
           if (!h) return false;
           const b = h.getBoundingClientRect();
           return b.width > 0 && b.height > 0; }""")
    rec["drawer_close_hit"] = page.evaluate(HIT_JS, "#navDrawerClose")
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-type-picker-open.png"))
    return rec


def back_to_type_picker(page, shot_prefix, shot_dir):
    """从「已进入某生成类型」的状态回到「生成类型选择」。"""
    rec = {}
    # 先看当前是否已经开着
    st = drawer_state(page)
    rec["pre_state"] = st
    if not st["sidebarOpen"]:
        rec["action"] = "open-sidebar"
        rec["hamburger_hit"] = page.evaluate(HIT_JS, "#navToggle")
        rec["tap"] = tap(page, "#navToggle")
        page.wait_for_timeout(500)
        st = drawer_state(page)
    rec["sidebar_open_after"] = st["sidebarOpen"]
    rec["drawer_title"] = page.evaluate(
        """() => { const t = document.querySelector('.nav-drawer-title');
           return t ? String(t.textContent || '').trim() : null; }""")
    rec["close_button_hit"] = page.evaluate(HIT_JS, "#navDrawerClose")
    if shot_prefix:
        page.screenshot(path=str(Path(shot_dir) / f"{shot_prefix}-back-to-type-picker.png"))
    return rec


def media_library_visibility(page):
    """生成媒体库（含左侧收藏入口）是否可见 / 被遮挡。"""
    jobs = page.evaluate(HIT_JS, "#jobsArea")
    grid = page.evaluate(HIT_JS, "#jobsGrid")
    cards = page.evaluate(
        """() => {
          const list = document.querySelectorAll('#jobsGrid .job-card[data-jid]');
          const vis = [];
          for (const el of list) {
            const b = el.getBoundingClientRect();
            const inView = b.bottom > 0 && b.top < innerHeight && b.right > 0 && b.left < innerWidth;
            const t = document.elementFromPoint(
              Math.round(Math.min(innerWidth-1, Math.max(0, b.left + b.width/2))),
              Math.round(Math.min(innerHeight-1, Math.max(0, b.top + b.height/2))));
            vis.push({jid: el.dataset.jid, inViewport: inView,
                      hitIsSelf: t ? (t === el || el.contains(t)) : false,
                      hitCls: t ? String(t.className||'').slice(0,50) : null});
          }
          return {total: list.length, visible: vis.filter(v=>v.inViewport).length,
                  clickable: vis.filter(v=>v.inViewport && v.hitIsSelf).length,
                  detail: vis.slice(0, 6)};
        }""")
    fav = page.evaluate(HIT_JS, "#navFavorites")
    return {"jobs_area": jobs, "jobs_grid": grid, "cards": cards,
            "favorites_nav_entry": fav}


def overlap_probe(page, targets, controls):
    return page.evaluate(OVERLAP_JS, {"targets": targets, "controls": controls})


def check_detail_modal(page):
    """卡片详情模态框：可打开 / 可关闭 / 内容完整（含真实 params）。

    【重要】真正的详情弹层是 **#detailModal**（浅色、字段完整），
    不是 #jobDetailModal —— 后者是 job-detail.js 里那套**已停用**的
    深色弹层（既有测试 tests/test_c6_detail_recommend.py 明确断言
    「点详情只能弹 detailModal，jobDetailModal 必须保持 hidden」）。
    上一轮我的检查脚本查错了 id，于是报「详情模态框打不开」——
    那是**测试脚本问题**，不是产品缺陷。截图证明弹层其实开得好好的。
    """
    rec = {}
    card = page.query_selector("#jobsGrid .job-card[data-jid]")
    if not card:
        rec["open"] = {"ok": False, "reason": "no-card"}
        return rec
    # 先确保没有别的模态框残留（否则会挡住）
    page.evaluate("""() => {
      document.querySelectorAll('.prompt-modal, .job-detail-modal, .lightbox')
        .forEach(m => { if (m.id !== 'detailModal') m.hidden = true; });
      document.body.classList.remove('modal-open', 'job-detail-open');
    }""")
    page.wait_for_timeout(200)

    rec["open"] = tap(page, "#jobsGrid .job-card[data-jid]")
    page.wait_for_timeout(900)
    rec["modal_visible"] = page.evaluate(
        """() => { const m = document.querySelector('#detailModal');
           if (!m) return false;
           if (m.hasAttribute('hidden')) return false;
           const b = m.getBoundingClientRect();
           const cs = getComputedStyle(m);
           return b.width > 0 && b.height > 0 && cs.display !== 'none'
                  && cs.visibility !== 'hidden' && cs.opacity !== '0'; }""")
    rec["legacy_modal_stays_hidden"] = page.evaluate(
        """() => { const m = document.querySelector('#jobDetailModal');
           if (!m) return true;
           return m.hasAttribute('hidden') || getComputedStyle(m).display === 'none'; }""")
    rec["body_text"] = page.evaluate(
        """() => { const b = document.querySelector('#detailModalBody');
           return b ? String(b.textContent || '').replace(/\\s+/g,' ').trim().slice(0, 1200) : null; }""")
    body = rec["body_text"] or ""
    rec["has_params_markers"] = {
        "steps": ("步数" in body) or ("Steps" in body) or ("steps" in body.lower()),
        "lora": "lora" in body.lower(),
        "scheduler": ("调度器" in body) or ("scheduler" in body.lower()),
        "seed": ("种子" in body) or ("seed" in body.lower()),
        "sampler": ("采样器" in body) or ("sampler" in body.lower()),
        "size": ("宽" in body and "高" in body),
        "prompt": ("提示词" in body),
    }
    rec["kv_count"] = page.evaluate(
        "() => document.querySelectorAll('#detailParams .detail-kv-row, #detailParams > *').length")
    rec["body_overflow"] = page.evaluate(
        """() => { const b = document.querySelector('#detailModalBody');
           if (!b) return null;
           return {scrollHeight: b.scrollHeight, clientHeight: b.clientHeight,
                   scrollable: b.scrollHeight > b.clientHeight,
                   maxScroll: b.scrollHeight - b.clientHeight}; }""")
    rec["preview_visible"] = page.evaluate(
        """() => { const p = document.querySelector('#detailPreview');
           if (!p || p.hasAttribute('hidden')) return false;
           const b = p.getBoundingClientRect();
           return b.width > 0 && b.height > 0; }""")
    rec["close_hit"] = page.evaluate(HIT_JS, "#detailModalClose")
    rec["close"] = tap(page, "#detailModalClose")
    page.wait_for_timeout(700)
    rec["modal_closed_after"] = not page.evaluate(
        """() => { const m = document.querySelector('#detailModal');
           return m && !m.hasAttribute('hidden') && getComputedStyle(m).display !== 'none'; }""")
    # 关不掉就用 Esc 兜底，保证不留下阻塞状态
    if not rec["modal_closed_after"]:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        rec["modal_closed_after_esc"] = not page.evaluate(
            """() => { const m = document.querySelector('#detailModal');
               return m && !m.hasAttribute('hidden') && getComputedStyle(m).display !== 'none'; }""")
    return rec


def check_filters(page):
    """筛选标签（全部/进行中/已完成/按生成类型/收藏）可点且生效。

    【注意】两套标签的属性不同，别搞混：
      · 状态标签 .jobs-tabs button[data-st]  → all/active/done/longfilm/failed/canceled/favorites
      · 生成类型标签 #genTypeTabs button[data-type] → ""（全部类型）/ t2v / flf / ...
    上一轮我把生成类型标签也按 data-st 找，结果一个都找不到 ——
    那是**脚本问题**，不是产品缺陷。
    """
    rec = {}
    for st in ["all", "active", "done", "favorites"]:
        sel = f'.jobs-tabs button[data-st="{st}"]'
        exists = page.evaluate("""(s) => !!document.querySelector(s)""", sel)
        if not exists:
            rec[st] = {"exists": False}
            continue
        hit = page.evaluate(HIT_JS, sel)
        t = tap(page, sel)
        page.wait_for_timeout(800)
        active = page.evaluate(
            """(s) => { const b = document.querySelector(`.jobs-tabs button[data-st="${s}"]`);
               return b && b.classList.contains('active'); }""", st)
        cards = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length")
        rec[st] = {"exists": True, "hit": hit, "tap": t,
                   "became_active": active, "card_count": cards}

    # 生成类型标签（#genTypeTabs 里的 button[data-type]）
    type_tabs = page.evaluate(
        """() => Array.from(document.querySelectorAll('#genTypeTabs button'))
                 .map(b => ({text: String(b.textContent||'').trim().slice(0,24),
                             type: b.dataset.type || '',
                             cls: String(b.className||'')}))""")
    rec["gen_type_tabs"] = type_tabs
    rec["gen_type_tab_count"] = len(type_tabs)
    if type_tabs:
        # 挑一个**非空**的 type（空串 = 全部类型，是默认值，点了看不出"生效"）
        target = next((t for t in type_tabs if t["type"]), None)
        if target:
            ty = target["type"]
            hit = page.evaluate(
                """(s) => {
                   const el = document.querySelector(`#genTypeTabs button[data-type="${s}"]`);
                   if (!el) return {found:false};
                   const b = el.getBoundingClientRect();
                   const x = Math.round(b.left+b.width/2), y = Math.round(b.top+b.height/2);
                   const t = document.elementFromPoint(x,y);
                   return {found:true, hitIsSelf: t ? (t===el||el.contains(t)) : false,
                           hitCls: t ? String(t.className||'').slice(0,60) : null,
                           rect:{w:Math.round(b.width),h:Math.round(b.height)}};
                 }""", ty)
            t = tap(page, f'#genTypeTabs button[data-type="{ty}"]')
            page.wait_for_timeout(900)
            became = page.evaluate(
                """(s) => { const b = document.querySelector(`#genTypeTabs button[data-type="${s}"]`);
                   return b && b.classList.contains('active'); }""", ty)
            cards = page.evaluate(
                "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length")
            rec["gen_type_tab_effect"] = {"type": ty, "hit": hit, "tap": t,
                                          "became_active": became, "card_count": cards}
            # 复位到「全部类型」
            tap(page, '#genTypeTabs button[data-type=""]')
            page.wait_for_timeout(500)

    # 每页数量选择器
    rec["page_size"] = page.evaluate(
        """() => { const s = document.querySelector('#pageSizeSelect');
           if (!s) return {exists:false};
           const b = s.getBoundingClientRect();
           const x = Math.round(b.left+b.width/2), y = Math.round(b.top+b.height/2);
           const t = document.elementFromPoint(x,y);
           return {exists:true, options: Array.from(s.options).map(o=>o.value),
                   value: s.value,
                   hitIsSelf: t ? (t===s||s.contains(t)) : false,
                   hitCls: t ? String(t.className||'').slice(0,60) : null,
                   hitTag: t ? t.tagName : null,
                   rect:{w:Math.round(b.width),h:Math.round(b.height)}}; }""")
    return rec


def run_one_type(page, mode, label, shot_dir, prefix, shot_enabled=True):
    """对单个生成类型跑完整验收。"""
    r = {"mode": mode, "label": label}
    close_all_drawers(page)
    reset_modals(page)
    page.wait_for_timeout(300)

    # --- 进入该生成类型 ---
    present = nav_item_present(page, mode)
    r["nav_item"] = present
    if not present.get("present"):
        r["skipped"] = "nav-item-not-in-dom"
        return r

    # 打开抽屉 -> 点该项
    open_type_picker(page, None if not shot_enabled else f"{prefix}-{mode}-01",
                     shot_dir)
    sel = f'.nav-item[data-mode="{mode}"], .nav-model-item[data-mode="{mode}"]'
    r["select_tap"] = tap(page, sel)
    page.wait_for_timeout(900)
    st = drawer_state(page)
    r["after_select_state"] = st
    r["after_select_active_mode"] = page.evaluate(
        """() => { const a = document.querySelector('.nav-item.active, .nav-model-item.active');
           return a ? {mode: a.dataset.mode || null,
                       text: String(a.textContent||'').trim().slice(0,40)} : null; }""")
    if shot_enabled:
        page.screenshot(path=str(Path(shot_dir) / f"{prefix}-{mode}-02-entered.png"))

    # --- 验收 1：仍能重新打开「生成类型选择」 ---
    r["A1_back_to_type_picker"] = back_to_type_picker(
        page, None if not shot_enabled else f"{prefix}-{mode}-03", shot_dir)
    r["A1_pass"] = bool(r["A1_back_to_type_picker"].get("sidebar_open_after"))

    # 关抽屉
    close_all_drawers(page)
    page.wait_for_timeout(350)

    # --- 验收 2：媒体库可见 + 收藏入口 ---
    r["A2_media_library"] = media_library_visibility(page)
    ml = r["A2_media_library"]
    r["A2_pass"] = bool(
        ml["jobs_area"].get("inViewport")
        and ml["cards"]["total"] > 0
        and ml["cards"]["visible"] > 0
        and ml["cards"]["clickable"] > 0)
    if shot_enabled:
        page.screenshot(path=str(Path(shot_dir) / f"{prefix}-{mode}-04-medialib.png"))

    # --- 验收 5：导航控件遮挡（对卡片正文 + 抽屉内容）---
    r["A5_overlap_closed"] = overlap_probe(
        page,
        targets=["#jobsGrid .job-card", "#jobsGrid .job-card img", "#jobsGrid .job-card video"],
        controls=["#navToggle", "#panelFab", "#sidebar", "#sidebarMask", "#panelMask"])
    r["A5_overlap_closed_total"] = sum(
        x.get("coveredArea", 0) for x in r["A5_overlap_closed"])
    # 抽屉打开时再测一次。
    # 【必须用真实 tap 打开，不能 JS 强制加类】
    #   抽屉有 `transition: transform .22s ease` ——
    #   JS 强制加类后立刻量，量到的是**动画中途**的位置（left=-193）。
    #   而且真实用户路径里，动态视频项点完会自动收抽屉，
    #   JS 强制加类测不到「点完类型后抽屉还开不开」这个真实场景。
    tap(page, "#navToggle")
    page.wait_for_timeout(900)   # 等动画结束
    drawer_open_real = drawer_state(page)["sidebarOpen"]
    r["A5_drawer_opened_by_real_tap"] = drawer_open_real
    r["A5_overlap_drawer_open"] = overlap_probe(
        page,
        targets=["#sidebar .nav-item", "#sidebar .nav-group", "#sidebar .nav-drawer-head"],
        controls=["#navToggle", "#panelFab", "#sidebarMask"])
    r["A5_overlap_drawer_total"] = sum(
        x.get("coveredArea", 0) for x in r["A5_overlap_drawer_open"])
    r["A5_pass"] = (r["A5_overlap_closed_total"] == 0
                    and r["A5_overlap_drawer_total"] == 0)
    close_all_drawers(page)
    page.wait_for_timeout(300)

    # --- 验收 3：详情模态框 ---
    r["A3_detail_modal"] = check_detail_modal(page)
    r["A3_pass"] = bool(r["A3_detail_modal"].get("modal_visible")
                        and r["A3_detail_modal"].get("modal_closed_after"))
    if shot_enabled:
        page.screenshot(path=str(Path(shot_dir) / f"{prefix}-{mode}-05-detail.png"))

    # --- 验收 4：筛选可点且生效 ---
    r["A4_filters"] = check_filters(page)
    f = r["A4_filters"]
    r["A4_pass"] = all(
        f.get(s, {}).get("became_active") is True
        for s in ["all", "active", "done", "favorites"] if f.get(s, {}).get("exists"))
    # 复位到「全部」
    tap(page, '.jobs-tabs button[data-st="all"]')
    page.wait_for_timeout(400)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=str(REPO / "evidence" / "mobile-nav" / "retest"))
    ap.add_argument("--width", type=int, default=375)
    ap.add_argument("--height", type=int, default=812)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shot", action="store_true", default=True)
    ap.add_argument("--no-shot", dest="shot", action="store_false")
    ap.add_argument("--modes", default="", help="逗号分隔；空=全部")
    ap.add_argument("--tag", default="retest")
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    modes = [m.strip() for m in args.modes.split(",") if m.strip()] or \
            [m[0] for m in GEN_TYPES]

    launch = {}
    if args.headed:
        # headed 需要 DISPLAY；调用方负责 xvfb-run，这里只是标记
        launch["headless"] = False
    else:
        launch["headless"] = True

    r = {"base": base, "viewport": [args.width, args.height],
         "headed": bool(args.headed), "browser": "chromium",
         "context": {"is_mobile": True, "has_touch": True, "device_scale_factor": 2},
         "types": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=launch["headless"],
                                     args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": args.width, "height": args.height},
                                  is_mobile=True, has_touch=True,
                                  device_scale_factor=2,
                                  user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                              "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                                              "Version/17.0 Mobile/15E148 Safari/604.1"))
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)[:200]))
        page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#jobsGrid .job-card[data-jid], #jobsGrid .job-card",
                               timeout=45000)
        page.wait_for_timeout(2500)

        # 记录首屏状态
        r["initial"] = drawer_state(page)
        r["initial_cards"] = page.evaluate(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length")
        r["page_errors"] = errors

        for mode, label, _sel in GEN_TYPES:
            if mode not in modes:
                continue
            print(f"[{args.tag}] {args.width}px  {mode:14s} {label}", flush=True)
            r["types"][mode] = run_one_type(
                page, mode, label, outdir,
                prefix=f"{args.tag}-{args.width}-{mode}", shot_enabled=args.shot)

        browser.close()

    (outdir / f"retest-{args.tag}-{args.width}.json").write_text(
        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")

    # 汇总
    print("\n===== 汇总 =====")
    for mode, v in r["types"].items():
        if "skipped" in v:
            print(f"  {mode:14s} SKIP ({v['skipped']})")
            continue
        marks = []
        for k, name in [("A1_pass", "1返回"), ("A2_pass", "2媒体库"),
                        ("A3_pass", "3详情"), ("A4_pass", "4筛选"),
                        ("A5_pass", "5遮挡")]:
            marks.append(f"{name}={'OK' if v.get(k) else 'FAIL'}")
        ov = (v.get("A5_overlap_closed_total", 0), v.get("A5_overlap_drawer_total", 0))
        print(f"  {mode:14s} " + " ".join(marks) + f"  遮挡面积={ov}")
    print("\nOUT:", outdir)


if __name__ == "__main__":
    main()
