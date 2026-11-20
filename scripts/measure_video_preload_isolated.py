#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**隔离**测量三种 `<video>` 写法的真实传输字节。

【为什么要隔离】上一版把测试用的 `<video>` 插进**已经开着媒体库**的页面，
于是「媒体库自己那 12 张卡」的请求被算进了测试组 —— 数字全污染
（测出 preload="none" 也省不下来，因为是媒体库在拉）。

这一版开一个**空白页**（about:blank + data URL），只放测试用的 video，
页面上**没有任何其它媒体**。

用法: python scripts/measure_video_preload_isolated.py --base http://127.0.0.1:8190
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
    "D_none_noposter": '<video src="{src}" controls preload="none"></video>',
}


def get_pairs(base, limit=6):
    with urllib.request.urlopen(base + "/api/jobs?status=done&page=1&page_size=12", timeout=180) as r:
        data = json.loads(r.read())
    pairs = []
    for j in data["jobs"]:
        for o in (j.get("outputs") or []):
            if o.get("kind") == "video":
                rel = str(o.get("file") or "").replace("\\", "/")
                if "/" not in rel:
                    rel = f"{j['id']}/{rel}"
                src = "/files/" + "/".join(urllib.parse.quote(s) for s in rel.split("/"))
                pairs.append((src, o.get("thumb") or ""))
    return pairs[:limit]


def measure(base, mode, tpl, pairs):
    recs = {}
    order = []
    t0 = time.perf_counter()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 800})
        pg = ctx.new_page()
        cdp = ctx.new_cdp_session(pg)
        cdp.send("Network.enable")

        def on_req(ev):
            rid = ev["requestId"]
            recs[rid] = {"url": ev["request"]["url"], "t": round((time.perf_counter() - t0) * 1000.0, 1),
                         "bytes": 0, "status": None, "failed": None}
            order.append(rid)

        def on_resp(ev):
            r = recs.get(ev["requestId"])
            if r:
                r["status"] = ev["response"]["status"]

        def on_data(ev):
            r = recs.get(ev["requestId"])
            if r:
                r["bytes"] += ev.get("dataLength", 0)

        def on_fail(ev):
            r = recs.get(ev["requestId"])
            if r:
                r["failed"] = ev.get("errorText", "")

        cdp.on("Network.requestWillBeSent", on_req)
        cdp.on("Network.responseReceived", on_resp)
        cdp.on("Network.dataReceived", on_data)
        cdp.on("Network.loadingFailed", on_fail)

        # 【踩坑 1】`about:blank` + `set_content` 下相对路径 `/files/...`
        #   解析不出来（origin 是 about:blank）=> 一个请求都没有。
        #   改成**绝对 URL**。
        # 【踩坑 2】直接 goto 线上首页会带上媒体库自己的 12 张卡 ->
        #   请求被污染。所以先 goto 拿到**同源** origin，
        #   再用 route 把「除 /files/ 以外的一切」全部 abort，
        #   最后 set_content 换成只有测试 video 的页面。
        pg.goto(base + "/api/generation-types", wait_until="domcontentloaded", timeout=120000)

        def route_handler(route):
            if "/files/" in route.request.url:
                route.continue_()
            else:
                route.abort()

        pg.route("**/*", route_handler)
        html = "<body style='margin:0;background:#111'>" + "".join(
            tpl.format(src=base + src, poster=(base + poster if poster else ""))
            for src, poster in pairs) + "</body>"
        pg.set_content(html)
        time.sleep(7)
        # 读一下 video 的元信息是否拿到（时长 / 首帧尺寸）
        meta = pg.evaluate(
            """() => [...document.querySelectorAll('video')].map(v => ({
                dur: v.duration, w: v.videoWidth, h: v.videoHeight, rs: v.readyState }))"""
        )
        pg.close()
        browser.close()

    media = [recs[r] for r in order if r in recs and "/files/" in recs[r]["url"]]
    total = sum(m["bytes"] for m in media)
    return {"mode": mode, "count": len(media), "bytes": total,
            "mb": round(total / 1048576, 2),
            "video_meta": meta,
            "detail": [{"url": m["url"][-34:], "bytes": m["bytes"], "status": m["status"],
                        "failed": m["failed"]} for m in media]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    pairs = get_pairs(base)
    print(f"测试 {len(pairs)} 个视频\n")
    results = {}
    for mode, tpl in MODES.items():
        r = measure(base, mode, tpl, pairs)
        results[mode] = r
        durs = [m.get("dur") for m in r["video_meta"]]
        ok_dur = [d for d in durs if d and d > 0]
        print(f"  {mode:<22} 请求 {r['count']:>3}  字节 {r['bytes']:>9} ({r['mb']:>6} MB)  "
              f"拿到时长的 {len(ok_dur)}/{len(durs)}")
    path = args.out or f"{OUT_DIR}/video-preload-isolated.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
