# -*- coding: utf-8 -*-
"""修两份真机扫描台（Python + Node）的三处判据缺陷。

【为什么必须修 —— 这三处都是**测试脚本自己的错**，不是产品缺陷】

  F1 参数抽屉有两个：#panel（普通创作）与 #stitchPanel（长片拼接页）
     —— 两者 HTML 上都带 .panel 类，窄屏下都是底部抽屉。
     脚本只查 #panel ⇒ 长片拼接那一支永远报「参数抽屉没打开」。
     实测（375×667 选 longfilm）：点 FAB 后打开的是 #stitchPanel
     （stitchOpen=true / maskOpen=true / 汉堡命中自身=true）—— 功能是好的。

  F2 矮视口下媒体库卡片在**首屏之外**
     实测 320×568：媒体库头部占 558px（状态标签 + 类型标签 + 筛选器 + 每页），
     卡片网格从第 691px 才开始 ⇒ 首屏必然看不到卡片，需要滚动 647px。
     这不是缺陷（用户本来就要滚），但判据必须是「**滚动后**能点到」。

  F3 横屏 932×430 是**平板档**（>700px）⇒ 侧栏常驻、没有汉堡。
     脚本假设「所有视口都是手机档、都能点汉堡开抽屉」⇒ 全部判 FAIL。
     实测：932px 下 hamburgerVisible=false / sidebarPosition=static ——
     布局是对的，脚本的假设错了。

【本脚本对两份扫描台做同样的修正】
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = REPO / "scripts" / "realscan_mobile_matrix.py"
JS = REPO / "scripts" / "realscan_webkit.cjs"


def patch_py(s: str) -> str:
    # --- F1a: STATE_JS 认两个参数面板 ---
    old = """  const panel = document.querySelector('#panel');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));"""
    new = """  // 【F1】参数抽屉有两个：#panel（普通创作）与 #stitchPanel（长片拼接页）
  //   —— 两者 HTML 上都带 .panel 类，窄屏下都是底部抽屉。
  //   只查 #panel 会漏掉长片拼接那一支（实测它功能是好的）。
  const panel = document.querySelector('#panel.open, #stitchPanel.open')
              || document.querySelector('#panel');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));"""
    assert old in s, "F1a anchor"
    s = s.replace(old, new, 1)

    # --- F1b: 抽屉打开判定认两个 ---
    old = """    st = page.evaluate(STATE_JS)
    exits["fab_open"] = st["panelOpen"]"""
    new = """    st = page.evaluate(STATE_JS)
    exits["fab_open"] = st["panelOpen"]   # STATE_JS 已含 #stitchPanel（F1a）"""
    assert old in s, "F1b anchor"
    s = s.replace(old, new, 1)

    # --- F2: 媒体库滚动后再判可点 ---
    old = """    st = page.evaluate(STATE_JS)
    r["media_library"] = {"total": st["cardsTotal"], "inViewport": st["cardsInViewport"],
                           "clickable": st["cardsClickable"]}"""
    new = """    st = page.evaluate(STATE_JS)
    # 【F2】矮视口（320×568）下媒体库头部占 558px，卡片网格从第 691px 才开始
    #   ⇒ 首屏必然看不到卡片，需要滚动 647px。
    #   这不是缺陷（用户本来就要滚），但判据必须是「**滚动后**能点到」。
    need_scroll = page.evaluate(
        \"\"\"() => {
          const area = document.querySelector('#jobsArea');
          const card = document.querySelector('#jobsGrid .job-card[data-jid]');
          if (!area || !card) return 0;
          const ar = area.getBoundingClientRect();
          const cr = card.getBoundingClientRect();
          return Math.max(0, Math.round(cr.top - ar.top));
        }\"\"\")
    r["media_library_scroll_needed"] = need_scroll
    if st["cardsClickable"] == 0 and need_scroll > 0:
        page.evaluate(
            \"\"\"() => {
              const area = document.querySelector('#jobsArea');
              const card = document.querySelector('#jobsGrid .job-card[data-jid]');
              if (!area || !card) return;
              area.scrollTop += card.getBoundingClientRect().top
                                - area.getBoundingClientRect().top;
            }\"\"\")
        page.wait_for_timeout(500)
        st = page.evaluate(STATE_JS)
    r["media_library"] = {"total": st["cardsTotal"], "inViewport": st["cardsInViewport"],
                           "clickable": st["cardsClickable"],
                           "scrollNeeded": need_scroll}"""
    assert old in s, "F2 anchor"
    s = s.replace(old, new, 1)

    # --- F3: 平板/桌面档（宽度 > 700）没有汉堡 —— 改用侧栏里的导航项 ---
    old = """    # --- 步骤 1：打开类型选择 ---
    reset(page)
    ok, method, blocker = tap(page, "#navToggle")
    r["open_picker"] = {"ok": ok, "method": method, "blocker": blocker}
    page.wait_for_timeout(600)
    st = page.evaluate(STATE_JS)
    r["picker_open"] = st["sidebarOpen"]
    if not st["sidebarOpen"]:
        r["fatal"] = "打不开类型选择（汉堡点不动）"
        return r"""
    new = """    # --- 步骤 1：打开类型选择 ---
    reset(page)
    # 【F3】宽度 > 700px 是**平板/桌面档**：侧栏常驻、没有汉堡。
    #   那一档根本没有「打开类型选择」这个动作 —— 侧栏本来就在那儿。
    #   ⇒ 手机档才需要点汉堡；平板档直接用侧栏里的导航项。
    is_phone = page.evaluate(
        \"\"\"() => window.BREAKPOINTS
             ? innerWidth <= window.BREAKPOINTS.phone
             : matchMedia('(max-width: 700px)').matches\"\"\")
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
        # 平板档：侧栏常驻，直接确认侧栏里的类型项可点
        r["open_picker"] = {"ok": True, "method": "sidebar-always-visible"}
        r["picker_open"] = page.evaluate(
            \"\"\"() => { const sb = document.querySelector('#sidebar');
               if (!sb) return false;
               const b = sb.getBoundingClientRect();
               return b.width > 0 && b.height > 0 && getComputedStyle(sb).position !== 'fixed'; }\"\"\")"""
    assert old in s, "F3 anchor"
    s = s.replace(old, new, 1)

    # --- F3b: 「回类型选择」在平板档的等价动作（选另一个类型）---
    old = """    # --- 步骤 3：回到类型选择（汉堡）---
    ok, method, blocker = tap(page, "#navToggle")
    r["back_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
    page.wait_for_timeout(700)
    st = page.evaluate(STATE_JS)
    r["reached_picker"] = st["sidebarOpen"]"""
    new = """    # --- 步骤 3：回到类型选择（手机档=汉堡；平板档=侧栏常驻，天然可达）---
    if r.get("is_phone_layout", True):
        ok, method, blocker = tap(page, "#navToggle")
        r["back_via_hamburger"] = {"ok": ok, "method": method, "blocker": blocker}
        page.wait_for_timeout(700)
        st = page.evaluate(STATE_JS)
        r["reached_picker"] = st["sidebarOpen"]
    else:
        # 平板档：侧栏常驻 ⇒ 「回到类型选择」= 侧栏里的类型项仍可点
        r["back_via_hamburger"] = {"ok": True, "method": "sidebar-always-visible"}
        r["reached_picker"] = page.evaluate(
            \"\"\"() => { const el = document.querySelector('#sidebar .nav-item[data-mode]');
               if (!el) return false;
               const b = el.getBoundingClientRect();
               if (b.width === 0 || b.height === 0) return false;
               const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
               const t = document.elementFromPoint(x, y);
               return !!(t && (t === el || el.contains(t))); }\"\"\")"""
    assert old in s, "F3b anchor"
    s = s.replace(old, new, 1)

    # --- F3c: 遮挡采样时平板档不该去点汉堡 ---
    old = """    reset(page)
    r["overlap_closed"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#jobsGrid .job-card[data-jid]", "#navToggle", "#panelFab"]})
    # 抽屉打开时
    tap(page, "#navToggle")
    page.wait_for_timeout(700)
    r["overlap_drawer_open"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#sidebar .nav-item", "#navToggle", "#panelFab"]})
    reset(page)"""
    new = """    reset(page)
    r["overlap_closed"] = page.evaluate(HIT_SAMPLE_JS, {
        "selectors": ["#jobsGrid .job-card[data-jid]", "#navToggle", "#panelFab"]})
    # 抽屉打开时（手机档才有抽屉）
    if r.get("is_phone_layout", True):
        tap(page, "#navToggle")
        page.wait_for_timeout(700)
        r["overlap_drawer_open"] = page.evaluate(HIT_SAMPLE_JS, {
            "selectors": ["#sidebar .nav-item", "#navToggle", "#panelFab"]})
    else:
        # 平板档：侧栏常驻，直接测它与卡片/FAB 的遮挡
        r["overlap_drawer_open"] = page.evaluate(HIT_SAMPLE_JS, {
            "selectors": ["#sidebar .nav-item", "#navToggle", "#panelFab"]})
    reset(page)"""
    assert old in s, "F3c anchor"
    s = s.replace(old, new, 1)

    return s


def patch_js(s: str) -> str:
    """Node 版做同样的修正。"""
    # F1a: STATE_JS 认两个参数面板
    old = """  const panel = document.querySelector('#panel');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));"""
    new = """  // 【F1】参数抽屉有两个：#panel（普通创作）与 #stitchPanel（长片拼接页）
  const panel = document.querySelector('#panel.open, #stitchPanel.open')
              || document.querySelector('#panel');
  const cards = Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'));"""
    assert old in s, "JS F1a anchor"
    s = s.replace(old, new, 1)

    # F2: 媒体库滚动
    old = """  st = await page.evaluate(STATE_JS);
  r.media_library = { total: st.cardsTotal, inViewport: st.cardsInViewport,
                      clickable: st.cardsClickable };"""
    new = """  st = await page.evaluate(STATE_JS);
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
                      clickable: st.cardsClickable, scrollNeeded: needScroll };"""
    assert old in s, "JS F2 anchor"
    s = s.replace(old, new, 1)

    # F3: 手机档 vs 平板档
    old = """  const open = await tap(page, '#navToggle');
  r.open_picker = open;
  await page.waitForTimeout(600);
  let st = await page.evaluate(STATE_JS);
  r.picker_open = st.sidebarOpen;
  if (!st.sidebarOpen) { r.fatal = '打不开类型选择（汉堡点不动）'; return r; }"""
    new = """  // 【F3】宽度 > 700px 是平板/桌面档：侧栏常驻、没有汉堡 ⇒ 没有「打开类型选择」这个动作
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
  }"""
    assert old in s, "JS F3 anchor"
    s = s.replace(old, new, 1)

    # F3b: 回类型选择
    old = """  // 回类型选择（汉堡）
  r.back_via_hamburger = await tap(page, '#navToggle');
  await page.waitForTimeout(700);
  st = await page.evaluate(STATE_JS);
  r.reached_picker = st.sidebarOpen;"""
    new = """  // 回类型选择（手机档=汉堡；平板档=侧栏常驻，天然可达）
  if (r.is_phone_layout !== false) {
    r.back_via_hamburger = await tap(page, '#navToggle');
    await page.waitForTimeout(700);
    st = await page.evaluate(STATE_JS);
    r.reached_picker = st.sidebarOpen;
  } else {
    r.back_via_hamburger = { ok: true, method: 'sidebar-always-visible' };
    r.reached_picker = await page.evaluate(`(() => {
      const el = document.querySelector('#sidebar .nav-item[data-mode]');
      if (!el) return false;
      const b = el.getBoundingClientRect();
      if (!b.width || !b.height) return false;
      const x = Math.round(b.left + b.width/2), y = Math.round(b.top + b.height/2);
      const t = document.elementFromPoint(x, y);
      return !!(t && (t === el || el.contains(t))); })()`);
  }"""
    assert old in s, "JS F3b anchor"
    s = s.replace(old, new, 1)

    # F3c: 遮挡采样
    old = """  await tap(page, '#navToggle');
  await page.waitForTimeout(700);
  r.overlap_drawer_open = await page.evaluate(HIT_SAMPLE_JS, {
    selectors: ['#sidebar .nav-item', '#navToggle', '#panelFab'] });"""
    new = """  if (r.is_phone_layout !== false) {
    await tap(page, '#navToggle');
    await page.waitForTimeout(700);
  }
  r.overlap_drawer_open = await page.evaluate(HIT_SAMPLE_JS, {
    selectors: ['#sidebar .nav-item', '#navToggle', '#panelFab'] });"""
    assert old in s, "JS F3c anchor"
    s = s.replace(old, new, 1)

    return s


def main():
    ok = True
    for path, fn in ((PY, patch_py), (JS, patch_js)):
        src = path.read_text(encoding="utf-8")
        try:
            new = fn(src)
        except AssertionError as e:
            print(f"❌ {path.name}: anchor 未找到 -> {e}")
            ok = False
            continue
        path.write_text(new, encoding="utf-8")
        print(f"✅ {path.name} 已修")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
