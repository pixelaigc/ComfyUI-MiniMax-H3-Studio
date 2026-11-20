#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""轮询不干扰用户观察 —— 浏览器实测。

用户明确要求过：「获取状态不能影响用户」。
本脚本验证：**轮询刷新（每 2 秒一次）期间**，下面这些**都不能被重置**：
  * 滚动位置（媒体库列表区 + 页面）
  * 当前标签（.jobs-tabs 的 active）
  * 每页数量下拉（#donePageSize 的值）
  * 筛选条件（搜索框的值）
  * 卡片 DOM 不整体重建（节点身份不变 -> 视频不中断、焦点不丢）

同时验证：
  * 缩略图 URL 不变 -> 卡片不会被重建
  * 控制台无 JS 错误

用法: python scripts/verify_poll_does_not_disturb.py --base http://127.0.0.1:8914
"""
from __future__ import annotations

import argparse
import json
import os
import time

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8914")
    ap.add_argument("--out", default=None)
    ap.add_argument("--polls", type=int, default=3)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    results = []
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        results.append({"name": name, "pass": bool(cond), "detail": str(detail)})
        print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")
        if not cond:
            ok = False

    console_errors = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        pg.goto(base + "/", wait_until="domcontentloaded", timeout=180000)
        pg.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=180000)
        pg.click(".jobs-tabs button[data-st='done']", timeout=30000)
        pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
        pg.wait_for_timeout(1500)

        # --- 造一个「用户正在观察」的状态 ---
        # 1) 滚到列表中部
        pg.evaluate("""() => {
            const area = document.querySelector('#jobsArea') || document.querySelector('#jobsGrid');
            if (area) area.scrollTop = 180;
            window.scrollTo(0, 90);
        }""")
        pg.wait_for_timeout(400)
        # 2) 改每页数量下拉
        pg.evaluate("""() => {
            const sel = document.querySelector('#pageSizeSelect');
            if (sel) { sel.value = '24'; sel.dispatchEvent(new Event('change', {bubbles: true})); }
        }""")
        pg.wait_for_timeout(1200)
        # 3) 输入搜索词
        pg.evaluate("""() => {
            const box = document.querySelector('#doneSearch');
            if (box) { box.value = 'h3'; box.dispatchEvent(new Event('input', {bubbles: true})); }
        }""")
        pg.wait_for_timeout(1500)

        before = pg.evaluate(
            """() => {
                const area = document.querySelector('#jobsArea');
                const grid = document.querySelector('#jobsGrid');
                const sel = document.querySelector('#pageSizeSelect');
                const box = document.querySelector('#doneSearch');
                const act = document.querySelector('.jobs-tabs button.active[data-st]');
                const cards = [...document.querySelectorAll('.job-card[data-jid]')];
                return {
                    active_tab: act ? act.dataset.st : null,
                    area_scroll_top: area ? area.scrollTop : -1,
                    page_scroll_top: window.scrollY,
                    page_size: sel ? sel.value : null,
                    search: box ? box.value : null,
                    card_count: cards.length,
                    // 节点身份：用来判断卡片有没有被重建
                    first_card_marker: cards.length ? (cards[0].dataset.sig || '') : '',
                    first_card_src: cards.length
                        ? (cards[0].querySelector('.job-preview img, .job-preview video')?.getAttribute('src') || '')
                        : '',
                    first_card_poster: cards.length
                        ? (cards[0].querySelector('.job-preview video')?.getAttribute('poster') || '')
                        : '',
                };
            }"""
        )
        print(f"  轮询前状态: {json.dumps(before, ensure_ascii=False)}")

        # 标记第一个卡片节点，用来判断有没有被替换
        pg.evaluate("""() => {
            const c = document.querySelector('.job-card[data-jid]');
            if (c) c.dataset.pollMarker = 'KEEPME';
        }""")

        # --- 等 N 轮轮询过去 ---
        wait = 2600 * args.polls
        pg.wait_for_timeout(wait)

        after = pg.evaluate(
            """() => {
                const area = document.querySelector('#jobsArea');
                const grid = document.querySelector('#jobsGrid');
                const sel = document.querySelector('#pageSizeSelect');
                const box = document.querySelector('#doneSearch');
                const act = document.querySelector('.jobs-tabs button.active[data-st]');
                const cards = [...document.querySelectorAll('.job-card[data-jid]')];
                const marked = document.querySelector('[data-poll-marker="KEEPME"]');
                return {
                    active_tab: act ? act.dataset.st : null,
                    area_scroll_top: area ? area.scrollTop : -1,
                    page_scroll_top: window.scrollY,
                    page_size: sel ? sel.value : null,
                    search: box ? box.value : null,
                    card_count: cards.length,
                    marker_survived: !!marked,
                    first_card_src: cards.length
                        ? (cards[0].querySelector('.job-preview img, .job-preview video')?.getAttribute('src') || '')
                        : '',
                    first_card_poster: cards.length
                        ? (cards[0].querySelector('.job-preview video')?.getAttribute('poster') || '')
                        : '',
                };
            }"""
        )
        print(f"  轮询后状态: {json.dumps(after, ensure_ascii=False)}")
        pg.screenshot(path=f"{OUT_DIR}/poll-disturb-after.png", full_page=False)
        pg.close()
        browser.close()

    check("当前标签没被重置", before["active_tab"] == after["active_tab"] and after["active_tab"] == "done",
          f"{before['active_tab']} -> {after['active_tab']}")
    check("列表滚动位置没被重置",
          abs(before["area_scroll_top"] - after["area_scroll_top"]) <= 2,
          f"{before['area_scroll_top']} -> {after['area_scroll_top']}")
    check("页面滚动位置没被重置",
          abs(before["page_scroll_top"] - after["page_scroll_top"]) <= 2,
          f"{before['page_scroll_top']} -> {after['page_scroll_top']}")
    check("每页数量下拉没被重置", before["page_size"] == after["page_size"],
          f"{before['page_size']} -> {after['page_size']}")
    check("搜索框没被清空", before["search"] == after["search"],
          f"{before['search']!r} -> {after['search']!r}")
    check("卡片数量没变", before["card_count"] == after["card_count"],
          f"{before['card_count']} -> {after['card_count']}")
    check("卡片 DOM 没被整体重建（节点身份保留）", after["marker_survived"],
          "标记节点还在 = 增量更新，不是整库重建")
    check("卡片媒体 URL 没变（所以不会重新下载）",
          before["first_card_src"] == after["first_card_src"]
          and before["first_card_poster"] == after["first_card_poster"],
          f"src {before['first_card_src'][-28:]} -> {after['first_card_src'][-28:]}")
    check("控制台无 JS 错误", len(console_errors) == 0, f"{len(console_errors)} 个: {console_errors[:2]}")

    path = args.out or f"{OUT_DIR}/verify-poll-disturb.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"before": before, "after": after,
                   "console_errors": console_errors, "results": results},
                  f, ensure_ascii=False, indent=2)
    print(f"\n=== 摘要：{sum(1 for r in results if r['pass'])}/{len(results)} PASS ===")
    print(f"证据: {path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
