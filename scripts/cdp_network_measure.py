#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 CDP 精确量网络：真实传输字节、请求数、时序。

为什么不用 Playwright 的 requestfinished：
  视频请求 Chrome 会**主动取消**（`Range: bytes=0-` 拿够 metadata 就断开），
  这时 Playwright 的 `r.body()` 抛异常，字节数记成 -1/0 —— 量不准。
  CDP 的 `Network.loadingFinished` 给的是**线上真实编码字节数**（encodedDataLength）。

产出：网络面板级证据（请求数 / 字节 / 状态码 / 时序）+ 渲染截图。

用法:
    python scripts/cdp_network_measure.py --base http://127.0.0.1:8190 --tab done --page 1
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--tab", default="done")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--scenario", default="switch", choices=["switch", "pageload"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--shot", default=None)
    ap.add_argument("--settle-ms", type=int, default=8000)
    args = ap.parse_args()

    reqs = {}          # requestId -> rec
    order = []
    console = []
    t0 = time.perf_counter()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        cdp = ctx.new_cdp_session(pg)
        cdp.send("Network.enable")

        def on_req(ev):
            r = ev["request"]
            rid = ev["requestId"]
            reqs[rid] = {
                "rid": rid, "url": r["url"], "method": r["method"],
                "type": ev.get("type"), "t_start": round((time.perf_counter() - t0) * 1000.0, 1),
                "status": None, "encoded": 0, "t_resp": None, "t_end": None,
                "from_cache": ev.get("responseFromDiskCache"),
                "headers": {k.lower(): v for k, v in (r.get("headers") or {}).items()
                            if k.lower() in ("range",)},
            }
            order.append(rid)

        def on_resp(ev):
            rec = reqs.get(ev["requestId"])
            if rec:
                rec["status"] = ev["response"]["status"]
                rec["t_resp"] = round((time.perf_counter() - t0) * 1000.0, 1)
                h = {k.lower(): v for k, v in (ev["response"].get("headers") or {}).items()}
                rec["resp_cache_control"] = h.get("cache-control")
                rec["resp_from_cache"] = ev["response"].get("fromDiskCache")

        def on_finish(ev):
            rec = reqs.get(ev["requestId"])
            if rec:
                rec["encoded"] = ev.get("encodedDataLength", 0)
                rec["t_end"] = round((time.perf_counter() - t0) * 1000.0, 1)

        cdp.on("Network.requestWillBeSent", on_req)
        cdp.on("Network.responseReceived", on_resp)
        cdp.on("Network.loadingFinished", on_finish)

        pg.on("console", lambda m: console.append({"type": m.type, "text": m.text}))

        pg.goto(args.base + "/", wait_until="domcontentloaded", timeout=180000)
        pg.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=180000)
        marks = {"js_ready": round((time.perf_counter() - t0) * 1000.0, 1)}

        if args.scenario == "switch":
            tc = time.perf_counter()
            pg.click(f".jobs-tabs button[data-st='{args.tab}']", timeout=30000)
            marks["tab_click_ms"] = round((time.perf_counter() - tc) * 1000.0, 1)
            try:
                pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
                marks["tt_first_card_ms"] = round((time.perf_counter() - tc) * 1000.0, 1)
            except Exception as e:  # noqa: BLE001
                print("!! 等卡片超时:", e)
                marks["tt_first_card_ms"] = -1
            if args.page > 1:
                pg.click(f"#jobsPagination button[data-page='{args.page}']", timeout=30000)
                marks["tt_page_click_ms"] = round((time.perf_counter() - tc) * 1000.0, 1)
                try:
                    pg.wait_for_function(
                        """() => {
                            const g = document.querySelector('#jobsGrid');
                            return g && g.querySelectorAll('.job-card[data-jid]').length > 0
                                   && !g.querySelector('.job-skeleton');
                        }""", timeout=180000)
                    marks["tt_page_cards_ms"] = round((time.perf_counter() - tc) * 1000.0, 1)
                except Exception as e:  # noqa: BLE001
                    print("!! 等翻页卡片超时:", e)
        else:
            try:
                pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
                marks["tt_first_card_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
            except Exception as e:  # noqa: BLE001
                print("!! 等卡片超时:", e)

        # 让媒体下载跑完
        time.sleep(args.settle_ms / 1000.0)
        marks["total_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

        shot = args.shot or f"{OUT_DIR}/cdp-{args.scenario}-{args.tab}-p{args.page}.png"
        os.makedirs(os.path.dirname(shot), exist_ok=True)
        pg.screenshot(path=shot, full_page=False)

        page_state = pg.evaluate(
            """() => ({
                active_tab: (document.querySelector('.jobs-tabs button.active[data-st]')||{}).dataset?.st || null,
                card_count: document.querySelectorAll('.job-card[data-jid]').length,
                scroll_top: window.scrollY,
                page_size_select: (document.querySelector('#donePageSize')||{}).value || null,
            })"""
        )
        pg.close()
        browser.close()

    recs = [reqs[r] for r in order if r in reqs]
    api = [r for r in recs if "/api/" in r["url"]]
    media = [r for r in recs if "/files/" in r["url"]]
    static = [r for r in recs if r not in api and r not in media]

    def summarize(group):
        return {
            "count": len(group),
            "bytes": sum(r["encoded"] for r in group),
            "mb": round(sum(r["encoded"] for r in group) / 1048576, 2),
        }

    by_ext = defaultdict(int)
    cnt_ext = defaultdict(int)
    for r in media:
        path = r["url"].split("?")[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else "?"
        by_ext[ext] += r["encoded"]
        cnt_ext[ext] += 1

    print(f"=== {args.scenario} tab={args.tab} page={args.page} ===")
    print("  marks:", json.dumps(marks, ensure_ascii=False))
    print(f"  请求: 总 {len(recs)}  api {len(api)}  media {len(media)}  static {len(static)}")
    print(f"  字节: api {summarize(api)['mb']} MB  media {summarize(media)['mb']} MB  "
          f"static {summarize(static)['mb']} MB")
    print(f"  媒体按扩展名: {dict(by_ext)}  计数 {dict(cnt_ext)}")
    print("  /api/jobs:")
    for r in api:
        if "/api/jobs" in r["url"]:
            dur = (r["t_end"] - r["t_start"]) if r["t_end"] else None
            print(f"      {dur:>8.1f}ms  {r['encoded']:>8}B  t={r['t_start']:>8}ms  "
                  f"?{r['url'].split('?')[1][:70] if '?' in r['url'] else ''}")
    print("  媒体明细:")
    for r in sorted(media, key=lambda x: -(x["encoded"])):
        dur = (r["t_end"] - r["t_start"]) if r["t_end"] else None
        print(f"      {dur:>8.1f}ms  {r['encoded']:>9}B  range={r['headers'].get('range')}  "
              f"{r['url'].split('?')[0][-45:]}")
    print(f"  控制台错误: {len([c for c in console if c['type']=='error'])}")

    out = {
        "scenario": args.scenario, "tab": args.tab, "page": args.page,
        "marks": marks, "page_state": page_state, "screenshot": shot,
        "counts": {"total": len(recs), "api": len(api), "media": len(media), "static": len(static)},
        "bytes": {"api": summarize(api), "media": summarize(media), "static": summarize(static)},
        "media_bytes_by_ext": dict(by_ext), "media_count_by_ext": dict(cnt_ext),
        "requests": recs, "console": console,
    }
    path = args.out or f"{OUT_DIR}/cdp-{args.scenario}-{args.tab}-p{args.page}.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
