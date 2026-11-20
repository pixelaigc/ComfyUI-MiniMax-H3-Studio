#!/usr/bin/env python3
"""长跑采样线上 8190 的 /api/jobs 延迟（每 1 秒一次，共 150 秒）。

目的：抓「60 秒 TTL 过期 -> 效果 LoRA 全盘扫描」造成的周期性尖峰。
"""
import json
import statistics
import sys
import time
import urllib.request

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 150
URL = sys.argv[2] if len(sys.argv) > 2 else \
    "http://127.0.0.1:8190/api/jobs?status=all&page=1&page_size=48"

samples = []
t0 = time.time()
print(f"sampling {URL} every 1s for {DUR}s")
while time.time() - t0 < DUR:
    t = time.time()
    try:
        with urllib.request.urlopen(URL, timeout=30) as r:
            b = r.read()
        ms = (time.time() - t) * 1000
        samples.append({"t": round(time.time() - t0, 2), "ms": round(ms, 1), "bytes": len(b)})
        print(f"  t={time.time()-t0:7.1f}s  {ms:8.1f}ms  {len(b)}B", flush=True)
    except Exception as e:
        ms = (time.time() - t) * 1000
        samples.append({"t": round(time.time() - t0, 2), "ms": round(ms, 1), "err": str(e)[:80]})
        print(f"  t={time.time()-t0:7.1f}s  {ms:8.1f}ms  ERROR {e}", flush=True)
    time.sleep(1)

ms_list = [s["ms"] for s in samples if "err" not in s]
ms_sorted = sorted(ms_list)
n = len(ms_sorted)


def pct(p):
    if not n:
        return None
    return round(ms_sorted[min(n - 1, int(n * p / 100))], 1)


print("\n=== 汇总 ===")
print(f"samples={n}  min={pct(0)}  p50={pct(50)}  p90={pct(90)}  p99={pct(99)}  max={pct(100)}")
print(f"mean={round(statistics.mean(ms_list),1) if ms_list else None}")
over = [s for s in samples if s.get("ms", 0) > 200]
print(f"\n>200ms 的样本: {len(over)}")
for s in over:
    print("   ", s)

with open("/root/.hermes-web-ui/.ekko/workspace/default/"
          "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
          "evidence/mediaperm-20261121/live-longsample.json", "w") as f:
    json.dump({"url": URL, "duration_s": DUR, "samples": samples,
               "p50": pct(50), "p90": pct(90), "p99": pct(99), "max": pct(100)},
              f, ensure_ascii=False, indent=2)
print("\nwrote live-longsample.json")
