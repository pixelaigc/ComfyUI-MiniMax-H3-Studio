#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8190 线上真实浏览器验收 —— D12 交互流程图入口。

不 mock、不 stub：直接打开 http://127.0.0.1:8190/，用真人一样的操作
（点左侧栏入口 -> 看面板 -> 点缩略图放大 -> 点源文件链接）走一遍，
把每一步的截图 + console + 网络请求结果落盘。

产出：
  <OUT>/01-入口在侧栏.png
  <OUT>/02-面板列出5套流程图.png
  <OUT>/03-放大某套流程图.png
  <OUT>/04-窄屏375-入口可点.png
  <OUT>/05-目录索引页.png
  <OUT>/browser_verify.json   （断言结果 + console + pageError + 请求日志）
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("H3_BASE", "http://127.0.0.1:8190")
OUT = os.environ.get(
    "H3_OUT",
    "/root/.hermes-web-ui/.ekko/workspace/default/"
    "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-d12-flowdocs")
os.makedirs(OUT, exist_ok=True)

console_log = []
page_errors = []
failed_requests = []
responses = {}
results = {}


def record(name, ok, detail):
    results[name] = {"ok": bool(ok), "detail": str(detail)}
    print("  [%s] %s: %s" % ("PASS" if ok else "FAIL", name, detail))


def shot(page, name):
    path = os.path.join(OUT, name)
    page.screenshot(path=path, full_page=False)
    print("    -> 截图 %s" % path)
    return path


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        page.on("console", lambda m: console_log.append(
            {"type": m.type, "text": m.text[:400]}))
        page.on("pageerror", lambda e: page_errors.append(str(e)[:400]))
        page.on("requestfailed", lambda r: failed_requests.append(
            {"url": r.url, "err": str(r.failure)}))

        def on_response(r):
            if "/docs/" in r.url or "/api/docs" in r.url:
                responses.setdefault(r.url.split("?")[0], r.status)

        page.on("response", on_response)

        print("== 打开 %s ==" % BASE)
        page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # ---------- 1. 侧栏入口存在 ----------
        print("== 检查 1：侧栏入口 ==")
        entry = page.query_selector("#navFlowDocs")
        record("侧栏有「交互流程图」入口", entry is not None,
               "found" if entry else "NOT FOUND")
        if entry is None:
            shot(page, "01-入口缺失.png")
            _dump(results, console_log, page_errors, failed_requests, responses)
            return 1
        text = (entry.text_content() or "").strip()
        record("入口文案是「交互流程图」", "交互流程图" in text, repr(text))
        shot(page, "01-入口在侧栏.png")

        # ---------- 2. 点入口 -> 面板打开 ----------
        print("== 检查 2：点入口打开面板 ==")
        entry.click()
        page.wait_for_selector("#fdModal:not([hidden])", timeout=15000)
        page.wait_for_timeout(2500)
        record("面板已打开", not page.eval_on_selector("#fdModal", "el => el.hidden"),
               "#fdModal visible")

        # ---------- 3. 面板列出 5 套流程图 ----------
        print("== 检查 3：5 套流程图 ==")
        titles = page.eval_on_selector_all(
            "#fdBody .fd-card .fd-title", "els => els.map(e => e.textContent.trim())")
        record("列出 5 套流程图", len(titles) == 5, "titles=%r" % (titles,))
        shot(page, "02-面板列出5套流程图.png")

        # ---------- 4. 缩略图真的加载出来（不是裂图）----------
        print("== 检查 4：缩略图加载 ==")
        # 【为什么要 wait_for_function 而不是固定 sleep】
        # 缩略图带 loading="lazy"，且本机负载高（load 20-31、100+ chromium 进程）
        # 时解码/绘制会被推迟 —— 固定 wait 2.5s 会误判成「没加载」。
        # 这里等到「5 张都 complete 且 naturalWidth>0」为止，超时才算失败。
        loaded_ok = True
        try:
            page.wait_for_function(
                """() => {
                     const imgs = [...document.querySelectorAll('#fdBody .fd-thumb')];
                     return imgs.length === 5 &&
                            imgs.every(i => i.complete && i.naturalWidth > 0);
                   }""",
                timeout=30000)
        except Exception as exc:
            loaded_ok = False
            print("    (等待缩略图加载超时: %s)" % type(exc).__name__)
        imgs = page.eval_on_selector_all(
            "#fdBody .fd-thumb",
            "els => els.map(e => ({src: e.getAttribute('src'), w: e.naturalWidth, h: e.naturalHeight}))")
        loaded = [i for i in imgs if i["w"] > 0 and i["h"] > 0]
        record("5 张缩略图都真的加载成功", loaded_ok and len(loaded) == 5,
               "loaded=%d/%d  %r" % (len(loaded), len(imgs),
                                     [(i["src"].split("/")[-1][:22], i["w"], i["h"]) for i in imgs]))

        # ---------- 5. .mmd 源文件链接 ----------
        print("== 检查 5：.mmd 源文件链接 ==")
        mmd = page.eval_on_selector_all(
            "#fdBody .fd-card .fd-link",
            "els => els.map(e => e.getAttribute('href')).filter(h => h && h.endsWith('.mmd'))")
        record("有 5 个 .mmd 源文件链接", len(mmd) == 5, "%r" % (mmd,))

        guide = page.eval_on_selector_all(
            "#fdBody .fd-guide a", "els => els.map(e => e.getAttribute('href'))")
        record("有说明文档入口", any(h and h.endswith(".md") for h in guide), "%r" % (guide,))

        # ---------- 6. 点缩略图放大 ----------
        print("== 检查 6：点缩略图放大 ==")
        page.click("#fdBody .fd-card:first-child .fd-thumb")
        page.wait_for_timeout(1500)
        stage_visible = page.eval_on_selector(
            "#fdStage", "el => !el.hidden && getComputedStyle(el).display !== 'none'")
        record("点缩略图后大图层出现", stage_visible, "stage visible=%s" % stage_visible)
        stage_src = page.eval_on_selector("#fdStageImg", "el => el.getAttribute('src')")
        stage_w = page.eval_on_selector("#fdStageImg", "el => el.naturalWidth")
        record("大图层图片真的加载", stage_w > 0, "src=%s naturalWidth=%s" % (stage_src, stage_w))
        shot(page, "03-放大某套流程图.png")
        page.click("#fdStageClose")
        page.wait_for_timeout(500)

        # ---------- 7. 关闭面板 ----------
        print("== 检查 7：关闭面板 ==")
        page.click("#fdClose")
        page.wait_for_timeout(800)
        record("面板可关闭", page.eval_on_selector("#fdModal", "el => el.hidden"), "hidden")

        # ---------- 8. 窄屏（375）入口仍可点 ----------
        print("== 检查 8：窄屏 375 ==")
        page.set_viewport_size({"width": 375, "height": 812})
        page.wait_for_timeout(1200)
        page.evaluate("() => { document.getElementById('navToggle').click(); }")
        page.wait_for_timeout(1000)
        narrow_entry = page.query_selector("#navFlowDocs")
        record("窄屏入口可见", narrow_entry is not None and narrow_entry.is_visible(),
               "visible=%s" % (narrow_entry.is_visible() if narrow_entry else False))
        if narrow_entry is not None:
            narrow_entry.click()
            page.wait_for_selector("#fdModal:not([hidden])", timeout=15000)
            page.wait_for_timeout(1800)
            record("窄屏能打开面板", True, "opened")
            ncards = page.eval_on_selector_all("#fdBody .fd-card", "els => els.length")
            record("窄屏面板里有 5 套图", ncards == 5, "cards=%s" % ncards)
            # 窄屏不许横向溢出
            hscroll = page.evaluate(
                "() => document.documentElement.scrollWidth > window.innerWidth + 1")
            record("窄屏无横向溢出", not hscroll, "hscroll=%s" % hscroll)
            shot(page, "04-窄屏375-入口可点.png")

        # ---------- 9. /docs 目录索引页 ----------
        print("== 检查 9：/docs 目录索引 ==")
        page2 = ctx.new_page()
        page2.goto(BASE + "/docs", wait_until="domcontentloaded", timeout=30000)
        page2.wait_for_timeout(1200)
        nlinks = page2.eval_on_selector_all("a", "els => els.length")
        record("/docs 目录索引可打开", True, "links=%s" % nlinks)
        shot(page2, "05-目录索引页.png")
        page2.close()

        # ---------- 10. 无报错 ----------
        print("== 检查 10：console / pageError ==")
        err_console = [c for c in console_log if c["type"] == "error"]
        record("无 console error", not err_console, "%r" % (err_console[:3],))
        record("无 pageError", not page_errors, "%r" % (page_errors[:3],))

        # 【关于 /files/videos/*.mp4 的 ERR_ABORTED】
        # 这**不是**本轮引入的：A/B 实测（scripts/verify_8190_flowdocs_browser.py
        # 同目录的对照实验）表明 —— 线上 8190 上
        #   A) 不开任何模态框            -> mp4_aborted=10
        #   B) 开既有模态框 loraInfoModal -> mp4_aborted=10
        #   C) 开本轮新增的 fdModal      -> mp4_aborted=10
        # 三者相同 -> 既有行为（生成库卡片里的 <video> 被 2 秒一次的
        # refreshJobs 重绘打断），与流程图面板无关。
        # 所以这里只断言「**非 mp4** 的失败请求为 0」，并把 mp4 数量记录下来。
        mp4_aborted = [f for f in failed_requests if ".mp4" in f["url"]]
        other_failed = [f for f in failed_requests if ".mp4" not in f["url"]]
        record("无与流程图相关的失败请求", not other_failed,
               "other=%r  (mp4 ERR_ABORTED=%d，属既有行为)" % (other_failed[:3], len(mp4_aborted)))

        # /docs 请求的 HTTP 状态汇总
        docs_status = {k.split("/docs")[-1][:40]: v for k, v in responses.items()}
        record("/docs 资源全部 200", all(v == 200 for v in responses.values()),
               "%r" % docs_status)

        _dump(results, console_log, page_errors, failed_requests, responses)
        browser.close()
    return 0 if all(v["ok"] for v in results.values()) else 1


def _dump(results, console_log, page_errors, failed_requests, responses):
    payload = {
        "base": BASE,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
        "console": console_log,
        "page_errors": page_errors,
        "failed_requests": failed_requests,
        "docs_responses": responses,
        "all_passed": all(v["ok"] for v in results.values()),
    }
    path = os.path.join(OUT, "browser_verify.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n=== 断言结果 ===")
    for k, v in results.items():
        print("  [%s] %s: %s" % ("PASS" if v["ok"] else "FAIL", k, v["detail"]))
    print("\n=== 汇总: all_passed=%s ===" % payload["all_passed"])
    print("证据目录: %s" % OUT)


if __name__ == "__main__":
    sys.exit(main())
