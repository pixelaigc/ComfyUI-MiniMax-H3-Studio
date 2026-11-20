#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对比三种 `<video>` 写法的**真实传输字节**（CDP Network.dataReceived）。

写法：
  A. preload="metadata"                      （原始实现）
  B. preload="metadata" + poster=缩略图
  C. preload="none"    + poster=缩略图

产出：每种写法的媒体字节 / 请求数 / 首个卡片就绪耗时。

用法: python scripts/measure_video_preload_modes.py --base http://127.0.0.1:8190
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")

MODES = {
    "A_metadata_only": '<video src="{src}" controls preload="metadata"></video>',
    "B_metadata_poster": '<video src="{src}" poster="{poster}" controls preload="metadata"></video>',
    "C_none_poster": '<video src="{src}" poster="{poster}" controls preload="none"></video>',
}


def measure(base, mode, html_tpl, out_dir):
    recs = {}
    order = []
    t0 = time.perf_counter()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        cdp = ctx.new_cdp_session(pg)
        cdp.send("Network.enable")

        def on_req(ev):
            rid = ev["requestId"]
            recs[rid] = {"url": ev["request"]["url"], "rid": rid,
                         "t": round((time.perf_counter() - t0) * 1000.0, 1),
                         "bytes": 0, "status": None}
            order.append(rid)

        def on_resp(ev):
            r = recs.get(ev["requestId"])
            if r:
                r["status"] = ev["response"]["status"]

        def on_data(ev):
            r = recs.get(ev["requestId"])
            if r:
                r["bytes"] += ev.get("dataLength", 0)

        cdp.on("Network.requestWillBeSent", on_req)
        cdp.on("Network.responseReceived", on_resp)
        cdp.on("Network.dataReceived", on_data)

        pg.goto(base + "/", wait_until="domcontentloaded", timeout=180000)
        pg.wait_for_function("() => typeof window.refreshJobs === 'function'", timeout=180000)

        # 取一页真实产物的 file + thumb
        with urllib.request.urlopen(base + "/api/jobs?status=done&page=1&page_size=12", timeout=180) as r:
            data = json.loads(r.read())
        pairs = []
        for j in data["jobs"]:
            for o in (j.get("outputs") or []):
                if o.get("kind") == "video" and o.get("thumb"):
                    rel = str(o.get("file") or "").replace("\\", "/")
                    if "/" not in rel:
                        rel = f"{j['id']}/{rel}"
                    src = "/files/" + "/".join(urllib.parse.quote(s) for s in rel.split("/"))
                    pairs.append((src, o["thumb"]))
        if not pairs:
            browser.close()
            return None

        # 在页面里造一个隔离的测试区（不干扰真实媒体库），放 6 个视频卡
        html = "<div id='__perftest'>" + "".join(
            html_tpl.format(src=src, poster=poster) for src, poster in pairs[:6]) + "</div>"
        pg.evaluate("h => { document.body.insertAdjacentHTML('beforeend', h); }", html)
        # 记录注入时刻，之后只统计注入之后的媒体请求
        t_inject = time.perf_counter()
        time.sleep(6)
        marks = {"inject_to_end_ms": round((time.perf_counter() - t_inject) * 1000.0, 1)}
        pg.close()
        browser.close()

    media = [recs[r] for r in order if r in recs and "/files/" in recs[r]["url"]]
    total = sum(m["bytes"] for m in media)
    return {"mode": mode, "count": len(media), "bytes": total,
            "mb": round(total / 1048576, 2), "marks": marks,
            "detail": [{"url": m["url"][-40:], "bytes": m["bytes"], "status": m["status"]}
                       for m in media]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    results = {}
    for mode, tpl in MODES.items():
        r = measure(args.base, mode, tpl, OUT_DIR)
        if r:
            results[mode] = r
            print(f"  {mode:<22} 请求 {r['count']:>3}  字节 {r['bytes']:>9}  ({r['mb']} MB)")
    path = args.out or f"{OUT_DIR}/video-preload-modes.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
