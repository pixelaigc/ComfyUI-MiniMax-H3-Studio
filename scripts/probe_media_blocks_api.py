#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并发实验：媒体下载期间 /api/jobs 会不会变慢。

背景：前端每 2 秒轮询一次 /api/jobs。若媒体文件传输（在 aiohttp 事件循环里
做文件读 + socket 写）把事件循环占住，**每一次轮询**都会被拖慢 —— 这正好
解释「有时候很慢很慢」。

做法：
  A. 空闲基线：连续 20 次 /api/jobs，记录 p50/p99
  B. 并发媒体：同时用 6 个连接拉 12 个卡片视频（Range: bytes=0-），
     期间连续 20 次 /api/jobs，记录 p50/p99
  C. 模拟浏览器首屏：先并发拉媒体，再立刻要 /api/jobs

用法: python scripts/probe_media_blocks_api.py --base http://127.0.0.1:8190
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")


def api_ms(base, path, timeout=60):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            r.read()
        return (time.perf_counter() - t0) * 1000.0
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000.0


def fetch_media(base, url, stop_after=None):
    """拉一个媒体文件（可带 Range），尽量读满。"""
    req = urllib.request.Request(base + url)
    if stop_after:
        req.add_header("Range", f"bytes=0-{stop_after}")
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                total += len(chunk)
    except Exception:  # noqa: BLE001
        pass
    return total


def stats(samples):
    s = sorted(samples)
    return {
        "n": len(s),
        "p50": round(statistics.median(s), 1),
        "p90": round(s[int(len(s) * 0.9) - 1], 1) if len(s) >= 10 else round(max(s), 1),
        "p99": round(s[-1], 1),
        "min": round(min(s), 1),
        "max": round(max(s), 1),
        "mean": round(statistics.mean(s), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    # 取一页真实卡片的媒体 URL
    with urllib.request.urlopen(base + "/api/jobs?status=done&page=1&page_size=12", timeout=60) as r:
        data = json.loads(r.read())
    media_urls = []
    for j in data["jobs"]:
        for o in (j.get("outputs") or []):
            if o.get("file"):
                rel = o["file"].replace("\\", "/")
                if not rel.startswith("/"):
                    rel = f"{j['id']}/{rel}"
                media_urls.append("/files/" + "/".join(urllib.parse.quote(s) for s in rel.split("/")))
    print(f"页面媒体 URL {len(media_urls)} 个")

    out = {}  # type: ignore[var-annotated]
    out["media_urls"] = media_urls

    # ---------- A. 空闲基线 ----------
    idle = [api_ms(base, "/api/jobs?status=done&page=1&page_size=12") for _ in range(args.samples)]
    out["A_idle"] = stats(idle)
    print(f"A 空闲基线       : {out['A_idle']}")

    # ---------- B. 媒体并发期间 ----------
    stop = threading.Event()
    media_samples = []
    media_bytes = []
    n_threads = 6

    def worker(url):
        b = fetch_media(base, url)
        media_bytes.append(b)

    def sampler():
        while not stop.is_set():
            media_samples.append(api_ms(base, "/api/jobs?status=done&page=1&page_size=12"))
            time.sleep(0.05)

    t = threading.Thread(target=sampler, daemon=True)
    t.start()
    threads = []
    for i in range(n_threads):
        u = media_urls[i % len(media_urls)]
        th = threading.Thread(target=worker, args=(u,))
        th.start()
        threads.append(th)
    for th in threads:
        th.join()
    stop.set()
    t.join(timeout=5)
    out["B_with_media"] = stats(media_samples) if media_samples else {}
    out["B_media_bytes_total"] = sum(media_bytes)
    print(f"B 媒体并发期间   : {out['B_with_media']}  (媒体字节 {sum(media_bytes)/1048576:.1f} MB)")

    # ---------- C. 媒体刚结束时立刻要 API ----------
    threads2 = []
    for i in range(n_threads):
        th = threading.Thread(target=worker, args=(media_urls[i % len(media_urls)],))
        th.start()
        threads2.append(th)
    for th in threads2:
        th.join()
    t0 = time.perf_counter()
    c_first = api_ms(base, "/api/jobs?status=done&page=1&page_size=12")
    out["C_immediately_after_media"] = {
        "first_after_ms": round(c_first, 1),
        "wall_ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }
    print(f"C 媒体结束后首问: {out['C_immediately_after_media']}")

    # ---------- D. 冷缓存场景：模拟 TTL 过期后的第一次列表请求 ----------
    # （服务端 300s TTL 的显示名映射；这里无法直接让它过期，
    #   所以改为记录连续多次请求，看是否有周期性尖峰）
    series = [api_ms(base, "/api/jobs?status=done&page=1&page_size=12") for _ in range(30)]
    out["D_series_30"] = {"samples": [round(x, 1) for x in series], "stats": stats(series)}
    print(f"D 连续 30 次     : {out['D_series_30']['stats']}")

    path = args.out or f"{OUT_DIR}/probe-media-blocks-api.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
