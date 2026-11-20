#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""区分三种「拖慢 /api/jobs」的可能机制。

M1 事件循环被同步代码阻塞   -> 症状：所有请求的 TTFB 一起抬高
M2 GIL 被 CPU 密集线程抢占   -> 症状：事件循环在跑，但线程拿不到 GIL
M3 浏览器同源连接数排队      -> 症状：只有浏览器里的请求被拖慢，curl 不受影响

做法：
  A. 单发一个慢端点，同时采样 /api/jobs（每次用**新连接**，
     排除 M3 的连接排队因素）
  B. 在慢端点请求进行中，用 py-spy / 线程栈采样看事件循环在干什么
  C. 浏览器里同时开 1 个慢端点请求 + 1 个 /api/jobs，对比
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

JOBS = "/api/jobs?status=done&page=1&page_size=12"


def get(base, path, timeout=180):
    t0 = time.perf_counter()
    req = urllib.request.Request(base + path)
    # 每次新建连接：排除「同源连接排队」
    opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
        return (time.perf_counter() - t0) * 1000.0, len(body), None
    except Exception as e:  # noqa: BLE001
        return (time.perf_counter() - t0) * 1000.0, 0, str(e)


def st(xs):
    s = sorted(xs)
    return {"n": len(s), "p50": round(statistics.median(s), 1),
            "max": round(max(s), 1), "mean": round(statistics.mean(s), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    out = {}

    # ---------- A. 空闲基线 ----------
    idle = [get(base, JOBS)[0] for _ in range(15)]
    out["A_idle"] = st(idle)
    print(f"A 空闲基线             : {out['A_idle']}")

    # ---------- B. 单发 video-options，同时采样 ----------
    for label, path in [("video-options", "/api/video-options"),
                        ("image-loras(Z-Image)", "/api/image-loras?base=Z-Image")]:
        samples = []
        slow_ms = [None]
        stop = threading.Event()

        def sampler():
            while not stop.is_set():
                samples.append(get(base, JOBS)[0])
                time.sleep(0.01)

        def slow():
            slow_ms[0] = get(base, path)[0]

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        t0 = time.perf_counter()
        w = threading.Thread(target=slow)
        w.start()
        w.join()
        stop.set()
        th.join(timeout=5)
        out[f"B_{label}"] = {"slow_endpoint_ms": round(slow_ms[0] or 0, 1), "jobs": st(samples)}
        print(f"B {label:<20}: 端点 {(slow_ms[0] or 0):>8.1f}ms | /api/jobs {out[f'B_{label}']['jobs']}")

    # ---------- C. 同步调用（同一线程）测 GIL 抢占 ----------
    # 在主线程发起慢请求的同时，用**另一个进程**采样（排除同进程 GIL 干扰）
    # 这里简化：直接记录慢请求期间，主线程调 /api/jobs 的耗时
    for label, path in [("video-options-sync", "/api/video-options")]:
        t0 = time.perf_counter()
        # 后台线程发起慢请求
        th = threading.Thread(target=lambda: get(base, path))
        th.start()
        time.sleep(0.02)
        samples = []
        while th.is_alive():
            samples.append(get(base, JOBS)[0])
        th.join()
        out[f"C_{label}"] = {"jobs": st(samples)}
        print(f"C {label:<20}: /api/jobs {out[f'C_{label}']['jobs']}")

    # ---------- D. ComfyUI 本身是否在忙 ----------
    t0 = time.perf_counter()
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:8188/queue", timeout=10) as r:
            q = json.loads(r.read())
        out["D_comfy_queue"] = {"ms": round((time.perf_counter() - t0) * 1000.0, 1),
                                "queue_running": len(q.get("queue_running") or []),
                                "queue_pending": len(q.get("queue_pending") or [])}
    except Exception as e:  # noqa: BLE001
        out["D_comfy_queue"] = {"error": str(e)}
    print(f"D ComfyUI 队列         : {out['D_comfy_queue']}")

    # ---------- E. ComfyUI 模型清单接口本身 ----------
    for ep in ["/models/diffusion_models", "/models/loras", "/object_info/UNETLoader"]:
        ms, n, err = get("http://127.0.0.1:8188", ep, timeout=120)
        out.setdefault("E_comfy_endpoints", {})[ep] = {"ms": round(ms, 1), "bytes": n, "err": err}
        print(f"E ComfyUI {ep:<30}: {ms:>8.1f}ms  {n}B")

    path = args.out or f"{OUT_DIR}/probe-mechanism.json"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n证据: {path}")


if __name__ == "__main__":
    main()
