# -*- coding: utf-8 -*-
"""Playwright 真实浏览器验收：左侧栏类别折叠 + 独立上下滚动（2026-11-13）。

【为什么单独写一个脚本】
需求验收要求「真实浏览器渲染验证」，并且要在多档宽度下截图 + 记录 console 报错。
仓库已有的 tests/test_responsive_mobile.py 测的是通用响应式，
不是本次的「折叠 + 滚动」—— 单独一个脚本，断言更贴需求。

【覆盖】
  * 折叠（顶层分组 7 个 + 视频类别 4 个）
      - 点分组标题 -> 入口项隐藏/显示，aria-expanded 翻转
      - 点视频类别的折叠开关 -> 模型列表隐藏/显示
      - 折叠状态持久化到 localStorage，刷新后保持
      - 折叠不影响选中态与路由：点入口项仍能切 mode
      - 折叠态下点入口项 -> 自动展开该分组（选中态要看得见）
  * 滚动
      - .nav-scroll 存在且可滚动（scrollHeight > clientHeight）
      - 矮视口（375/390 高）下滚到底能看到最下面几项（系统 / 释放显存）
      - 不被 overflow:hidden 截断
  * 三档宽度截图（375 / 390 / 1280）+ console 0 错误 + 无横向溢出

用法: python3 tests/verify_sidebar_fold_browser.py [base_url] [evidence_dir]
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8231"
EVIDENCE = Path(sys.argv[2] if len(sys.argv) > 2 else
                 "/root/.hermes-web-ui/.ekko/workspace/default/"
                 "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)

WIDTHS = (375, 390, 1280)
FOLD_KEY = "h3studio.sidebarFold"


def run():
    from playwright.sync_api import sync_playwright

    failures = []
    console_errors = []

    def check(name, cond, detail=""):
        status = "OK  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f"  {detail}" if detail else ""))
        if not cond:
            failures.append(f"{name} {detail}")

    def wait_sidebar(page, timeout_ms=15000):
        """等侧栏 DOM 就位。

        【为什么需要】页面 reload 后到 DOM 重建完成之间有个窗口，
        此时 evaluate 里 document.querySelector('#sidebar') 会拿到 null
        —— 表现为「Cannot read properties of null」，是**脚本竞态**，
        不是产品缺陷（实测同一份代码重跑即通过）。
        """
        try:
            page.wait_for_function(
                "() => !!document.getElementById('sidebar')"
                " && !!document.getElementById('navScroll')",
                timeout=timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(300)

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # ---------------------------------------------------------- 首屏
        print("\n[1] 首屏加载（1280），清掉折叠状态")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.evaluate("k => localStorage.removeItem(k)", FOLD_KEY)
        page.reload(wait_until="domcontentloaded")
        wait_sidebar(page)

        groups = page.eval_on_selector_all(
            ".nav-group[data-nav-key]", "els => els.map(e => e.dataset.navKey)")
        check("7 个顶层分组标题可折叠",
              len(groups) == 7, f"got={groups}")

        cats = page.eval_on_selector_all(
            "#navVideoCategories .nav-cat-label",
            "els => els.map(e => e.textContent.trim())")
        check("视频四个类别仍在", len(cats) == 4, f"got={cats}")

        toggles = page.eval_on_selector_all(
            "#navVideoCategories .nav-cat-toggle",
            "els => els.map(e => e.dataset.category)")
        check("每个视频类别都有折叠开关", len(toggles) == 4, f"got={toggles}")

        # ---------------------------------------------------------- 顶层折叠
        print("\n[2] 顶层分组折叠")
        before = page.evaluate(
            "() => document.querySelector('.nav-item[data-mode=\"t2i\"]').offsetParent !== null")
        check("折叠前「Z-IMAGE 文生图」可见", before)

        page.click('.nav-group[data-nav-key="image"]')
        page.wait_for_timeout(600)
        st = page.evaluate("""() => {
          const g = document.querySelector('.nav-group[data-nav-key="image"]');
          return {
            expanded: g.getAttribute('aria-expanded'),
            itemVisible: document.querySelector('.nav-item[data-mode="t2i"]').offsetParent !== null,
            stored: localStorage.getItem('h3studio.sidebarFold'),
          };
        }""")
        check("点分组标题 -> aria-expanded 变 false",
              st["expanded"] == "false", f"expanded={st['expanded']}")
        check("折叠后该分组入口项隐藏", not st["itemVisible"])
        check("折叠状态已写入 localStorage",
              "image" in (st["stored"] or ""), f"stored={st['stored']}")

        # 持久化：刷新后仍折叠
        page.reload(wait_until="domcontentloaded")
        wait_sidebar(page)
        st2 = page.evaluate("""() => ({
          expanded: document.querySelector('.nav-group[data-nav-key="image"]')
                     .getAttribute('aria-expanded'),
          itemVisible: document.querySelector('.nav-item[data-mode="t2i"]').offsetParent !== null,
        })""")
        check("刷新后分组仍是折叠态（持久化）",
              st2["expanded"] == "false", f"expanded={st2['expanded']}")
        check("刷新后入口项仍隐藏", not st2["itemVisible"])

        # 折叠态下点入口项（用 JS 点，因为 display:none 不可点）-> 自动展开
        page.evaluate(
            """() => {
              const it = document.querySelector('.nav-item[data-mode="t2i"]');
              it.click();
            }""")
        page.wait_for_timeout(900)
        st3 = page.evaluate("""() => ({
          expanded: document.querySelector('.nav-group[data-nav-key="image"]')
                     .getAttribute('aria-expanded'),
          mode: window.__h3State ? window.__h3State.mode : null,
          active: [...document.querySelectorAll('.nav-item.active')]
                    .map(e => e.textContent.trim()),
        })""")
        check("折叠态下点入口项 -> 自动展开该分组",
              st3["expanded"] == "true", f"expanded={st3['expanded']}")
        check("点入口项仍能切 mode（路由未破坏）",
              st3["mode"] == "t2i", f"mode={st3['mode']}")
        check("选中态正确", st3["active"] == ["Z-IMAGE 文生图"],
              f"active={st3['active']}")

        # 再折回去，保持后面截图的默认形态可控
        page.click('.nav-group[data-nav-key="image"]')
        page.wait_for_timeout(500)

        # ---------------------------------------------------------- 视频类别折叠
        print("\n[3] 视频类别折叠")
        page.evaluate("() => localStorage.removeItem('h3studio.sidebarFold')")
        page.reload(wait_until="domcontentloaded")
        wait_sidebar(page)

        page.click('#navVideoCategories .nav-cat-toggle[data-category="t2v"]')
        page.wait_for_timeout(600)
        st = page.evaluate("""() => {
          const cat = document.querySelector('.nav-cat[data-category="t2v"]');
          const models = cat.querySelector('.nav-cat-models');
          return {
            expanded: cat.querySelector('.nav-cat-toggle').getAttribute('aria-expanded'),
            headExpanded: cat.querySelector('.nav-cat-head').getAttribute('aria-expanded'),
            modelsVisible: models.offsetParent !== null,
            stored: localStorage.getItem('h3studio.sidebarFold'),
          };
        }""")
        check("点类别折叠开关 -> 模型列表隐藏", not st["modelsVisible"])
        check("开关 aria-expanded 变 false",
              st["expanded"] == "false", f"expanded={st['expanded']}")
        check("类别 head 的 aria-expanded 同步为 false",
              st["headExpanded"] == "false", f"head={st['headExpanded']}")
        check("类别折叠状态已写入 localStorage",
              "t2v" in (st["stored"] or ""), f"stored={st['stored']}")

        page.reload(wait_until="domcontentloaded")
        wait_sidebar(page)
        st2 = page.evaluate("""() => {
          const cat = document.querySelector('.nav-cat[data-category="t2v"]');
          return {
            modelsVisible: cat.querySelector('.nav-cat-models').offsetParent !== null,
            otherVisible: document.querySelector('.nav-cat[data-category="flf"] .nav-cat-models')
                           .offsetParent !== null,
          };
        }""")
        check("刷新后该类别仍是折叠态（持久化）", not st2["modelsVisible"])
        check("其它类别不受影响（未连带折叠）", st2["otherVisible"])

        # 点类别标题 -> 选中该类别且自动展开
        page.click('.nav-cat-head[data-category="t2v"]')
        page.wait_for_timeout(900)
        st3 = page.evaluate("""() => ({
          modelsVisible: document.querySelector('.nav-cat[data-category="t2v"] .nav-cat-models')
                         .offsetParent !== null,
          mode: window.__h3State ? window.__h3State.mode : null,
        })""")
        check("点类别标题 -> 展开该类别", st3["modelsVisible"])
        check("点类别标题 -> 切到对应 mode",
              st3["mode"] in ("t2v", "flf"), f"mode={st3['mode']}")

        # ---------------------------------------------------------- 滚动
        print("\n[4] 左侧列独立滚动")
        page.evaluate("() => localStorage.removeItem('h3studio.sidebarFold')")
        page.reload(wait_until="domcontentloaded")
        wait_sidebar(page)

        scroll_info = page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          if (!el) return {exists: false};
          const cs = getComputedStyle(el);
          return {
            exists: true,
            overflowY: cs.overflowY,
            scrollHeight: el.scrollHeight,
            clientHeight: el.clientHeight,
            canScroll: el.scrollHeight > el.clientHeight + 1,
          };
        }""")
        check(".nav-scroll 滚动容器存在", scroll_info.get("exists"))
        check("滚动容器 overflow-y 是 auto/scroll",
              scroll_info.get("overflowY") in ("auto", "scroll"),
              f"overflowY={scroll_info.get('overflowY')}")

        # 矮视口：375 高
        page.set_viewport_size({"width": 375, "height": 375})
        page.wait_for_timeout(900)
        page.evaluate("() => { document.getElementById('navToggle').click(); }")
        page.wait_for_timeout(700)
        short = page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          return {
            canScroll: el.scrollHeight > el.clientHeight + 1,
            scrollH: el.scrollHeight,
            clientH: el.clientHeight,
          };
        }""")
        check("375px 高视口下左侧列可滚动",
              short["canScroll"], f"scrollH={short['scrollH']} clientH={short['clientH']}")

        # 滚到底 -> 最下面几项可见
        bottom = page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          el.scrollTop = el.scrollHeight;
          const foot = document.querySelector('.sidebar-foot');
          const system = document.querySelector('.nav-item#navSystem');
          const fr = foot.getBoundingClientRect();
          const sr = system.getBoundingClientRect();
          const er = el.getBoundingClientRect();
          const bar = document.querySelector('#sidebar').getBoundingClientRect();
          return {
            scrollTop: el.scrollTop,
            // 最后一个导航项（硬件参数）在滚动区内：滚到底必须落在滚动区可视范围内
            systemVisible: sr.top >= er.top - 1 && sr.bottom <= er.bottom + 1,
            // 释放显存按钮在滚动区**外**（常驻底栏），对着侧栏判定
            footVisible: fr.top >= bar.top - 1 && fr.bottom <= bar.bottom + 1,
            footText: (foot.textContent || '').trim(),
            footOutsideScroll: !el.contains(foot),
          };
        }""")
        check("滚到底后「硬件参数」（最后一个导航项）可见",
              bottom["systemVisible"], f"system={bottom['systemVisible']}")
        check("「释放显存」常驻底栏（在滚动区外）且可见",
              bottom["footVisible"] and bottom["footOutsideScroll"],
              f"foot={bottom['footVisible']} outside={bottom['footOutsideScroll']}")
        check("滚动真的生效（scrollTop > 0，不是被 overflow 截断）",
              bottom["scrollTop"] > 0, f"scrollTop={bottom['scrollTop']}")
        # 折叠几个分组后，滚动区内容变矮 —— 仍应能滚到最后一项
        page.evaluate("""() => {
          ['image', 'image-edit', 'music'].forEach(k => {
            const g = document.querySelector('.nav-group[data-nav-key="' + k + '"]');
            if (g && g.getAttribute('aria-expanded') === 'true') g.click();
          });
        }""")
        page.wait_for_timeout(600)
        after = page.evaluate("""() => {
          const el = document.getElementById('navScroll');
          el.scrollTop = el.scrollHeight;
          const sr = document.querySelector('.nav-item#navSystem').getBoundingClientRect();
          const er = el.getBoundingClientRect();
          return {
            visible: sr.top >= er.top - 1 && sr.bottom <= er.bottom + 1,
            scrollTop: el.scrollTop,
          };
        }""")
        check("折叠多个分组后仍能滚到最后一项",
              after["visible"], f"visible={after['visible']}")
        # 展开回来
        page.evaluate("""() => {
          ['image', 'image-edit', 'music'].forEach(k => {
            const g = document.querySelector('.nav-group[data-nav-key="' + k + '"]');
            if (g && g.getAttribute('aria-expanded') === 'false') g.click();
          });
        }""")
        page.wait_for_timeout(500)

        # ---------------------------------------------------------- 三档宽度截图
        print("\n[5] 三档宽度截图 + console 0 错误 + 无横向溢出")
        for width, height in ((375, 375), (390, 375), (1280, 900)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(900)
            # 窄屏把抽屉拉开，桌面端常驻。
            # 【为什么要判状态再点】[4] 步骤已经开过一次抽屉，
            # 无脑再点会把抽屉**关掉** —— 截图里就只剩主界面，侧栏根本没入镜。
            if width < 700:
                page.evaluate("""() => {
                  const sb = document.getElementById('sidebar');
                  if (!sb.classList.contains('open')) {
                    document.getElementById('navToggle').click();
                  }
                }""")
                page.wait_for_timeout(700)
            drawer_open = page.evaluate(
                "() => document.getElementById('sidebar').classList.contains('open')")
            check(f"[{width}x{height}] 侧栏可见（窄屏抽屉已拉开 / 桌面端常驻）",
                  drawer_open or width >= 700)

            # 滚动位置归零 —— [4] 步骤把 scrollTop 推到最底了，
            # 不归零的话截图拍到的是「滚到底」的样子，看不出顶部分组。
            page.evaluate("() => { document.getElementById('navScroll').scrollTop = 0; }")
            page.wait_for_timeout(400)

            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"[{width}x{height}] 无横向溢出", overflow <= 1, f"overflow={overflow}")

            shot = EVIDENCE / f"sidebar-fold-{width}x{height}.png"
            page.screenshot(path=str(shot), full_page=False)
            print(f"       截图: {shot}")

            # 滚到底的证据图：证明矮视口下最下面几项够得着
            if height <= 500:
                page.evaluate("""() => {
                  const el = document.getElementById('navScroll');
                  el.scrollTop = el.scrollHeight;
                }""")
                page.wait_for_timeout(500)
                shot_bottom = EVIDENCE / f"sidebar-fold-{width}x{height}-scrolled-bottom.png"
                page.screenshot(path=str(shot_bottom), full_page=False)
                print(f"       截图: {shot_bottom}")
                page.evaluate("() => { document.getElementById('navScroll').scrollTop = 0; }")
                page.wait_for_timeout(300)

            # 折叠态截图：把「文生图」折起来，看箭头与隐藏效果
            page.evaluate("""() => {
              const g = document.querySelector('.nav-group[data-nav-key="image"]');
              if (g && g.getAttribute('aria-expanded') === 'true') g.click();
            }""")
            page.wait_for_timeout(600)
            shot2 = EVIDENCE / f"sidebar-fold-{width}x{height}-collapsed.png"
            page.screenshot(path=str(shot2), full_page=False)
            print(f"       截图: {shot2}")
            # 展开回来
            page.evaluate("""() => {
              const g = document.querySelector('.nav-group[data-nav-key="image"]');
              if (g && g.getAttribute('aria-expanded') === 'false') g.click();
            }""")
            page.wait_for_timeout(500)

            if width < 700:
                page.evaluate("""() => {
                  const sb = document.getElementById('sidebar');
                  if (sb.classList.contains('open')) {
                    document.getElementById('navToggle').click();
                  }
                }""")
                page.wait_for_timeout(600)

        # console 错误
        real_errors = [e for e in console_errors
                       if "favicon" not in e.lower() and "Failed to load resource" not in e]
        check("console 0 错误", not real_errors, f"errors={real_errors[:3]}")

        browser.close()
    print("\n" + "=" * 70)
    if failures:
        print(f"验收失败 {len(failures)} 项：")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run())
