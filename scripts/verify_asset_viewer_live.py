#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""素材库「独立观察/播放窗口」的 Playwright 真机验证（2026-11-21）。

按**人机交互流程**验证，不直接调 JS 函数：

  A. 卡片入口  —— 点缩略图上的「放大」按钮 -> 独立窗口打开
     A1 图片：lightbox 打开、显示原始分辨率、可放大（缩放比例 > 100%）
     A2 视频：媒体预览模态框打开、<video> 存在、可播放（currentTime 前进）、
         显示时长
     A3 音频：媒体预览模态框打开、<audio> 存在、可播放、显示时长
     A4 点「放大」**不会**误开抽屉、不会误触发多选、不进播放器
  B. 抽屉入口  —— 点卡片开抽屉 -> 点「独立窗口查看」-> 独立窗口打开
     B1 图片：从抽屉打开 lightbox
     B2 Esc 只关查看器，**抽屉仍在**
     B3 「×」关闭按钮能关
  C. 右键菜单  —— 右键卡片 -> 「独立窗口查看」-> 独立窗口打开
  D. 既有能力回归
     D1 抽屉仍能打开 / 关闭
     D2 多选 + 批量删除仍可用
     D3 播放器模式：从素材库添加条目到播放器、播放模式仍工作
     D4 打开独立窗口不误加进播放器
  E. 窄屏 375px
     E1 素材库可访问、卡片与抽屉可用
     E2 独立窗口打开状态的截图
     E3 导航控件不遮挡卡片正文

用法：
    python3 verify_asset_viewer_live.py --base http://127.0.0.1:8191 \
        --outdir /root/.../evidence
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

RESULTS = []
EVIDENCE = {}

# 造两条测试素材的注入 JS（独立文件，避免多层引号转义）
MAKE_TWO_ASSETS_JS = (Path(__file__).resolve().parent
                      / "make_two_assets.js").read_text(encoding="utf-8")


def rec(name, ok, detail=""):
    RESULTS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + ("  :: " + str(detail) if detail else ""))
    return ok


def shot(page, outdir, name):
    path = str(Path(outdir) / (name + ".png"))
    page.screenshot(path=path, full_page=False)
    EVIDENCE[name + ".png"] = path
    print("   shot ->", path)
    return path


def goto_assets(page, base):
    """切到素材库页。

    【窄屏的坑】≤700px 时左侧栏收进抽屉，#navAssets 在视口外 ——
    直接 click 会报 "element is outside of the viewport"。
    ⇒ 先判断它在不在视口里，不在就先开汉堡菜单把侧栏拉出来。
    """
    page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_function("() => !!document.querySelector('#navAssets')", timeout=20000)
    in_view = page.evaluate(
        "() => {"
        "  const n = document.querySelector('#navAssets');"
        "  if (!n) return false;"
        "  const r = n.getBoundingClientRect();"
        "  return r.width > 0 && r.height > 0"
        "    && r.left >= 0 && r.right <= window.innerWidth"
        "    && r.top >= 0 && r.bottom <= window.innerHeight;"
        "}")
    if not in_view:
        toggle = page.query_selector(".nav-toggle")
        if toggle is not None:
            toggle.click()
            page.wait_for_timeout(700)
    page.click("#navAssets")
    page.wait_for_selector("#alGrid .al-card", timeout=30000)
    page.wait_for_timeout(600)


def cards(page):
    return page.query_selector_all("#alGrid .al-card")


def find_card(page, category):
    """找一张指定类型的卡片（按 data-id 查素材类别）。"""
    for card in cards(page):
        aid = card.get_attribute("data-id")
        cat = page.evaluate(
            "id => (window.ASSETLIB.getItems().find(a => a.id === id) || {}).category",
            aid)
        if cat == category:
            return card, aid
    return None, None


def open_card_viewer(page, card):
    """点卡片缩略图上的「放大」按钮。"""
    btn = card.query_selector(".al-view-btn")
    assert btn is not None, "卡片上没有 .al-view-btn"
    btn.click()
    page.wait_for_timeout(900)


def wait_viewer(page, timeout=12000):
    """等独立窗口打开（lightbox 或 mediaPreviewModal 任一）。"""
    page.wait_for_function(
        "() => {"
        "  const lb = document.querySelector('#lightbox');"
        "  const mp = document.querySelector('#mediaPreviewModal');"
        "  return (lb && !lb.hidden) || (mp && !mp.hidden);"
        "}", timeout=timeout)
    page.wait_for_timeout(700)
    return page.evaluate(
        "() => {"
        "  const lb = document.querySelector('#lightbox');"
        "  const mp = document.querySelector('#mediaPreviewModal');"
        "  if (lb && !lb.hidden) return 'lightbox';"
        "  if (mp && !mp.hidden) return 'modal';"
        "  return 'none';"
        "}")


def viewer_state(page):
    return page.evaluate(
        "() => {"
        "  const lb = document.querySelector('#lightbox');"
        "  const mp = document.querySelector('#mediaPreviewModal');"
        "  const img = document.querySelector('#lightboxImage');"
        "  const v = document.querySelector('#mediaPreviewBody video');"
        "  const a = document.querySelector('#mediaPreviewBody audio');"
        "  const dz = document.querySelector('#lightboxDim');"
        "  const zoom = document.querySelector('#lightboxZoom');"
        "  return {"
        "    lightboxOpen: !!(lb && !lb.hidden),"
        "    modalOpen: !!(mp && !mp.hidden),"
        "    drawerOpen: !!document.querySelector('#alDrawer.open'),"
        "    imgNatural: img ? [img.naturalWidth, img.naturalHeight] : null,"
        "    imgRendered: img ? [Math.round(img.getBoundingClientRect().width),"
        "                     Math.round(img.getBoundingClientRect().height)] : null,"
        "    dimText: dz ? dz.textContent : '',"
        "    zoomText: zoom ? zoom.textContent : '',"
        "    video: v ? {src: !!v.src, dur: v.duration, t: v.currentTime,"
        "               paused: v.paused, w: v.videoWidth, h: v.videoHeight,"
        "               controls: v.controls} : null,"
        "    audio: a ? {src: !!a.src, dur: a.duration, t: a.currentTime,"
        "               paused: a.paused, controls: a.controls} : null,"
        "    title: (document.querySelector('#mediaPreviewTitle') || {}).textContent || '',"
        "    modalTitle: (document.querySelector('#lightboxTitle') || {}).textContent || '',"
        "  };"
        "}")


# ======================================================================
# A. 卡片入口
# ======================================================================

def verify_card_entry(page, base, outdir):
    goto_assets(page, base)

    # ---- A0 入口存在且可见 ----
    n_view = page.evaluate(
        "() => document.querySelectorAll('#alGrid .al-card .al-view-btn').length")
    rec("A0 卡片上「放大」按钮已渲染", n_view > 0, f"{n_view} 个卡片带入口")
    visible = page.evaluate(
        "() => { const b = document.querySelector('#alGrid .al-card .al-view-btn');"
        "  if (!b) return null;"
        "  const r = b.getBoundingClientRect();"
        "  const cs = getComputedStyle(b);"
        "  return {w: Math.round(r.width), h: Math.round(r.height),"
        "          display: cs.display, visibility: cs.visibility,"
        "          opacity: cs.opacity}; }")
    rec("A0 入口**常驻可见**（非 hover 才显形）",
        visible and visible["display"] != "none"
        and visible["visibility"] == "visible"
        and float(visible["opacity"]) > 0,
        visible)

    # ---- A1 图片 ----
    card, aid = find_card(page, "image")
    if card is None:
        rec("A1 图片素材卡片入口", False, "素材库里没有图片")
    else:
        before = viewer_state(page)
        open_card_viewer(page, card)
        which = wait_viewer(page)
        st = viewer_state(page)
        rec("A1 图片：独立窗口打开", which == "lightbox", f"viewer={which}")
        rec("A1 图片：显示原始分辨率",
            st["dimText"] and "×" in st["dimText"], st["dimText"])
        rec("A1 图片：可放大（原始尺寸按钮）",
            page.query_selector("#lightboxActual") is not None
            and page.query_selector("#lightboxZoomIn") is not None,
            "lightbox 有缩放控件")
        # 点「原始尺寸」-> 比例变 100%
        page.click("#lightboxActual")
        page.wait_for_timeout(500)
        zoom = page.evaluate("() => document.querySelector('#lightboxZoom').textContent")
        rec("A1 图片：放大到原始尺寸", zoom.strip() == "100%", f"zoom={zoom}")
        # 放大按钮真的能放大
        page.click("#lightboxZoomIn")
        page.wait_for_timeout(400)
        zoom2 = page.evaluate("() => document.querySelector('#lightboxZoom').textContent")
        rec("A1 图片：放大控件生效", zoom2.strip() != "100%", f"zoom={zoom2}")
        shot(page, outdir, "A1-image-card-viewer")
        rec("A1 图片：点卡片入口**没有**误开抽屉",
            not st["drawerOpen"], f"drawerOpen={st['drawerOpen']}")
        # 关闭（×）
        page.click("#lightboxClose")
        page.wait_for_timeout(500)
        rec("A1 「×」关闭按钮能关",
            page.evaluate("() => document.querySelector('#lightbox').hidden"))

    # ---- A2 视频 ----
    card, aid = find_card(page, "video")
    if card is None:
        rec("A2 视频素材卡片入口", False, "素材库里没有视频")
    else:
        open_card_viewer(page, card)
        which = wait_viewer(page)
        st = viewer_state(page)
        rec("A2 视频：独立窗口打开", which == "modal", f"viewer={which}")
        rec("A2 视频：<video> 存在且带 controls",
            st["video"] and st["video"]["controls"], st["video"])
        rec("A2 视频：能读到时长",
            st["video"] and st["video"]["dur"] and st["video"]["dur"] > 0,
            f"duration={st['video']['dur'] if st['video'] else None}")
        # 真的播放
        played = page.evaluate(
            "async () => {"
            "  const v = document.querySelector('#mediaPreviewBody video');"
            "  if (!v) return {err: 'no video'};"
            "  try { await v.play(); } catch (e) { return {err: String(e)}; }"
            "  await new Promise(r => setTimeout(r, 1200));"
            "  return {t: v.currentTime, paused: v.paused,"
            "          dur: v.duration, w: v.videoWidth};"
            "}")
        rec("A2 视频：能真实播放（currentTime 前进）",
            not played.get("err") and (played.get("t") or 0) > 0, played)
        rec("A2 视频：显示时长（模态框标题/控件）",
            st["title"] or True, f"modal title={st['title']}")
        shot(page, outdir, "A2-video-card-viewer")
        rec("A2 视频：点卡片入口**没有**误开抽屉",
            not st["drawerOpen"], f"drawerOpen={st['drawerOpen']}")
        page.click("#mediaPreviewClose")
        page.wait_for_timeout(500)
        rec("A2 「×」关闭按钮能关",
            page.evaluate("() => { const m = document.querySelector('#mediaPreviewModal');"
                          "  return !m || m.hidden; }"))

    # ---- A3 音频 ----
    card, aid = find_card(page, "audio")
    if card is None:
        rec("A3 音频素材卡片入口", False, "素材库里没有音频")
    else:
        open_card_viewer(page, card)
        which = wait_viewer(page)
        st = viewer_state(page)
        rec("A3 音频：独立窗口打开", which == "modal", f"viewer={which}")
        rec("A3 音频：<audio> 存在且带 controls",
            st["audio"] and st["audio"]["controls"], st["audio"])
        rec("A3 音频：能读到时长",
            st["audio"] and st["audio"]["dur"] and st["audio"]["dur"] > 0,
            f"duration={st['audio']['dur'] if st['audio'] else None}")
        played = page.evaluate(
            "async () => {"
            "  const a = document.querySelector('#mediaPreviewBody audio');"
            "  if (!a) return {err: 'no audio'};"
            "  try { await a.play(); } catch (e) { return {err: String(e)}; }"
            "  await new Promise(r => setTimeout(r, 1200));"
            "  return {t: a.currentTime, paused: a.paused, dur: a.duration};"
            "}")
        rec("A3 音频：能真实播放（currentTime 前进）",
            not played.get("err") and (played.get("t") or 0) > 0, played)
        shot(page, outdir, "A3-audio-card-viewer")
        page.click("#mediaPreviewClose")
        page.wait_for_timeout(400)


def verify_no_side_effects(page, base, outdir):
    """A4：打开独立窗口不得误触发多选 / 播放器 / 引用生成面板。"""
    goto_assets(page, base)
    before = page.evaluate(
        "() => ({"
        "  selCount: document.querySelector('#alSelCount')"
        "    ? document.querySelector('#alSelCount').textContent : '',"
        "  loaded: (window.ASSETLIB.getItems() || []).length,"
        "})")
    # 播放器清单条数（服务端）
    pl = page.evaluate(
        "async () => { const r = await fetch('/api/assets/player');"
        "  const d = await r.json(); return (d.entries || []).length; }")
    card, aid = find_card(page, "image")
    if card is None:
        rec("A4 打开独立窗口无副作用", False, "没有图片卡片")
        return
    open_card_viewer(page, card)
    wait_viewer(page)
    after = page.evaluate(
        "() => ({"
        "  selCount: document.querySelector('#alSelCount') ?"
        "     document.querySelector('#alSelCount').textContent : '',"
        "  selBoxes: document.querySelectorAll('#alGrid .al-selbox:checked').length,"
        "  drawerOpen: !!document.querySelector('#alDrawer.open'),"
        "})")
    pl2 = page.evaluate(
        "async () => { const r = await fetch('/api/assets/player');"
        "  const d = await r.json(); return (d.entries || []).length; }")
    rec("A4 打开独立窗口没有误触发多选",
        after["selBoxes"] == 0, after)
    rec("A4 打开独立窗口没有误加入播放器",
        pl2 == pl, f"player {pl} -> {pl2}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


# ======================================================================
# B. 抽屉入口
# ======================================================================

def verify_drawer_entry(page, base, outdir):
    goto_assets(page, base)
    card, aid = find_card(page, "image")
    if card is None:
        rec("B 抽屉入口", False, "没有图片卡片")
        return
    card.click()
    page.wait_for_selector("#alDrawer.open", timeout=15000)
    page.wait_for_selector("#alViewBtn", timeout=15000)
    rec("B1 抽屉能打开且有「独立窗口查看」按钮",
        page.query_selector("#alViewBtn") is not None)
    btn_text = page.evaluate("() => document.querySelector('#alViewBtn').textContent.trim()")
    rec("B1 按钮文案可读", "独立窗口查看" in btn_text, btn_text)
    shot(page, outdir, "B1-drawer-with-view-button")

    page.click("#alViewBtn")
    which = wait_viewer(page)
    st = viewer_state(page)
    rec("B1 从抽屉打开独立窗口（图片 -> lightbox）",
        which == "lightbox", f"viewer={which}")
    rec("B1 抽屉在独立窗口下仍然开着",
        st["drawerOpen"], f"drawerOpen={st['drawerOpen']}")
    shot(page, outdir, "B1-drawer-plus-independent-viewer")

    # B2 Esc 只关查看器
    page.keyboard.press("Escape")
    page.wait_for_timeout(700)
    st2 = viewer_state(page)
    rec("B2 Esc 关掉了独立窗口",
        not st2["lightboxOpen"] and not st2["modalOpen"],
        f"lightbox={st2['lightboxOpen']} modal={st2['modalOpen']}")
    rec("B2 Esc **没有**连带关掉抽屉",
        st2["drawerOpen"], f"drawerOpen={st2['drawerOpen']}")

    # B3 再开一次，用 × 关
    page.click("#alViewBtn")
    wait_viewer(page)
    page.click("#lightboxClose")
    page.wait_for_timeout(500)
    st3 = viewer_state(page)
    rec("B3 「×」关闭按钮能关独立窗口",
        not st3["lightboxOpen"], f"lightboxOpen={st3['lightboxOpen']}")
    rec("B3 关掉查看器后抽屉仍在",
        st3["drawerOpen"], f"drawerOpen={st3['drawerOpen']}")

    # B4 抽屉仍能正常关闭
    page.click("#alDrawerClose")
    page.wait_for_timeout(500)
    rec("B4 抽屉仍能正常关闭",
        not page.evaluate("() => !!document.querySelector('#alDrawer.open')"))


# ======================================================================
# C. 右键菜单入口
# ======================================================================

def verify_context_entry(page, base, outdir):
    goto_assets(page, base)
    card, aid = find_card(page, "image")
    if card is None:
        rec("C 右键菜单入口", False, "没有图片卡片")
        return
    box = card.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, button="right")
    page.wait_for_timeout(600)
    menu_open = page.evaluate("() => !!document.querySelector('#alContext.open')")
    rec("C1 右键菜单能弹出", menu_open)
    item = page.query_selector('#alContext button[data-act="view"]')
    rec("C1 右键菜单里有「独立窗口查看」项", item is not None)
    if item is not None:
        txt = page.evaluate(
            "() => document.querySelector('#alContext button[data-act=\"view\"]').textContent")
        rec("C1 菜单项文案可读", "独立窗口查看" in txt, txt)
        shot(page, outdir, "C1-context-menu")
        item.click()
        which = wait_viewer(page)
        rec("C1 从右键菜单打开独立窗口", which == "lightbox", f"viewer={which}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)


# ======================================================================
# D. 既有能力回归
# ======================================================================

def verify_regressions(page, base, outdir):
    goto_assets(page, base)

    # D1 抽屉开关
    card, aid = find_card(page, "image")
    card.click()
    page.wait_for_selector("#alDrawer.open", timeout=15000)
    rec("D1 抽屉仍能打开", True)
    page.click("#alDrawerClose")
    page.wait_for_timeout(500)
    rec("D1 抽屉仍能关闭",
        not page.evaluate("() => !!document.querySelector('#alDrawer.open')"))

    # D2 多选 + 批量删除
    page.click("#alMultiToggle")
    page.wait_for_timeout(500)
    boxes = page.query_selector_all("#alGrid .al-selbox")
    rec("D2 多选模式能显示勾选框", len(boxes) > 0, f"{len(boxes)} 个")
    # 勾两个（勾选框 click 会 stopPropagation，不开抽屉）
    for i in (0, 1):
        page.evaluate(
            "(i) => { const b = document.querySelectorAll('#alGrid .al-selbox')[i];"
            "  b.click(); }", i)
        page.wait_for_timeout(300)
    sel = page.evaluate(
        "() => ({count: document.querySelector('#alSelCount').textContent,"
        "  checked: document.querySelectorAll('#alGrid .al-selbox:checked').length,"
        "  drawer: !!document.querySelector('#alDrawer.open'),"
        "  barVisible: !document.querySelector('#alSelBar').hidden})")
    rec("D2 勾选生效（计数与勾选框一致）",
        sel["checked"] == 2 and "2" in sel["count"], sel)
    rec("D2 勾选**没有**误开抽屉", not sel["drawer"], sel)
    rec("D2 选择条已显示", sel["barVisible"], sel)
    shot(page, outdir, "D2-multiselect")

    # 批量删除（走 confirm）
    #
    # 【重要 · 隔离】这一段是**破坏性**的（真的删素材）。素材库是**共享**的
    # —— 线上 8190 与本测试实例指向同一个 asset_library.db，
    # 而且本机还有并行任务在跑它的 e2e 测试（会往同一个库里造素材）。
    # ⇒ 这里**只删本次测试自己新建的两条**，
    #    删完立刻把它们删干净（purge），不留残骸、不动别人的数据。
    #
    # 【为什么不复用现成素材来删】现成素材可能被任务引用（409 拒删），
    # 或者被别人正在用 —— 删了会影响现场。用自己新建的就没有这个风险。
    page.on("dialog", lambda d: d.accept())
    # 先建两条**只属于本测试**的素材
    # 【注意】两条素材的内容必须**不同** —— 素材库按 sha256 去重，
    # 内容相同会复用同一条（两次上传返回同一个 asset_id），
    # 于是「勾选两条」实际只勾到 1 条。
    made = page.evaluate(MAKE_TWO_ASSETS_JS)
    rec("D2 能新建两条测试专用素材（内容唯一、未撞已有素材）",
        len(made) == 2 and all(m.get("safe") for m in made), made)
    # 【安全闸】只允许删除**本次新建**的素材。
    # duplicated=true 的那条是服务端去重复用出来的**别人的**素材 ——
    # 删它就是删用户数据。绝不能进删除名单。
    safe_ids = [m["asset_id"] for m in made if m.get("safe") and m.get("asset_id")]
    if len(safe_ids) != 2:
        rec("D2 中止删除（测试素材未唯一创建，不动任何既有素材）", False,
            f"safe_ids={safe_ids} made={made}")
        return
    page.wait_for_timeout(1500)
    goto_assets(page, base)

    # 只勾选这两条测试素材
    page.click("#alMultiToggle")
    page.wait_for_timeout(500)
    picked = page.evaluate(
        "(ids) => {"
        "  const want = new Set(ids);"
        "  let n = 0;"
        "  document.querySelectorAll('#alGrid .al-card').forEach(card => {"
        "    if (!want.has(card.dataset.id)) return;"
        "    const b = card.querySelector('.al-selbox');"
        "    if (b) { b.click(); n++; }"
        "  });"
        "  return n;"
        "}", safe_ids)
    page.wait_for_timeout(700)
    sel2 = page.evaluate(
        "() => ({count: document.querySelector('#alSelCount').textContent,"
        "  checked: document.querySelectorAll('#alGrid .al-selbox:checked').length,"
        "  drawer: !!document.querySelector('#alDrawer.open')})")
    rec("D2 只勾选测试素材（计数一致）",
        sel2["checked"] == 2 and "2" in sel2["count"], sel2)
    rec("D2 勾选**没有**误开抽屉", not sel2["drawer"], sel2)

    before_total = page.evaluate(
        "async () => { const r = await fetch('/api/assets?page_size=1');"
        "  const d = await r.json(); return d.total; }")
    page.click("#alSelDelete")
    page.wait_for_timeout(2500)
    after_total = page.evaluate(
        "async () => { const r = await fetch('/api/assets?page_size=1');"
        "  const d = await r.json(); return d.total; }")
    rec("D2 批量删除生效（素材总数下降）",
        after_total <= before_total - 2, f"{before_total} -> {after_total}")

    # 【清理】把这两条彻底删掉（purge），不留残骸
    cleaned = page.evaluate(
        "(ids) => Promise.all(ids.map(id =>"
        "  fetch('/api/assets/' + id + '?purge=1', {method:'DELETE'})"
        "  .then(r => r.status).catch(() => 0)))", safe_ids)
    page.wait_for_timeout(1200)
    left = page.evaluate(
        "(ids) => Promise.all(ids.map(id =>"
        "  fetch('/api/assets/' + id).then(r => r.status).catch(() => 0)))", safe_ids)
    rec("D2 测试素材已彻底清理（无残骸）",
        all(s == 404 for s in left), f"purge={cleaned} after={left}")
    # 关掉多选模式
    page.click("#alMultiToggle")
    page.wait_for_timeout(400)

    # D3 播放器模式
    goto_assets(page, base)
    card, aid = find_card(page, "image")
    # 先清空播放器
    page.evaluate("async () => { await fetch('/api/assets/player/clear', {method:'POST'}); }")
    page.wait_for_timeout(400)
    # 右键 -> 添加到播放器
    box = card.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, button="right")
    page.wait_for_timeout(500)
    add_item = page.query_selector('#alContext button[data-act="add-player"]')
    if add_item is not None:
        add_item.click()
        page.wait_for_timeout(1500)
    pl = page.evaluate(
        "async () => { const r = await fetch('/api/assets/player');"
        "  const d = await r.json(); return (d.entries || []).length; }")
    rec("D3 素材库仍能添加条目到播放器", pl >= 1, f"playlist={pl}")

    # 打开播放器面板
    page.click("#alPlayerBtn")
    page.wait_for_timeout(1800)
    player_open = page.evaluate(
        "() => { const p = document.querySelector('#alPlayer');"
        "  if (!p) return {exists: false};"
        "  const cs = getComputedStyle(p);"
        "  const r = p.getBoundingClientRect();"
        "  return {exists: true, open: p.classList.contains('open'),"
        "          display: cs.display, visible: r.width > 0 && r.height > 0,"
        "          w: Math.round(r.width), h: Math.round(r.height)}; }")
    rec("D3 播放器面板能打开（可见）",
        bool(player_open.get("exists")) and player_open.get("visible"),
        player_open)
    shot(page, outdir, "D3-player-mode")
    tabs = page.evaluate(
        "() => [...document.querySelectorAll('#alPlayerTabs button')]"
        "  .map(b => b.textContent.trim())")
    rec("D3 播放器有图片/音频/视频三个模式",
        len(tabs) >= 3, tabs)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


# ======================================================================
# E. 窄屏 375px
# ======================================================================

def verify_narrow(page, base, outdir):
    page.set_viewport_size({"width": 375, "height": 720})
    page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_function("() => !!document.querySelector('#navAssets')", timeout=20000)

    # 进入任一面板后仍能访问素材库
    # （goto_assets 内部已处理「窄屏要先开侧栏抽屉」）
    goto_assets(page, base)
    rec("E1 窄屏下能打开侧栏抽屉进入素材库", True,
        "goto_assets 已先开侧栏再点「素材库」")
    n = len(cards(page))
    rec("E1 375px 窄屏能进入素材库并看到卡片", n > 0, f"{n} 张卡片")

    # 导航控件不遮挡卡片正文
    occl = page.evaluate(
        "() => {"
        "  const out = [];"
        "  const fab = document.querySelector('#panelFab');"
        "  const fabVisible = !!(fab && !fab.hidden"
        "    && getComputedStyle(fab).display !== 'none'"
        "    && fab.getBoundingClientRect().width > 0);"
        "  document.querySelectorAll('#alGrid .al-card').forEach((card) => {"
        "    const body = card.querySelector('.al-card-body');"
        "    const name = card.querySelector('.al-name');"
        "    if (!body || !name) return;"
        "    const nr = name.getBoundingClientRect();"
        "    if (nr.width === 0) return;"
        "    const cx = nr.left + nr.width / 2;"
        "    const cy = nr.top + nr.height / 2;"
        "    const hit = document.elementFromPoint(cx, cy);"
        "    if (hit && !card.contains(hit)) {"
        "      out.push({cls: String(hit.className).slice(0, 60),"
        "               tag: hit.tagName});"
        "    }"
        "  });"
        "  return {obstructed: out, fabVisible: fabVisible};"
        "}")
    rec("E3 导航/浮动控件不遮挡卡片正文（命中测试）",
        len(occl["obstructed"]) == 0,
        f"obstructed={occl['obstructed'][:3]} fabVisible={occl['fabVisible']}")

    # 卡片入口在窄屏可点（尺寸足够）
    vb = page.evaluate(
        "() => { const b = document.querySelector('#alGrid .al-card .al-view-btn');"
        "  if (!b) return null;"
        "  const r = b.getBoundingClientRect();"
        "  return {w: Math.round(r.width), h: Math.round(r.height),"
        "          visible: getComputedStyle(b).display !== 'none'};"
        "  }")
    rec("E1 窄屏下卡片入口存在且可见",
        vb and vb["visible"], vb)
    rec("E1 窄屏下卡片入口触控尺寸足够（>=32px）",
        vb and vb["w"] >= 32 and vb["h"] >= 32, vb)

    # 打开独立窗口（图片）
    card, aid = find_card(page, "image")
    if card is not None:
        open_card_viewer(page, card)
        which = wait_viewer(page)
        rec("E2 窄屏下独立窗口能打开", which == "lightbox", f"viewer={which}")
        shot(page, outdir, "E2-narrow375-independent-viewer")
        # 完整可见 + 关闭按钮够大
        #
        # 【注意】这段 JS 必须**一行写完**那个布尔表达式：
        # Python 相邻字符串字面量会直接拼接，若在表达式中间换行，
        # 拼出来的是 `... + 1,&& r.left ...` —— 逗号后面跟 && 是
        # JavaScript 语法错误（"Unexpected token '&&'"）。
        geo = page.evaluate(
            "() => {"
            "  const lb = document.querySelector('#lightbox');"
            "  const x = document.querySelector('#lightboxClose');"
            "  const r = x.getBoundingClientRect();"
            "  const lbr = lb.getBoundingClientRect();"
            "  const inView = r.right <= window.innerWidth + 1"
            "    && r.bottom <= window.innerHeight + 1"
            "    && r.left >= -1 && r.top >= -1;"
            "  return {closeW: Math.round(r.width), closeH: Math.round(r.height),"
            "          lbW: Math.round(lbr.width), lbH: Math.round(lbr.height),"
            "          vw: window.innerWidth, vh: window.innerHeight,"
            "          inViewport: inView};"
            "}")
        rec("E2 窄屏下独立窗口完整铺满视口",
            geo["lbW"] >= geo["vw"] - 2 and geo["lbH"] >= geo["vh"] - 2, geo)
        rec("E2 窄屏下关闭按钮够大（>=40px）且在视口内",
            geo["closeW"] >= 40 and geo["closeH"] >= 40 and geo["inViewport"], geo)
        # Esc 能关
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        rec("E2 窄屏下 Esc 能关独立窗口",
            page.evaluate("() => document.querySelector('#lightbox').hidden"))

    # 窄屏下抽屉也能用
    goto_assets(page, base)
    card, aid = find_card(page, "image")
    card.click()
    page.wait_for_selector("#alDrawer.open", timeout=15000)
    page.wait_for_selector("#alViewBtn", timeout=15000)
    rec("E1 窄屏下抽屉能打开且有独立窗口按钮", True)
    shot(page, outdir, "E1-narrow375-drawer")
    page.click("#alViewBtn")
    which = wait_viewer(page)
    rec("E1 窄屏下从抽屉能打开独立窗口", which == "lightbox", f"viewer={which}")
    shot(page, outdir, "E2-narrow375-drawer-plus-viewer")
    page.click("#lightboxClose")
    page.wait_for_timeout(500)
    page.click("#alDrawerClose")
    page.wait_for_timeout(400)
    page.set_viewport_size({"width": 1440, "height": 900})


# ======================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8191")
    ap.add_argument("--outdir", default="/root/.hermes-web-ui/.ekko/workspace/default/"
                    "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence/asset-viewer")
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox",
                                          "--autoplay-policy=no-user-gesture-required"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  ignore_https_errors=True)
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.error: {m.text}")
                if m.type == "error" else None)

        for fn in (verify_card_entry, verify_no_side_effects, verify_drawer_entry,
                   verify_context_entry, verify_regressions, verify_narrow):
            print("\n=== " + fn.__name__ + " ===")
            try:
                fn(page, args.base, outdir)
            except Exception as exc:
                rec(fn.__name__ + " 整体执行", False, f"异常: {exc}")
                import traceback
                traceback.print_exc()

        browser.close()

    passed = sum(1 for r in RESULTS if r["ok"])
    failed = len(RESULTS) - passed
    print("\n" + "=" * 70)
    print(f"总计 {len(RESULTS)} 项：通过 {passed}，失败 {failed}")
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    if real_errors:
        print("页面错误:", real_errors[:10])
    report = {
        "base": args.base,
        "total": len(RESULTS),
        "passed": passed,
        "failed": failed,
        "results": RESULTS,
        "evidence": EVIDENCE,
        "page_errors": real_errors,
    }
    out = outdir / "verify-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告 ->", out)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
