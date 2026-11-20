#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""并发归因实验：谁把 /api/jobs 拖慢了？

浏览器实测：首屏 25 个 API 请求**同时**发出，/api/jobs 自己要 707ms；
但单独 curl /api/jobs 只要 4.7ms。⇒ 慢的不是 /api/jobs 自己，
是**同一时刻别的接口把事件循环/GIL/磁盘占住了**。

本脚本逐个候选端点做「并发压测」：
  对每个候选 E：
    1. 先测 /api/jobs 的空闲基线（20 次）
    2. 起 4 个 E 的并发请求
    3. 期间连续测 /api/jobs（20 次）
    4. 对比 p50 / p99 抬升倍数

用法: python scripts/probe_api_contention.py --base http://127.0.0.1:8190
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")

JOBS_PATH = "/api/jobs?status=done&page=1&page_size=12"

CANDIDATES = [
    ("image-loras(Z-Image)", "/api/image-loras?base=Z-Image"),
    ("image-loras(MOSS)", "/api/image-loras?base=MOSS-Image"),
    ("image-loras(Qwen)", "/api/image-loras?base=Qwen-Image%202.1"),
    ("video-options", "/api/video-options"),
    ("image-options", "/api/image-options"),
    ("music-options", "/api/music-options"),
    ("generation-types", "/api/generation-types"),
    ("system", "/api/system"),
    ("agent/sessions", "/api/agent/sessions?limit=200"),
    ("video-effect-loras", "/api/video-effect-loras?model=fl2va_pruned_int8"),
    ("video-config", "/api/video-config"),
]


def timed_get(base, path, timeout=120):
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            n = len(r.read())
        return (time.perf_counter() - t0) * 1000.0, n
    except Exception:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000.0, 0


def stats(xs):
    s = sorted(xs)
    return {
        "n": len(s),
        "p50": round(statistics.median(s), 1),
        "p90": round(s[int(len(s) * 0.9) - 1], 1) if len(s) >= 10 else round(max(s), 1),
        "max": round(max(s), 1),
        "mean": round(statistics.mean(s), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--samples", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    base_samples = [timed_get(base, JOBS_PATH)[0] for _ in range(args.samples)]
    base_stats = stats(base_samples)
    print(f"空闲基线 /api/jobs : {base_stats}\n")

    results = {"idle_baseline": base_stats, "concurrency": args.concurrency, "cases": {}}

    for label, path in CANDIDATES:
        stop = threading.Event()
        samples = []
        c_times = []

        def sampler():
            while not stop.is_set():
                samples.append(timed_get(base, JOBS_PATH)[0])
                time.sleep(0.02)

        def hammer():
            t0 = time.perf_counter()
            timed_get(base, path)
            c_times.append((time.perf_counter() - t0) * 1000.0)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        workers = [threading.Thread(target=hammer) for _ in range(args.concurrency)]
        t_start = time.perf_counter()
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        stop.set()
        th.join(timeout=5)

        st = stats(samples) if samples else {}
        ratio = round(st.get("p50", 0) / base_stats["p50"], 1) if st else 0
        results["cases"][label] = {
            "path": path,
            "concurrent_endpoint_ms": round(statistics.median(c_times), 1) if c_times else None,
            "jobs_under_load": st,
            "p50_slowdown_x": ratio,
        }
        print(f"  {label:<22} 端点自身 {results['cases'][label]['concurrent_endpoint_ms']:>8.1f}ms  "
              f"| /api/jobs p50 {st.get('p50')}ms (x{ratio})  max {st.get('max')}ms")

    # 复测空闲基线（确认不是负载残留）
    after = stats([timed_get(base, JOBS_PATH)[0] for _ in range(args.samples)])
    results["idle_after"] = after
    print(f"\n压测后空闲基线   : {after}")

    path = args.out or f"{OUT_DIR}/probe-api-contention.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
