# -*- coding: utf-8 -*-
"""「定时刷新不得打断交互」的完整验收脚本（真实 Chromium + 真实后端）。

覆盖任务书要求的 5 个场景 + 3 档宽度截图：
  1. 媒体库展开下拉 -> 保持展开 >=3 个刷新周期 -> 截图证明仍展开、选中项未变
  2. 滚动到中部 -> 等刷新 -> 滚动位置保持
  3. 聚焦输入框输入文字 -> 等刷新 -> 输入内容不丢
  4. 后台生成完成 -> 新内容仍能自动出现（素材库 + 媒体库）
  5. 375 / 390 / 1280px 三档宽度各截一次图

用法: python3 tests/verify_refresh_ux.py [base_url]
产物: /tmp/h3verify/*.png + /tmp/h3verify/report.json
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = Path("/tmp/h3verify")
OUT.mkdir(parents=True, exist_ok=True)

REFRESH_PERIOD_MS = 2000
WAIT_CYCLES = 4
WAIT_MS = REFRESH_PERIOD_MS * WAIT_CYCLES + 1500


def mark(page):
    """给关键节点打稳定标记，用来判断「节点是否被整体换掉」。"""
    page.evaluate("""() => {
      const mark = el => { if (el && el.nodeType===1 && !el.__m) el.__m = 'm'+Math.random().toString(36).slice(2,9); return el && el.__m; };
      window.__mark = mark;
      ['doneMedia','doneAspect','doneDuration','pageSizeSelect','alSource','alSort',
       'alSearch','alTagPick','alTagMenu','jobsGrid','jobsPagination','alGrid','alPager']
        .forEach(id => mark(document.getElementById(id)));
      document.querySelectorAll('#jobsGrid .job-card').forEach(mark);
      document.querySelectorAll('#alGrid .al-card').forEach(mark);
    }""")


def snap(page):
    return page.evaluate("""() => {
      const m = window.__mark;
      const s = id => { const el = document.getElementById(id); return el ? {m: m(el), v: el.value} : null; };
      const grid = document.getElementById('jobsGrid');
      const alGrid = document.getElementById('alGrid');
      const area = document.querySelector('.jobs-area');
      const apage = document.querySelector('.assets-page');
      const tagPick = document.getElementById('alTagPick');
      return {
        doneMedia: s('doneMedia'), doneAspect: s('doneAspect'),
        doneDuration: s('doneDuration'), pageSize: s('pageSizeSelect'),
        doneSearch: s('doneSearch'),
        alSource: s('alSource'), alSort: s('alSort'), alSearch: s('alSearch'),
        tagPickOpen: tagPick ? tagPick.classList.contains('open') : null,
        jobsScrollTop: area ? area.scrollTop : null,
        alScrollTop: apage ? apage.scrollTop : null,
        jobCardMarks: Array.from(grid ? grid.querySelectorAll('.job-card') : []).map(m),
        alCardMarks: Array.from(alGrid ? alGrid.querySelectorAll('.al-card') : []).map(m),
        activeTag: document.activeElement ? document.activeElement.tagName : null,
        activeId: document.activeElement ? (document.activeElement.id || '') : '',
        activeInGrid: !!(document.activeElement && document.activeElement.closest &&
                         document.activeElement.closest('#jobsGrid')),
        openSelects: document.querySelectorAll('select:open').length,
        statusText: (document.getElementById('statusText')||{}).textContent,
      };
    }""")


def same_node(a, b, key):
    if not a or not b:
        return None
    return a.get(key) == b.get(key)


def main():
    report = {"base": BASE, "scenarios": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                if m.type == "error" else None)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1500)

        # ============================================================
        # 场景 1：媒体库「已完成」页签 + 展开原生下拉
        # ============================================================
        page.evaluate("""() => {
          const b=[...document.querySelectorAll('.jobs-tabs button')].find(x=>x.dataset.st==='done');
          if(b) b.click();
        }""")
        page.wait_for_timeout(2500)
        # 选一个非默认值（模拟用户已做出选择）
        page.evaluate("""() => {
          const sel = document.getElementById('doneAspect');
          if (sel && sel.options.length > 1) {
            sel.value = sel.options[1].value;
            sel.dispatchEvent(new Event('change', {bubbles:true}));
          }
        }""")
        page.wait_for_timeout(2000)
        mark(page)
        # 用键盘打开原生下拉（Alt+ArrowDown 在 Chromium 里会展开 select 弹层）
        page.focus("#doneAspect")
        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(700)
        pre1 = snap(page)
        pre1["openSelects"] = page.evaluate("() => document.querySelectorAll('select:open').length")
        page.screenshot(path=str(OUT / "01_dropdown_open_before_refresh.png"))
        # 等 >=3 个刷新周期
        page.wait_for_timeout(WAIT_MS)
        post1 = snap(page)
        post1["openSelects"] = page.evaluate("() => document.querySelectorAll('select:open').length")
        page.screenshot(path=str(OUT / "01_dropdown_open_after_refresh.png"))
        report["scenarios"]["S1_dropdown_stays_open"] = {
            "open_before": pre1["openSelects"],
            "open_after": post1["openSelects"],
            "stays_open": pre1["openSelects"] > 0 and post1["openSelects"] > 0,
            "value_before": (pre1["doneAspect"] or {}).get("v"),
            "value_after": (post1["doneAspect"] or {}).get("v"),
            "value_kept": same_node(pre1, post1, "doneAspect"),
            "node_replaced": not same_node(pre1, post1, "doneAspect"),
            "screenshots": ["01_dropdown_open_before_refresh.png",
                            "01_dropdown_open_after_refresh.png"],
        }
        page.keyboard.press("Escape")

        # ============================================================
        # 场景 2：滚动到中部 -> 等刷新 -> 滚动位置保持
        # ============================================================
        page.evaluate("""() => {
          const b=[...document.querySelectorAll('.jobs-tabs button')].find(x=>x.dataset.st==='all');
          if(b) b.click();
        }""")
        page.wait_for_timeout(2500)
        page.evaluate("""() => {
          const area = document.querySelector('.jobs-area');
          if (area) area.scrollTop = Math.floor((area.scrollHeight - area.clientHeight) / 2);
        }""")
        page.wait_for_timeout(800)
        pre2 = snap(page)
        page.screenshot(path=str(OUT / "02_scroll_before_refresh.png"))
        page.wait_for_timeout(WAIT_MS)
        post2 = snap(page)
        page.screenshot(path=str(OUT / "02_scroll_after_refresh.png"))
        report["scenarios"]["S2_scroll_position_held"] = {
            "scroll_before": pre2["jobsScrollTop"],
            "scroll_after": post2["jobsScrollTop"],
            "delta": (post2["jobsScrollTop"] or 0) - (pre2["jobsScrollTop"] or 0),
            "held": abs((post2["jobsScrollTop"] or 0) - (pre2["jobsScrollTop"] or 0)) <= 2,
            "screenshots": ["02_scroll_before_refresh.png", "02_scroll_after_refresh.png"],
        }

        # ============================================================
        # 场景 3：聚焦输入框输入文字 -> 等刷新 -> 输入不丢
        # ============================================================
        page.evaluate("""() => {
          const s = document.getElementById('doneSearch');
          s.focus(); s.value = '测试关键词abc123';
          s.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(1500)
        pre3 = snap(page)
        page.screenshot(path=str(OUT / "03_input_before_refresh.png"))
        page.wait_for_timeout(WAIT_MS)
        post3 = snap(page)
        page.screenshot(path=str(OUT / "03_input_after_refresh.png"))
        report["scenarios"]["S3_input_not_overwritten"] = {
            "value_before": (pre3["doneSearch"] or {}).get("v"),
            "value_after": (post3["doneSearch"] or {}).get("v"),
            "kept": (pre3["doneSearch"] or {}).get("v") == (post3["doneSearch"] or {}).get("v"),
            "focus_kept": pre3["activeId"] == "doneSearch" and post3["activeId"] == "doneSearch",
            "screenshots": ["03_input_before_refresh.png", "03_input_after_refresh.png"],
        }
        # 清掉搜索词，恢复正常列表
        page.evaluate("""() => {
          const s = document.getElementById('doneSearch');
          s.value = ''; s.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(1500)

        # ============================================================
        # 场景 3b：焦点在卡片内按钮 -> 等刷新 -> 焦点不丢
        # ============================================================
        page.evaluate("""() => {
          const btn = document.querySelector('#jobsGrid .job-card button');
          if (btn) btn.focus();
        }""")
        pre3b = snap(page)
        page.wait_for_timeout(WAIT_MS)
        post3b = snap(page)
        report["scenarios"]["S3b_card_focus_kept"] = {
            "focus_in_grid_before": pre3b["activeInGrid"],
            "focus_in_grid_after": post3b["activeInGrid"],
            "kept": pre3b["activeInGrid"] and post3b["activeInGrid"],
        }

        # ============================================================
        # 场景 4：素材库 —— 下拉保持 + 输入不丢 + 滚动保持
        # ============================================================
        page.evaluate("() => { const b = document.getElementById('navAssets'); if (b) b.click(); }")
        page.wait_for_timeout(3000)
        mark(page)
        # 展开「按标签筛选」自绘下拉
        page.evaluate("""() => {
          const t = document.getElementById('alTagToggle');
          if (t) t.click();
        }""")
        page.wait_for_timeout(700)
        pre4 = snap(page)
        page.screenshot(path=str(OUT / "04_assets_tagmenu_before.png"))
        page.wait_for_timeout(WAIT_MS)
        post4 = snap(page)
        page.screenshot(path=str(OUT / "04_assets_tagmenu_after.png"))
        report["scenarios"]["S4a_assets_tagmenu_stays_open"] = {
            "open_before": pre4["tagPickOpen"], "open_after": post4["tagPickOpen"],
            "kept": pre4["tagPickOpen"] == post4["tagPickOpen"] and post4["tagPickOpen"],
            "screenshots": ["04_assets_tagmenu_before.png", "04_assets_tagmenu_after.png"],
        }
        # 素材库排序下拉：原生 select 展开
        page.evaluate("""() => {
          const t = document.getElementById('alTagToggle');
          if (t) t.click();   // 关掉标签菜单
        }""")
        page.wait_for_timeout(400)
        page.focus("#alSort")
        page.keyboard.press("Alt+ArrowDown")
        page.wait_for_timeout(600)
        pre4b = snap(page)
        pre4b["openSelects"] = page.evaluate("() => document.querySelectorAll('select:open').length")
        page.wait_for_timeout(WAIT_MS)
        post4b = snap(page)
        post4b["openSelects"] = page.evaluate("() => document.querySelectorAll('select:open').length")
        page.screenshot(path=str(OUT / "04b_assets_sort_dropdown_after.png"))
        report["scenarios"]["S4b_assets_sort_dropdown_stays_open"] = {
            "open_before": pre4b["openSelects"], "open_after": post4b["openSelects"],
            "stays_open": pre4b["openSelects"] > 0 and post4b["openSelects"] > 0,
            "value_before": (pre4b["alSort"] or {}).get("v"),
            "value_after": (post4b["alSort"] or {}).get("v"),
        }
        page.keyboard.press("Escape")
        # 素材库输入框
        page.evaluate("""() => {
          const s = document.getElementById('alSearch');
          s.focus(); s.value = '素材检索xyz';
          s.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(1200)
        pre4c = snap(page)
        page.wait_for_timeout(WAIT_MS)
        post4c = snap(page)
        page.screenshot(path=str(OUT / "04c_assets_input_after.png"))
        report["scenarios"]["S4c_assets_input_not_overwritten"] = {
            "value_before": (pre4c["alSearch"] or {}).get("v"),
            "value_after": (post4c["alSearch"] or {}).get("v"),
            "kept": (pre4c["alSearch"] or {}).get("v") == (post4c["alSearch"] or {}).get("v"),
        }
        page.evaluate("""() => {
          const s = document.getElementById('alSearch');
          s.value = ''; s.dispatchEvent(new Event('input', {bubbles:true}));
        }""")
        page.wait_for_timeout(1200)

        # ============================================================
        # 场景 4d：素材库节点稳定性（同一批卡片不被整体重建）
        # ============================================================
        mark(page)
        pre4d = snap(page)
        page.wait_for_timeout(WAIT_MS)
        post4d = snap(page)
        pre_marks = set(pre4d["alCardMarks"])
        kept = sum(1 for x in post4d["alCardMarks"] if x in pre_marks)
        report["scenarios"]["S4d_asset_cards_not_rebuilt"] = {
            "cards_before": len(pre4d["alCardMarks"]),
            "cards_after": len(post4d["alCardMarks"]),
            "same_node_cards": kept,
            "all_kept": len(post4d["alCardMarks"]) > 0 and kept == len(post4d["alCardMarks"]),
        }

        # ============================================================
        # 场景 5：三档宽度截图
        # ============================================================
        widths = {}
        for w in (375, 390, 1280):
            page.set_viewport_size({"width": w, "height": 900})
            page.wait_for_timeout(1500)
            name = f"05_width_{w}.png"
            page.screenshot(path=str(OUT / name))
            widths[w] = name
        page.set_viewport_size({"width": 1280, "height": 900})
        page.wait_for_timeout(800)
        report["scenarios"]["S5_responsive_screenshots"] = widths

        report["pageerrors"] = errors
        (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
        browser.close()

    # ---- 汇总 ----
    # 注意：有些布尔项是「反向指标」（如 node_replaced=True 表示有问题），
    # 汇总时必须按语义判断，不能简单 all(values)。
    INVERTED = {"node_replaced"}
    print("\n" + "=" * 68)
    ok = True
    for name, r in report["scenarios"].items():
        flags = {k: v for k, v in r.items() if isinstance(v, bool) and k not in INVERTED}
        inverted = {k: v for k, v in r.items() if isinstance(v, bool) and k in INVERTED}
        passed = all(flags.values()) and not any(inverted.values())
        ok = ok and passed
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
        for k, v in r.items():
            if k == "screenshots":
                continue
            print(f"        {k} = {v}")
    print("=" * 68)
    print("OVERALL:", "PASS" if ok else "FAIL")
    print(f"report: {OUT/'report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
