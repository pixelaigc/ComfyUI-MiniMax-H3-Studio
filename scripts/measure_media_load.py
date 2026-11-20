#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成媒体库「卡片载入慢」实测取证。

测什么：
  1. 列表接口 /api/jobs 的响应耗时（不同标签/页码/每页条数）
  2. 卡片媒体请求：请求的是原图还是缩略图？字节数？耗时？并发数？
  3. 首屏「卡片可交互」耗时（从进入媒体库到卡片图片/视频首帧就绪）
  4. 服务端是否被同步调用堵住（同一时刻另一个面板的请求是否变慢）

用法:
    python scripts/measure_media_load.py --base http://127.0.0.1:8190 --tag done
    python scripts/measure_media_load.py --base http://127.0.0.1:8190 --tag all --page 2
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evidence")


def http_ms(url, timeout=120):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
        return (time.perf_counter() - t0) * 1000.0, len(body), r.status
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000.0, 0, getattr(e, "code", 0)


def api_timings(base: str, cases) -> list:
    rows = []
    for label, q in cases:
        # 每种场景跑 3 次取中位数 —— 单次值会被噪声带偏
        samples = []
        size = 0
        status = 0
        for _ in range(3):
            ms, n, st = http_ms(f"{base}/api/jobs?{q}")
            samples.append(ms)
            size = n
            status = st
        rows.append({
            "label": label,
            "query": q,
            "ms_min": round(min(samples), 1),
            "ms_median": round(statistics.median(samples), 1),
            "ms_max": round(max(samples), 1),
            "bytes": size,
            "http": status,
        })
    return rows


def browser_trace(base: str, tab: str, page: int, page_size: int, shot: str | None):
    from playwright.sync_api import sync_playwright

    reqs = []          # 全部请求
    console_errors = []
    t0 = time.perf_counter()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        page_obj = ctx.new_page()

        def on_request(r):
            reqs.append({
                "t_start": round((time.perf_counter() - t0) * 1000.0, 1),
                "method": r.method,
                "url": r.url,
                "type": r.resource_type,
                "bytes": 0,
                "status": None,
                "ms": None,
                "finished": False,
            })

        def on_response(r):
            for rec in reqs:
                if rec["url"] == r.url and rec["status"] is None:
                    rec["status"] = r.status
                    try:
                        rec["bytes"] = len(r.body())
                    except Exception:  # noqa: BLE001
                        rec["bytes"] = 0
                    break

        def on_finished(r):
            for rec in reqs:
                if rec["url"] == r.url and rec["ms"] is None:
                    rec["ms"] = round((time.perf_counter() - t0) * 1000.0, 1) - rec["t_start"]
                    rec["finished"] = True
                    break

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text)

        page_obj.on("request", on_request)
        page_obj.on("response", on_response)
        page_obj.on("requestfinished", on_finished)
        page_obj.on("console", on_console)

        page_obj.goto(base + "/", wait_until="domcontentloaded", timeout=120000)

        # 等前端起好
        page_obj.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=120000)

        # 切到目标标签 + 目标页 —— **走真实 UI 路径**（点标签按钮 / 点分页按钮），
        # 不走内部变量。原因：app.js 的 `state` 是**模块级 const**，从来没挂到
        # window 上（全仓 grep `window.state =` 命中 0 处），
        # 而且真实用户路径才是我们要测量的对象。
        t_click = time.perf_counter()
        tab_sel = f".jobs-tabs button[data-st='{tab}']"
        try:
            page_obj.click(tab_sel, timeout=30000)
        except Exception as e:  # noqa: BLE001
            print("!! 点标签失败:", e)
        if page > 1:
            try:
                page_obj.click(f"#jobsPagination button[data-page='{page}']", timeout=30000)
            except Exception as e:  # noqa: BLE001
                print("!! 点分页失败:", e)
        # 等真实卡片（骨架卡没有 data-jid）
        try:
            page_obj.wait_for_selector(".job-card[data-jid]", timeout=120000)
        except Exception as e:  # noqa: BLE001
            print("!! 等待卡片超时:", e)
        t_cards = time.perf_counter()

        # 等媒体真正就绪（img complete / video 首帧 / audio metadata）
        try:
            page_obj.wait_for_function(
                """() => {
                    const cards = [...document.querySelectorAll('.job-card[data-jid]')];
                    if (!cards.length) return false;
                    return cards.every(c => {
                        const img = c.querySelector('img');
                        if (img) return img.complete && img.naturalWidth > 0;
                        const v = c.querySelector('video');
                        if (v) return v.readyState >= 1;
                        const a = c.querySelector('audio');
                        if (a) return a.readyState >= 1;
                        return true;   // 无媒体的卡（失败/进行中）不算阻塞
                    });
                }""",
                timeout=180000,
            )
        except Exception as e:  # noqa: BLE001
            print("!! 等待媒体就绪超时:", e)
        t_media = time.perf_counter()

        if shot:
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            page_obj.screenshot(path=shot, full_page=False)

        page_obj.close()
        browser.close()

    api = [r for r in reqs if "/api/jobs" in r["url"]]
    media = [r for r in reqs if "/files/" in r["url"]]
    other = [r for r in reqs if r not in api and r not in media]

    # 并发数：任意时刻在途的媒体请求峰值
    peak = 0
    cur = 0
    events = []
    for r in media:
        events.append((r["t_start"], 1))
        events.append((r["t_start"] + (r["ms"] or 0), -1))
    for _, d in sorted(events):
        cur += d
        peak = max(peak, cur)

    return {
        "tab": tab,
        "page": page,
        "page_size": page_size,
        "tt_cards_ms": round((t_cards - t_click) * 1000.0, 1),
        "tt_media_ready_ms": round((t_media - t_click) * 1000.0, 1),
        "api_requests": [
            {"url": r["url"].split("?")[-1][:120], "ms": r["ms"], "bytes": r["bytes"], "status": r["status"]}
            for r in api
        ],
        "media_request_count": len(media),
        "media_total_bytes": sum(r["bytes"] for r in media),
        "media_peak_concurrency": peak,
        "media_bytes_by_ext": {
            ext: sum(r["bytes"] for r in media if r["url"].split("?")[0].endswith(ext))
            for ext in sorted({r["url"].split("?")[0].split(".")[-1].lower() for r in media})
        },  # type: ignore[dict-item]
        "media_slowest": sorted(
            [{"url": r["url"][-70:], "ms": r["ms"], "bytes": r["bytes"], "status": r["status"]} for r in media],
            key=lambda x: -(x["ms"] or 0),
        )[:8],
        "other_request_count": len(other),
        "console_errors": console_errors,
        "screenshot": shot,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--tab", default="done")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--page-size", type=int, default=12)
    ap.add_argument("--out", default=None)
    ap.add_argument("--shot", default=None)
    ap.add_argument("--skip-browser", action="store_true")
    args = ap.parse_args()

    cases = [
        ("全部/第1页/12条", "status=all&page=1&page_size=12"),
        ("已完成/第1页/12条", "status=done&page=1&page_size=12"),
        ("已完成/第5页/12条", "status=done&page=5&page_size=12"),
        ("已完成/第1页/60条", "status=done&page=1&page_size=60"),
        ("进行中/第1页/12条", "status=active&page=1&page_size=12"),
        ("收藏/第1页/12条", "favorites=1&page=1&page_size=12"),
    ]
    print("=== 1) 列表接口耗时（每场景跑 3 次，取中位数）===")
    rows = api_timings(args.base, cases)
    for r in rows:
        print(f"  {r['label']:<22} 中位 {r['ms_median']:>7.1f}ms  "
              f"(min {r['ms_min']} / max {r['ms_max']})  {r['bytes']}B  HTTP {r['http']}")

    result = {"api_timings": rows}

    if not args.skip_browser:
        print("\n=== 2) 浏览器网络取证 ===")
        shot = args.shot or f"{OUT_DIR}/media-perf/media-load-{args.tab}-p{args.page}.png"
        bt = browser_trace(args.base, args.tab, args.page, args.page_size, shot)
        result["browser"] = bt
        print(f"  标签={bt['tab']} 页={bt['page']} 每页={bt['page_size']}")
        print(f"  卡片 DOM 就绪      : {bt['tt_cards_ms']} ms")
        print(f"  媒体全部就绪       : {bt['tt_media_ready_ms']} ms")
        print(f"  媒体请求数         : {bt['media_request_count']}")
        print(f"  媒体总字节         : {bt['media_total_bytes']/1048576:.2f} MB")
        print(f"  媒体峰值并发       : {bt['media_peak_concurrency']}")
        print(f"  按扩展名字节       : {bt['media_bytes_by_ext']}")
        print(f"  最慢的 8 个媒体请求:")
        for s in bt["media_slowest"]:
            print(f"     {s['ms']}ms  {s['bytes']/1024:.0f}KB  {s['url']}")
        print(f"  控制台错误         : {len(bt['console_errors'])} {bt['console_errors'][:3]}")
        print(f"  截图               : {bt['screenshot']}")

    out = args.out or f"{OUT_DIR}/media-perf/measure-{args.tab}-p{args.page}.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {out}")


if __name__ == "__main__":
    main()
