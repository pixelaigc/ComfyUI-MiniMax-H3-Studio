#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量请求时间轴取证：找出「慢」的那一段到底是谁。

上一版发现：浏览器里 /api/jobs 第一次要 632ms，但 curl 只要 4ms。
差异说明 **有别的东西占着 aiohttp 事件循环**。这一版把**所有**请求
按时间排开，看谁先开始、谁最慢、谁和 /api/jobs 重叠。

用法: python scripts/trace_all_requests.py --base http://127.0.0.1:8190 --out ...
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
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    ap.add_argument("--wait-ms", type=int, default=30000)
    ap.add_argument("--action", default="tabdone", choices=["tabdone", "pageload", "taball"])
    args = ap.parse_args()

    recs = []
    by_req = {}
    console = []
    t0 = time.perf_counter()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()

        def on_request(r):
            rec = {
                "rid": id(r), "t_start": round((time.perf_counter() - t0) * 1000.0, 1),
                "method": r.method, "url": r.url,
                "path": r.url.split("?")[0], "query": r.url.split("?")[1] if "?" in r.url else "",
                "rtype": r.resource_type, "status": None, "t_resp": None, "t_done": None,
                "bytes": 0, "failed": None,
            }
            recs.append(rec)
            by_req[id(r)] = rec

        def on_response(r):
            rec = by_req.get(id(r.request))
            if rec:
                rec["status"] = r.status
                rec["t_resp"] = round((time.perf_counter() - t0) * 1000.0, 1)

        def on_finished(r):
            rec = by_req.get(id(r))
            if rec:
                rec["t_done"] = round((time.perf_counter() - t0) * 1000.0, 1)
                try:
                    rec["bytes"] = len(r.body())
                except Exception:  # noqa: BLE001
                    rec["bytes"] = -1

        def on_failed(r):
            rec = by_req.get(id(r))
            if rec:
                rec["failed"] = r.failure

        pg.on("request", on_request)
        pg.on("response", on_response)
        pg.on("requestfinished", on_finished)
        pg.on("requestfailed", on_failed)
        pg.on("console", lambda m: console.append({"type": m.type, "text": m.text}))

        pg.goto(args.base + "/", wait_until="domcontentloaded", timeout=180000)
        pg.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=180000)
        marks = {"js_ready": round((time.perf_counter() - t0) * 1000.0, 1)}

        if args.action != "pageload":
            tc = time.perf_counter()
            tab = "all" if args.action == "taball" else "done"
            pg.click(f".jobs-tabs button[data-st='{tab}']", timeout=30000)
            marks["tab_click_end"] = round((time.perf_counter() - tc) * 1000.0, 1)
            pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
            marks["first_card_after_click"] = round((time.perf_counter() - tc) * 1000.0, 1)

        # 等网络静默或超时
        try:
            pg.wait_for_load_state("networkidle", timeout=args.wait_ms)
            marks["networkidle"] = round((time.perf_counter() - t0) * 1000.0, 1)
        except Exception:  # noqa: BLE001
            marks["networkidle"] = -1
        marks["total"] = round((time.perf_counter() - t0) * 1000.0, 1)
        pg.close()
        browser.close()

    for r in recs:
        r["ms_total"] = (r["t_done"] - r["t_start"]) if r["t_done"] else None
        r["ms_to_resp"] = (r["t_resp"] - r["t_start"]) if r["t_resp"] else None
        r.pop("rid", None)

    api = [r for r in recs if "/api/" in r["path"]]
    media = [r for r in recs if "/files/" in r["path"]]
    static = [r for r in recs if "/api/" not in r["path"] and "/files/" not in r["path"]]

    print(f"=== {args.action} ===  marks={marks}")
    print(f"  总请求 {len(recs)}  api {len(api)}  media {len(media)}  static {len(static)}")

    print("\n  --- API 请求按耗时排序（Top 20）---")
    for r in sorted(api, key=lambda x: -(x["ms_total"] or 0))[:20]:
        print(f"    {(r['ms_total'] or 0):>9.1f}ms  ttfb={(r['ms_to_resp'] or 0):>8.1f}ms  {r['bytes']:>8}B  "
              f"t={r['t_start']:>8}ms  {r['path'].split('/')[-1][:26]:<26} ?{r['query'][:60]}")

    print("\n  --- /api/jobs 请求（全部）---")
    for r in [x for x in api if x["path"].endswith("/api/jobs")]:
        print(f"    {(r['ms_total'] or 0):>9.1f}ms  ttfb={(r['ms_to_resp'] or 0):>8.1f}ms  t_start={r['t_start']:>8}ms  "
              f"t_resp={r['t_resp']}  ?{r['query'][:80]}")

    print("\n  --- 媒体请求 ---")
    tot = sum(max(0, r["bytes"]) for r in media)
    print(f"    {len(media)} 个, 合计 {tot/1048576:.2f} MB")
    for r in sorted(media, key=lambda x: -(x["ms_total"] or 0))[:8]:
        print(f"    {(r['ms_total'] or 0):>9.1f}ms  {r['bytes']:>9}B  t={r['t_start']:>8}ms  {r['path'][-45:]}")

    print("\n  --- 静态资源 ---")
    print(f"    {len(static)} 个, 合计 {sum(max(0,r['bytes']) for r in static)/1048576:.2f} MB")

    out = {
        "action": args.action,
        "marks": marks,
        "counts": {"total": len(recs), "api": len(api), "media": len(media), "static": len(static)},
        "media_total_bytes": tot,
        "requests": recs,
        "console": console,
    }
    path = args.out or f"{OUT_DIR}/trace-all-{args.action}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
