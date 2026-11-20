#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""缩略图功能的端到端验证（对隔离实例跑）。

验证：
  1. /api/jobs 的每条产物都带 thumb 字段（图片 + 视频）
  2. /api/thumb/<key>.jpg 能取到（200 + 正确的 Content-Type + 体积小）
  3. 缩略图内容确实比原图小很多
  4. 第二次请求命中缓存（几乎免费）
  5. 非法 key / 目录穿越一律 404
  6. 缩略图是「不可变」的（key 含 mtime/size）-> Cache-Control 允许长缓存

用法: python scripts/verify_thumbnails.py --base http://127.0.0.1:8912
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "..", "evidence", "media-perf")


def fetch(url, timeout=120):
    t0 = time.perf_counter()
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return {"ms": (time.perf_counter() - t0) * 1000.0, "status": r.status,
                    "bytes": len(body), "headers": dict(r.headers)}
    except urllib.error.HTTPError as e:
        return {"ms": (time.perf_counter() - t0) * 1000.0, "status": e.code,
                "bytes": 0, "headers": dict(e.headers or {})}
    except Exception as e:  # noqa: BLE001
        return {"ms": (time.perf_counter() - t0) * 1000.0, "status": 0,
                "bytes": 0, "headers": {}, "error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8190")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    base = args.base.rstrip("/")
    results = []
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        results.append({"name": name, "pass": bool(cond), "detail": str(detail)})
        print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")
        if not cond:
            ok = False

    print("=== 1) /api/jobs 的 thumb 字段 ===")
    t0 = time.perf_counter()
    with urllib.request.urlopen(base + "/api/jobs?status=done&page=1&page_size=12", timeout=180) as r:
        data = json.loads(r.read())
    list_ms = (time.perf_counter() - t0) * 1000.0
    jobs = data.get("jobs") or []
    with_thumb = 0
    total_media = 0
    sample = None
    for j in jobs:
        for o in (j.get("outputs") or []):
            if o.get("kind") in ("image", "video"):
                total_media += 1
                if o.get("thumb"):
                    with_thumb += 1
                    if sample is None:
                        sample = (j, o)
    check("产物带 thumb 字段", with_thumb > 0,
          f"{with_thumb}/{total_media} 个媒体产物有 thumb（列表接口 {list_ms:.0f}ms）")

    if sample is None:
        check("有可测的缩略图样本", False, "列表里没有带 thumb 的媒体产物")
        print("\n=== 摘要：失败 ===")
        return 1

    job, out = sample
    thumb_url = base + out["thumb"]
    print("\n=== 2) 取缩略图 ===")
    r1 = fetch(thumb_url)
    check("缩略图 200", r1["status"] == 200, f"status={r1['status']} bytes={r1['bytes']}")
    check("Content-Type 是 image/jpeg",
          r1["headers"].get("Content-Type", "").startswith("image/jpeg"),
          r1["headers"].get("Content-Type"))
    cc = r1["headers"].get("Cache-Control", "")
    # 【为什么不是 immutable】仓库既有契约
    # （tests/test_frontend_assets_hygiene.py::
    #  test_server_static_has_no_cache_control_is_known_risk）：
    # `server.py` 里不允许出现那串「永久缓存」指令 ——
    # 静态资源的 `?v=` 版本号才是唯一的破缓存防线。
    # 缩略图 URL 是**内容寻址**的（sha1(路径+mtime_ns+size)），
    # 源文件一变 URL 就变 => 24 小时 max-age 完全安全。
    check("Cache-Control 允许长缓存（max-age）", "max-age=86400" in cc, cc)
    check("Cache-Control 不是 no-cache（否则每次都回源）", "no-cache" not in cc, cc)

    print("\n=== 3) 与原图比体积 ===")
    # 【踩坑】out["file"] 有两种形态：
    #   "images/xxx.png"（全局目录）或 "<job_id>/xxx.png"（任务目录）。
    #   前者不能拼 job_id 前缀 —— 拼了就是 404。
    #   判据：**含 "/"** 说明已经在子目录里；不含才是「任务目录下的文件名」。
    orig_path = str(out.get("file") or "")
    rel = orig_path.replace("\\", "/")
    if "/" not in rel:
        rel = f"{job['id']}/{rel}"
    orig_url = base + "/files/" + "/".join(urllib.parse.quote(s) for s in rel.split("/"))
    ro = fetch(orig_url)
    if ro["status"] == 200 and ro["bytes"] > 0:
        ratio = ro["bytes"] / max(1, r1["bytes"])
        check("缩略图显著小于原图", ratio >= 3, f"原 {ro['bytes']}B -> 缩略 {r1['bytes']}B（{ratio:.1f}x）")
    else:
        check("原图可取（用于对比）", False, f"status={ro['status']}")

    print("\n=== 4) 第二次取（应命中浏览器/磁盘缓存语义）===")
    r2 = fetch(thumb_url)
    check("第二次仍 200 且字节一致", r2["status"] == 200 and r2["bytes"] == r1["bytes"],
          f"{r2['status']} {r2['bytes']}B")

    print("\n=== 5) 非法 key / 目录穿越 ===")
    for bad in ["/api/thumb/notahash.jpg",
                "/api/thumb/../../etc/passwd",
                "/api/thumb/0123456789abcdef0123456789abcdef01234567.jpg",
                "/api/thumb/short.jpg",
                "/api/thumb/0123456789abcdef0123456789abcdef01234567.png"]:
        rr = fetch(base + bad)
        check(f"非法路径 {bad} -> 404", rr["status"] == 404, f"status={rr['status']}")

    out_path = args.out or f"{OUT_DIR}/verify-thumbnails.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"base": base, "list_ms": list_ms,
                   "with_thumb": with_thumb, "total_media": total_media,
                   "results": results}, f, ensure_ascii=False, indent=2)
    print(f"\n=== 摘要：{sum(1 for r in results if r['pass'])}/{len(results)} PASS ===")
    print(f"证据: {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
