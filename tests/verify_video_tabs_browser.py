# -*- coding: utf-8 -*-
"""Playwright 真实浏览器验收：视频两级导航 + 2.0M 分辨率（三档宽度）。

【为什么单独写一个脚本】
需求验收要求「临时端口起服务做浏览器验证」，并且要在三档宽度下截图、
console 0 错误。仓库已有的 tests/test_responsive_mobile.py 测的是
通用响应式，不是本次的视频两级导航 —— 单独一个脚本，断言更贴需求。

【覆盖】
  * 三档宽度 375 / 390 / 1280：
      - 四个类别顺序与文案逐字符合需求
      - 每个类别下有模型项（第二级）
      - console 0 错误
      - 无横向溢出
  * 交互：
      - 点类别 -> 选中该类别，模型按回退规则选
      - 点模型项 -> 切到对应 mode，两级高亮同步
      - 刷新 -> 恢复上次的类别+模型（localStorage）
      - 切回图像模式 -> 视频分组取消高亮（不出现两个高亮项）
  * 分辨率：
      - 1920x1088 可达（2.0MP 预设）
      - 超过 2.0M 时给出明确提示（不静默截断）
  * 截图：写到 evidence 目录

用法: python3 tests/verify_video_tabs_browser.py [base_url] [evidence_dir]
"""
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8231"
EVIDENCE = Path(sys.argv[2] if len(sys.argv) > 2 else
                 "/root/.hermes-web-ui/.ekko/workspace/default/"
                 "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)

WIDTHS = (375, 390, 1280)
EXPECTED_CATS = ["文生视频", "首尾帧生视频", "参考生视频", "长片拼接"]


def run():
    from playwright.sync_api import sync_playwright

    failures = []
    console_errors = []

    def check(name, cond, detail=""):
        status = "OK  " if cond else "FAIL"
        print(f"  [{status}] {name}" + (f"  {detail}" if detail else ""))
        if not cond:
            failures.append(f"{name} {detail}")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: console_errors.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

        # ---------------------------------------------------------- 首屏
        print("\n[1] 首屏加载（1280）")
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        page.evaluate("() => localStorage.removeItem('h3studio.videoNav')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)

        cats = page.eval_on_selector_all(
            "#navVideoCategories .nav-cat-label", "els => els.map(e => e.textContent.trim())")
        check("四个类别顺序与文案符合需求",
              cats == EXPECTED_CATS, f"got={cats}")

        n_models = page.eval_on_selector_all(
            "#navVideoCategories .nav-cat", "els => els.map(e => e.querySelectorAll('.nav-model-item').length)")
        check("每个类别下都有模型项（第二级）",
              all(n >= 1 for n in n_models), f"counts={n_models}")

        active = page.eval_on_selector_all(
            ".nav-item.active", "els => els.map(e => e.textContent.trim())")
        check("首屏只有一个高亮项（默认 Z-IMAGE 文生图）",
              active == ["Z-IMAGE 文生图"], f"got={active}")

        # ---------------------------------------------------------- 交互
        print("\n[2] 交互：类别 / 模型 / 持久化 / 高亮一致性")
        page.click('.nav-cat-head[data-category="flf"]')
        page.wait_for_timeout(900)
        st = page.evaluate("""() => ({
            mode: window.__h3state().mode,
            cat: [...document.querySelectorAll('.nav-cat-head.active')]
                 .map(e => e.querySelector('.nav-cat-label').textContent.trim()),
            model: [...document.querySelectorAll('.nav-model-item.active')]
                 .map(e => e.dataset.mode),
            nav: [...document.querySelectorAll('.nav-item.active')]
                 .map(e => e.textContent.trim()),
        })""")
        check("点类别标题 -> 切到该类别对应的 mode", st["mode"] == "flf", f"mode={st['mode']}")
        check("类别标题高亮同步", st["cat"] == ["首尾帧生视频"], f"cat={st['cat']}")
        check("模型项高亮同步", st["model"] == ["flf"], f"model={st['model']}")

        page.click('.nav-model-item[data-mode="r2v"]')
        page.wait_for_timeout(900)
        st = page.evaluate("""() => ({
            mode: window.__h3state().mode,
            cat: [...document.querySelectorAll('.nav-cat-head.active')]
                 .map(e => e.querySelector('.nav-cat-label').textContent.trim()),
        })""")
        check("点模型项 -> 切到对应 mode", st["mode"] == "r2v", f"mode={st['mode']}")
        check("切到别的模型后类别高亮跟着换",
              st["cat"] == ["参考生视频"], f"cat={st['cat']}")

        # 持久化：刷新后恢复
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        st = page.evaluate("""() => ({
            cat: [...document.querySelectorAll('.nav-cat-head.active')]
                 .map(e => e.querySelector('.nav-cat-label').textContent.trim()),
            model: [...document.querySelectorAll('.nav-model-item.active')]
                 .map(e => e.dataset.mode),
        })""")
        check("刷新后恢复上次类别（持久化）",
              st["cat"] == ["参考生视频"], f"cat={st['cat']}")
        check("刷新后恢复上次模型（持久化）",
              st["model"] == ["r2v"], f"model={st['model']}")

        # 切回图像模式 -> 视频分组取消高亮
        page.click('.nav-item[data-mode="t2i"]')
        page.wait_for_timeout(900)
        st = page.evaluate("""() => ({
            cat: [...document.querySelectorAll('.nav-cat-head.active')]
                 .map(e => e.querySelector('.nav-cat-label').textContent.trim()),
            nav: [...document.querySelectorAll('.nav-item.active')]
                 .map(e => e.textContent.trim()),
        })""")
        check("切回图像模式 -> 视频分组取消高亮（不出现两个高亮）",
              st["cat"] == [], f"cat={st['cat']}")
        check("切回图像模式 -> 只有 Z-IMAGE 高亮",
              st["nav"] == ["Z-IMAGE 文生图"], f"nav={st['nav']}")

        # ---------------------------------------------------------- 分辨率
        print("\n[3] 分辨率：1920x1088 可达 + 超限提示")
        page.click('.nav-model-item[data-mode="t2v"]')
        page.wait_for_timeout(1500)
        for _ in range(40):
            n = page.evaluate("() => document.querySelector('#vidSizePresetSelect')?.options?.length || 0")
            if n > 1:
                break
            page.wait_for_timeout(500)

        opts = page.eval_on_selector_all(
            "#vidSizePresetSelect option", "els => els.map(e => e.value)")
        check("尺寸预设含 16:9 的 2.0MP 档（=1920x1088）",
              "16:9_2.0" in opts, f"opts={opts}")

        page.evaluate("""() => {
            const s = document.querySelector('#vidSizePresetSelect');
            s.value = '16:9_2.0';
            s.dispatchEvent(new Event('change', {bubbles: true}));
        }""")
        page.wait_for_timeout(1500)
        size = page.evaluate("""() => ({
            w: document.querySelector('#vidWidth').value,
            h: document.querySelector('#vidHeight').value,
            note: document.querySelector('#vidSizeNote').textContent,
            adjust: document.querySelector('#vidSizeAdjust').textContent,
        })""")
        check("2.0MP 预设 -> 1920x1088",
              (size["w"], size["h"]) == ("1920", "1088"), f"got={size['w']}x{size['h']}")
        check("1920x1088 不触发调整提示（合法值）",
              not size["adjust"].strip(), f"adjust={size['adjust']!r}")
        check("尺寸提示里写明 2.0M 上限",
              "2.0M" in size["note"], f"note={size['note']!r}")

        # 超限
        page.evaluate("""() => {
            const w = document.querySelector('#vidWidth');
            w.value = '2560';
            w.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(800)
        page.evaluate("""() => {
            const h = document.querySelector('#vidHeight');
            h.value = '1440';
            h.dispatchEvent(new Event('input', {bubbles: true}));
        }""")
        page.wait_for_timeout(1600)
        over = page.evaluate("""() => ({
            adjust: document.querySelector('#vidSizeAdjust').textContent,
            hidden: document.querySelector('#vidSizeAdjust').hidden,
        })""")
        check("超 2.0M 时给出明确提示（不是静默截断）",
              (not over["hidden"]) and "上限" in over["adjust"],
              f"adjust={over['adjust']!r}")

        # ---------------------------------------------------------- 三档宽度
        print("\n[4] 三档宽度截图 + console 0 错误 + 无横向溢出")
        for width in WIDTHS:
            page.set_viewport_size({"width": width, "height": 900})
            page.wait_for_timeout(1200)
            # 切到视频模式，让截图里有两级导航 + 分辨率面板
            page.evaluate("""() => {
                const it = document.querySelector('.nav-model-item[data-mode="t2v"]');
                if (it) it.click();
            }""")
            page.wait_for_timeout(1500)

            cats_w = page.eval_on_selector_all(
                "#navVideoCategories .nav-cat-label", "els => els.map(e => e.textContent.trim())")
            check(f"[{width}px] 四个类别仍正确",
                  cats_w == EXPECTED_CATS, f"got={cats_w}")

            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check(f"[{width}px] 无横向溢出", overflow <= 1, f"overflow={overflow}")

            shot = EVIDENCE / f"video-tabs-{width}px.png"
            page.screenshot(path=str(shot), full_page=False)
            print(f"       截图: {shot}")

            # 窄屏下侧栏是抽屉 —— 额外拍一张「抽屉拉开」的，
            # 否则窄屏截图里根本看不到两级导航（证据就白拍了）。
            if width < 900:
                page.click("#navToggle")
                page.wait_for_timeout(900)
                drawer_shot = EVIDENCE / f"video-tabs-{width}px-drawer.png"
                page.screenshot(path=str(drawer_shot), full_page=False)
                print(f"       截图: {drawer_shot}")
                # 抽屉里两级的结构也要对
                cats_d = page.eval_on_selector_all(
                    "#navVideoCategories .nav-cat-label",
                    "els => els.map(e => e.textContent.trim())")
                check(f"[{width}px 抽屉] 四个类别仍正确",
                      cats_d == EXPECTED_CATS, f"got={cats_d}")
                models_d = page.eval_on_selector_all(
                    "#navVideoCategories .nav-model-item",
                    "els => els.map(e => e.textContent.trim())")
                check(f"[{width}px 抽屉] 模型项可见（第二级）",
                      len(models_d) >= 4, f"models={models_d}")
                page.click("#navToggle")
                page.wait_for_timeout(700)

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
