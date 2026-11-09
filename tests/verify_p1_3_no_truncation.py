# -*- coding: utf-8 -*-
"""P1-3 复验：普通增强（medium 档）连跑 10 次，长提示词不得被硬切 / 502。

【审查报告 P1-3 原文】
  max-token 分支把「详尽」档提到 32768，但普通增强（image 模板）的
  default_length 仍是 medium，而 medium 档预算是固定 300 tokens。
  实测 10 次里 6 次被硬切（成功率仅 40%）。
  验收：同一长提示词连跑 10 次，**不得出现硬切 / 502**。

【本脚本判据】
对每次调用读后端返回：
  - HTTP 状态码（502 = 截断守卫命中且降级重试也失败）
  - finish_reason（length = 被硬切）
  - coverage ratio（< 1.0 = 要素丢失）
  - 输出字数

用法：python3 tests/verify_p1_3_no_truncation.py --base http://127.0.0.1:8297/ --runs 10
"""
import argparse
import json
import sys
import time
import urllib.request
import urllib.error

# 审查报告 P2-1 用的同一段 381 字港风街拍提示词
PROMPT = (
    "一位年轻亚洲女性站立在狭窄的香港后街人行道边缘，身穿米白色高领针织衫，"
    "外搭深红色灯芯绒西装外套，下身蓝色直筒牛仔裤。背景是港式老旧街道，"
    "左侧一排带生锈铁栏杆和霓虹灯招牌的骑楼建筑，右侧斑驳灰色混凝土墙，"
    "墙上贴着褪色繁体中文海报，地面潮湿沥青路面反射昏暗光线，"
    "空气悬浮细微雨滴雾气。光线为低色温暖黄，来自头顶悬挂的霓虹招牌，"
    "与远处冷青色自然光形成冷暖对比。中景视角，轻微低角度仰拍，胶片颗粒质感。"
)


def call(base, spec_id, mode_ctx, model_key):
    body = json.dumps({
        "prompt": PROMPT,
        "mode": "replace",
        "spec_id": spec_id,
        "mode_ctx": mode_ctx,
        "model_key": model_key,
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/api/enhance", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except Exception as e:
        return {"status": "ERR:" + type(e).__name__,
                "error": str(e)[:200], "secs": round(time.time() - t0, 1)}
    try:
        d = json.loads(raw)
    except Exception:
        d = {"raw": raw.decode("utf-8", "replace")[:300]}
    if not isinstance(d, dict):
        d = {"raw": str(d)[:300]}
    cov = d.get("coverage")
    if not isinstance(cov, dict):
        cov = {}
    params = d.get("params")
    if not isinstance(params, dict):
        params = {}
    out = {
        "status": status,
        "secs": round(time.time() - t0, 1),
        "chars": len(d.get("enhanced") or ""),
        "finish_reason": d.get("finish_reason") or "",
        "error": d.get("error") or "",
        "coverage": cov.get("ratio"),
        "truncated": bool(cov.get("truncated")),
        "max_tokens": params.get("max_tokens"),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8297/")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--spec", default="image")
    ap.add_argument("--mode-ctx", default="t2i")
    ap.add_argument("--model", default="z_image_turbo_bf16")
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    rows = []
    for i in range(args.runs):
        r = call(args.base, args.spec, args.mode_ctx, args.model)
        r["run"] = i
        rows.append(r)
        print(f"run{i}: status={r['status']} chars={r.get('chars')} "
              f"finish={r.get('finish_reason')!r} coverage={r.get('coverage')} "
              f"truncated={r.get('truncated')} max_tokens={r.get('max_tokens')} "
              f"secs={r.get('secs')} {('ERR: ' + r.get('error','')) if r.get('error') else ''}")

    trunc = [r for r in rows if r.get("truncated") or r.get("finish_reason") == "length"]
    bad = [r for r in rows if r.get("status") != 200]
    empty = [r for r in rows if r.get("status") == 200 and not r.get("chars")]

    summary = {
        "runs": len(rows),
        "ok": len(rows) - len(bad),
        "http_errors": len(bad),
        "truncated": len(trunc),
        "empty": len(empty),
        "chars_min": min([r.get("chars") or 0 for r in rows] or [0]),
        "chars_max": max([r.get("chars") or 0 for r in rows] or [0]),
        "rows": rows,
    }
    print("\n=== SUMMARY ===")
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"},
                     ensure_ascii=False, indent=2))
    print("VERDICT:", "PASS (no truncation / no 502)" if not bad and not trunc and not empty
          else "FAIL")

    import os
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "p1-3-no-truncation.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    return 0 if (not bad and not trunc and not empty) else 1


if __name__ == "__main__":
    sys.exit(main())
