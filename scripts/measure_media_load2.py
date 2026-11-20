#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成媒体库「卡片载入慢」精确取证（第二版）。

上一版的问题：卡 DOM 1980ms 而 /api/jobs 只要 4ms —— 中间那 1976ms 到底花在哪？
这一版把时间轴拆成可归因的里程碑，并且**把媒体请求的传输量真实量出来**
（上一版 video 的 requestfinished 没触发，字节数记成 0）。

里程碑：
  T0  点标签/翻页
  T1  骨架出现（如果出现）
  T2  第一张真实卡片出现（.job-card[data-jid]）
  T3  全部卡片 DOM 就绪
  T4  全部媒体就绪（img.complete / video.readyState>=1 / audio.readyState>=1）
  T5  网络静默（无在途请求）

用法:
    python scripts/measure_media_load2.py --base http://127.0.0.1:8190 --tab done --page 1
    python scripts/measure_media_load2.py --base ... --scenario pageload   # 首屏
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


class Recorder:
    """按 request 对象身份记录，避免同 URL 多次请求互相覆盖。"""

    def __init__(self):
        self.recs = []
        self.by_req = {}
        self.t0 = time.perf_counter()

    def on_request(self, r):
        rec = {
            "rid": id(r),
            "t_start": round((time.perf_counter() - self.t0) * 1000.0, 1),
            "method": r.method,
            "url": r.url,
            "path": r.url.split("?")[0],
            "query": r.url.split("?")[1] if "?" in r.url else "",
            "rtype": r.resource_type,
            "bytes": 0,
            "status": None,
            "t_resp": None,
            "t_done": None,
            "range": r.headers.get("range"),
            "failed": None,
        }
        self.recs.append(rec)
        self.by_req[id(r)] = rec

    def on_response(self, r):
        rec = self.by_req.get(id(r.request))
        if rec is None:
            return
        rec["status"] = r.status
        rec["t_resp"] = round((time.perf_counter() - self.t0) * 1000.0, 1)
        rec["resp_headers"] = {
            k.lower(): v for k, v in r.headers.items()
            if k.lower() in ("content-length", "content-range", "cache-control", "etag", "content-type")
        }

    def on_finished(self, r):
        # requestfinished 事件给的是 **Request** 本身（不是 Response）——
        # 上一版写 r.request 直接抛 AttributeError 把整个测量打断。
        rec = self.by_req.get(id(r))
        if rec is None:
            return
        rec["t_done"] = round((time.perf_counter() - self.t0) * 1000.0, 1)
        try:
            rec["bytes"] = len(r.body())
        except Exception:  # noqa: BLE001
            rec["bytes"] = -1   # -1 = 拿不到（流式/被取消）

    def on_failed(self, r):
        rec = self.by_req.get(id(r))
        if rec is not None:
            rec["failed"] = r.failure


def run(base, tab, page, scenario, shot, wait_media_ms):
    rec = Recorder()
    console_errors = []
    marks = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        pg.on("request", rec.on_request)
        pg.on("response", rec.on_response)
        pg.on("requestfinished", rec.on_finished)
        pg.on("requestfailed", rec.on_failed)
        pg.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        pg.goto(base + "/", wait_until="domcontentloaded", timeout=180000)
        marks["domcontentloaded"] = round((time.perf_counter() - rec.t0) * 1000.0, 1)

        pg.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=180000)
        marks["js_ready"] = round((time.perf_counter() - rec.t0) * 1000.0, 1)

        if scenario == "pageload":
            # 首屏：等第一次真实卡片自己出现
            t_wait = time.perf_counter()
            try:
                pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
            except Exception as e:  # noqa: BLE001
                print("!! 首屏等卡片超时:", e)
            marks["first_cards_ms_after_js"] = round((time.perf_counter() - t_wait) * 1000.0, 1)
        else:
            t_click = time.perf_counter()
            if tab:
                pg.click(f".jobs-tabs button[data-st='{tab}']", timeout=30000)
                marks["after_tab_click"] = round((time.perf_counter() - t_click) * 1000.0, 1)
            if page and page > 1:
                pg.click(f"#jobsPagination button[data-page='{page}']", timeout=30000)
                marks["after_page_click"] = round((time.perf_counter() - t_click) * 1000.0, 1)
            t0_click = time.perf_counter()

            # T1 骨架
            try:
                pg.wait_for_selector(".job-skeleton", timeout=4000)
                marks["tt_skeleton_ms"] = round((time.perf_counter() - t0_click) * 1000.0, 1)
            except Exception:  # noqa: BLE001
                marks["tt_skeleton_ms"] = None
            # T2 第一张真实卡
            try:
                pg.wait_for_selector(".job-card[data-jid]", timeout=180000)
                marks["tt_first_card_ms"] = round((time.perf_counter() - t0_click) * 1000.0, 1)
            except Exception as e:  # noqa: BLE001
                print("!! 等卡片超时:", e)
                marks["tt_first_card_ms"] = None
            # T3 全部卡片 DOM
            try:
                pg.wait_for_function(
                    """() => {
                        const g = document.querySelector('#jobsGrid');
                        if (!g) return false;
                        const real = [...g.querySelectorAll('.job-card[data-jid]')];
                        const skel = [...g.querySelectorAll('.job-skeleton')];
                        return real.length > 0 && skel.length === 0;
                    }""",
                    timeout=180000,
                )
                marks["tt_all_cards_ms"] = round((time.perf_counter() - t0_click) * 1000.0, 1)
            except Exception as e:  # noqa: BLE001
                print("!! 等全部卡片超时:", e)
                marks["tt_all_cards_ms"] = None

        # T4 媒体就绪
        t_media = time.perf_counter()
        try:
            pg.wait_for_function(
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
                        return true;
                    });
                }""",
                timeout=wait_media_ms,
            )
        except Exception as e:  # noqa: BLE001
            print("!! 等媒体就绪超时:", e)
        marks["tt_media_ready_ms"] = round((time.perf_counter() - t_media) * 1000.0, 1)

        # T5 网络静默
        t_idle = time.perf_counter()
        try:
            pg.wait_for_load_state("networkidle", timeout=60000)
            marks["tt_networkidle_ms"] = round((time.perf_counter() - t_idle) * 1000.0, 1)
        except Exception:  # noqa: BLE001
            marks["tt_networkidle_ms"] = None

        if shot:
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            pg.screenshot(path=shot, full_page=False)

        # 页面内状态（确认没被重置）
        page_state = pg.evaluate(
            """() => {
                const grid = document.querySelector('#jobsGrid');
                const activeTab = document.querySelector('.jobs-tabs button.active[data-st]');
                return {
                    active_tab: activeTab ? activeTab.dataset.st : null,
                    card_count: grid ? grid.querySelectorAll('.job-card[data-jid]').length : 0,
                    scroll_top: window.scrollY,
                    media_bytes_hint: null,
                };
            }"""
        )
        pg.close()
        browser.close()

    api = [r for r in rec.recs if "/api/jobs" in r["path"]]
    media = [r for r in rec.recs if "/files/" in r["path"]]
    other = [r for r in rec.recs if r not in api and r not in media]

    # 峰值并发（在途媒体请求）
    events = []
    for r in media:
        s = r["t_start"]
        e = r["t_done"] if r["t_done"] is not None else (r["t_resp"] or s) + 500
        events.append((s, 1))
        events.append((e, -1))
    cur = peak = 0
    for _, d in sorted(events):
        cur += d
        peak = max(peak, cur)

    by_ext = defaultdict(int)
    cnt_ext = defaultdict(int)
    for r in media:
        ext = r["path"].rsplit(".", 1)[-1].lower() if "." in r["path"] else "?"
        by_ext[ext] += max(0, r["bytes"])
        cnt_ext[ext] += 1

    return {
        "scenario": scenario,
        "tab": tab,
        "page": page,
        "marks": marks,
        "page_state": page_state,
        "api_jobs": [
            {"query": r["query"][:120], "ms": (r["t_done"] - r["t_start"]) if r["t_done"] else None,
             "bytes": r["bytes"], "status": r["status"]}
            for r in api
        ],
        "api_jobs_count": len(api),
        "media_count": len(media),
        "media_total_bytes": sum(max(0, r["bytes"]) for r in media),
        "media_peak_concurrency": peak,
        "media_bytes_by_ext": dict(by_ext),
        "media_count_by_ext": dict(cnt_ext),
        "media_range_headers": [
            {"path": r["path"][-50:], "range": r["range"], "bytes": r["bytes"], "status": r["status"],
             "content_range": (r.get("resp_headers") or {}).get("content-range"),
             "cache_control": (r.get("resp_headers") or {}).get("cache-control")}
            for r in media
        ],
        "other_count": len(other),
        "console_errors": console_errors,
        "screenshot": shot,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--tab", default="done")
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--scenario", default="switch", choices=["switch", "pageload"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--shot", default=None)
    ap.add_argument("--wait-media-ms", type=int, default=180000)
    args = ap.parse_args()

    shot = args.shot or f"{OUT_DIR}/{args.scenario}-{args.tab}-p{args.page}.png"
    res = run(args.base, args.tab, args.page, args.scenario, shot, args.wait_media_ms)

    print(f"=== 场景 {args.scenario} tab={args.tab} page={args.page} ===")
    print("  里程碑(ms):", json.dumps(res["marks"], ensure_ascii=False))
    print(f"  /api/jobs 请求数 : {res['api_jobs_count']}")
    for a in res["api_jobs"]:
        print(f"      {a['ms']}ms  {a['bytes']}B  ?{a['query'][:90]}")
    print(f"  媒体请求数       : {res['media_count']}")
    print(f"  媒体总字节       : {res['media_total_bytes']/1048576:.2f} MB")
    print(f"  媒体峰值并发     : {res['media_peak_concurrency']}")
    print(f"  按扩展名         : {res['media_bytes_by_ext']}  计数 {res['media_count_by_ext']}")
    print(f"  其它请求数       : {res['other_count']}")
    print(f"  控制台错误       : {len(res['console_errors'])} {res['console_errors'][:3]}")
    print(f"  页面状态         : {res['page_state']}")
    print("  媒体请求明细（前 14 条）:")
    for m in res["media_range_headers"][:14]:
        print(f"      {m['bytes']:>9}B  range={m['range']}  {m['path']}")

    out = args.out or f"{OUT_DIR}/{args.scenario}-{args.tab}-p{args.page}.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {out}")


if __name__ == "__main__":
    main()
