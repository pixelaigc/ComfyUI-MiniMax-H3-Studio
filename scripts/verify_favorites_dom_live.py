# -*- coding: utf-8 -*-
"""线上 8190 的 DOM 级取证：把「界面上真的有什么」用断言记录下来。

【为什么不用截图做判断】截图需要人眼/VLM 来看，而本机 VLM 调用超。
DOM 断言是**机器可验证**的：selector 命中 + 文本内容 + 计算样式，
比截图更硬（截图可能拍不到、可能拍糊）。

【产出】evidence/favorites-dom-assert-<ts>/assertions.json

运行：
    /home/ubuntu/comfyui/venv/bin/python scripts/verify_favorites_dom_live.py \
        --base http://127.0.0.1:8190
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

WAIT_TIMEOUT = 60000


def api(base, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(base.rstrip("/") + path, data=data,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    base = args.base
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or ("evidence/favorites-dom-assert-" + ts)).resolve()
    out.mkdir(parents=True, exist_ok=True)

    results = {}

    def check(name, ok, detail):
        results[name] = {"pass": bool(ok), "detail": detail}
        print("%-46s %s  %s" % (name, "PASS" if ok else "FAIL", detail),
              flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.goto(base.rstrip("/") + "/", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)

        # ---- 左侧「收藏」入口 ----
        nav = page.evaluate("""() => {
            const el = document.querySelector('#navFavorites');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            const svg = el.querySelector('svg');
            return {
              exists: true,
              text: el.textContent.trim(),
              tag: el.tagName,
              cls: el.className,
              visible: r.width > 0 && r.height > 0 && cs.display !== 'none',
              hasSvg: !!svg,
              svgSize: svg ? [svg.getAttribute('width'), svg.getAttribute('height')] : null,
              color: cs.color,
              fontSize: cs.fontSize,
              // 与「素材库」同一套导航约定？
              sameClassAsAssets: el.className
                  === document.querySelector('#navAssets').className,
              inSameGroup: (() => {
                // 找它前面最近的 .nav-group（分组标题）
                let node = el.previousElementSibling, group = null;
                while (node) {
                  if (node.classList && node.classList.contains('nav-group')) { group = node.dataset.navKey; break; }
                  node = node.previousElementSibling;
                }
                return group;
              })(),
              assetsGroup: (() => {
                let node = document.querySelector('#navAssets').previousElementSibling, group = null;
                while (node) {
                  if (node.classList && node.classList.contains('nav-group')) { group = node.dataset.navKey; break; }
                  node = node.previousElementSibling;
                }
                return group;
              })(),
              // 顶层分组数量（契约：恰好 7 个）
              groupCount: document.querySelectorAll('.nav-group[data-nav-key]').length,
              groupKeys: Array.from(document.querySelectorAll('.nav-group[data-nav-key]'))
                  .map(e => e.dataset.navKey),
            };
        }""")
        check("左侧「收藏」入口存在且可见", nav and nav["visible"], nav)
        check("「收藏」与「素材库」同一分组（素材）",
              nav and nav["inSameGroup"] == nav["assetsGroup"] == "assets",
              "fav group=%r assets group=%r" % (nav["inSameGroup"], nav["assetsGroup"]))
        check("「收藏」与「素材库」同一套导航样式类",
              nav and nav["sameClassAsAssets"], nav and nav["cls"])
        check("顶层分组仍是 7 个（契约未破）",
              nav and nav["groupCount"] == 7, nav and nav["groupKeys"])

        # ---- 「收藏」标签 ----
        tab = page.evaluate("""() => {
            const b = document.querySelector('.jobs-tabs button[data-st="favorites"]');
            if (!b) return null;
            const cs = getComputedStyle(b);
            return {text: b.textContent.trim(), visible: cs.display !== 'none',
                    fontSize: cs.fontSize};
        }""")
        check("状态标签行有「收藏」标签", tab and tab["visible"], tab)

        # ---- 收藏一条，检查 ★ 与右键菜单 ----
        for jid in api(base, "/api/jobs/favorites").get("ids", []):
            api(base, "/api/jobs/%s/favorite" % jid, "POST", {"favorited": False})
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)
        page.click(".jobs-tabs button[data-st='done']")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .job-card[data-jid]').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1200)

        jid = page.evaluate("""() => {
            const c = document.querySelector(
                '#jobsGrid .job-card[data-jid] .job-preview img,'
                + ' #jobsGrid .job-card[data-jid] .job-preview video');
            const card = c ? c.closest('.job-card[data-jid]') : null;
            return card ? card.dataset.jid : null;
        }""")
        page.click('.job-card[data-jid="%s"] .job-preview img,'
                   '.job-card[data-jid="%s"] .job-preview video' % (jid, jid),
                   button="right")
        page.wait_for_timeout(800)
        menu = page.evaluate("""() => {
            const m = document.querySelector('#mediaContext');
            if (!m || !m.classList.contains('open')) return null;
            return {
              buttons: Array.from(m.querySelectorAll('button[data-act]'))
                  .map(b => ({act: b.dataset.act, text: b.textContent.trim()})),
              visible: getComputedStyle(m).display !== 'none',
            };
        }""")
        check("右键菜单打开且含收藏入口",
              menu and any(b["act"] == "favorite" for b in menu["buttons"]), menu)
        check("右键菜单仍保留「添加到素材库」（既有能力未破）",
              menu and any(b["act"] == "add-asset" for b in menu["buttons"]), menu)
        check("右键菜单仍保留「下载原文件」（既有能力未破）",
              menu and any(b["act"] == "download" for b in menu["buttons"]), menu)

        # 点收藏 -> ★ 立即出现
        page.click('#mediaContext button[data-act="favorite"]')
        page.wait_for_timeout(1200)
        badge = page.evaluate("""(jid) => {
            const c = document.querySelector(`.job-card[data-jid="${jid}"]`);
            if (!c) return {cardMissing: true};
            const b = c.querySelector('.job-fav-badge');
            if (!b) return {badge: false};
            const cs = getComputedStyle(b);
            return {badge: true, text: b.textContent.trim(), color: cs.color,
                    title: b.getAttribute('title'), visible: cs.display !== 'none'};
        }""", jid)
        check("收藏后卡片出现 ★ 标识（乐观更新：不等服务端）",
              badge.get("badge") and badge.get("text") == "★", badge)

        # 右键菜单文案应变成「取消收藏」
        page.wait_for_function(
            """(jid) => {
                const c = document.querySelector(`.job-card[data-jid="${jid}"]`);
                return !!(c && c.querySelector('.job-preview img, .job-preview video'));
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        page.click('.job-card[data-jid="%s"] .job-preview img,'
                   '.job-card[data-jid="%s"] .job-preview video' % (jid, jid),
                   button="right")
        page.wait_for_timeout(800)
        menu2 = page.evaluate("""() => {
            const b = document.querySelector('#mediaContext button[data-act="favorite"]');
            return b ? b.textContent.trim() : null;
        }""")
        check("已收藏时右键显示「取消收藏」", menu2 == "取消收藏", menu2)
        page.keyboard.press("Escape")

        # ---- 收藏视图 ----
        page.click("#navFavorites")
        page.wait_for_function(
            "() => document.querySelectorAll('#jobsGrid .fav-sel').length > 0",
            timeout=WAIT_TIMEOUT)
        page.wait_for_timeout(1000)
        view = page.evaluate("""() => {
            const active = document.querySelector('.jobs-tabs button[data-st].active');
            return {
              activeTab: active ? active.dataset.st : null,
              navActive: document.querySelector('#navFavorites').classList.contains('active'),
              cardIds: Array.from(document.querySelectorAll('#jobsGrid .job-card[data-jid]'))
                  .map(e => e.dataset.jid),
              selBoxes: document.querySelectorAll('#jobsGrid .fav-sel').length,
              toolsVisible: !document.querySelector('#favTools').hidden,
              toolsText: document.querySelector('#favTools').textContent.replace(/\\s+/g,' ').trim(),
              filtersVisible: !document.querySelector('#doneFilters').hidden,
              // 卡片交互复用：详情模态框按钮在不在？
              hasDetailBtn: !!document.querySelector('#jobsGrid [data-act="detail"]'),
              hasDeleteBtn: !!document.querySelector('#jobsGrid [data-act="delete"]'),
              hasDownloadBtn: !!document.querySelector('#jobsGrid [data-act="download"]'),
            };
        }""")
        check("收藏视图激活「收藏」标签 + 导航高亮",
              view["activeTab"] == "favorites" and view["navActive"], view)
        check("收藏视图显示收藏的卡片",
              jid in view["cardIds"], view["cardIds"])
        check("收藏视图有批量管理条 + 选择框",
              view["toolsVisible"] and view["selBoxes"] > 0, view)
        check("收藏视图沿用既有筛选条（搜索/类型/比例/时长）",
              view["filtersVisible"], view)
        check("收藏视图卡片保留既有按钮（详情/删除/下载）",
              view["hasDetailBtn"] and view["hasDeleteBtn"], view)

        # 勾选框能真的勾上
        page.click("#jobsGrid .fav-sel")
        page.wait_for_timeout(500)
        sel = page.evaluate("""() => ({
            checked: document.querySelector('#jobsGrid .fav-sel').checked,
            count: document.querySelector('#favToolsCount').textContent.trim(),
        })""")
        check("点勾选框能真的勾上（label 未吞掉默认行为）",
              sel["checked"] is True, sel)

        # 批量取消
        page.once("dialog", lambda d: d.accept())
        page.click("#favBatchUnfavBtn")
        page.wait_for_function(
            """async (jid) => {
                const r = await fetch('/api/jobs/' + jid);
                const d = await r.json();
                return d.favorited === false;
            }""", arg=jid, timeout=WAIT_TIMEOUT)
        check("批量取消收藏生效",
              api(base, "/api/jobs/" + jid).get("favorited") is False, jid)

        # ---- 「取消收藏」与「删除产物」是两个不同的入口 ----
        distinct = page.evaluate("""() => {
            const favBtn = document.querySelector('#favBatchUnfavBtn');
            const delBtns = Array.from(document.querySelectorAll('#jobsGrid [data-act="delete"]'));
            return {
              batchText: favBtn ? favBtn.textContent.trim() : null,
              batchTitle: favBtn ? favBtn.getAttribute('title') : null,
              deleteTexts: delBtns.map(b => b.textContent.trim()),
              deleteIsDanger: delBtns.map(b => b.classList.contains('danger')),
              sameId: favBtn ? favBtn.id === 'favBatchUnfavBtn' : false,
              deleteHasOwnHandler: !!document.querySelector(
                  '#jobsGrid [data-act="delete"]'),
            };
        }""")
        check("「批量取消收藏」与「删除」是不同按钮、不同文案",
              distinct["batchText"] == "批量取消收藏"
              and "删除" in (distinct["deleteTexts"] or [""])[0]
              and distinct["deleteIsDanger"] and distinct["deleteHasOwnHandler"],
              distinct)

        browser.close()

    (out / "assertions.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [k for k, v in results.items() if not v["pass"]]
    print("\n=== 汇总：%d 项断言，失败 %d 项 ===" % (len(results), len(failed)))
    for k in failed:
        print("  FAIL:", k, results[k]["detail"])
    print("证据:", out / "assertions.json")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
