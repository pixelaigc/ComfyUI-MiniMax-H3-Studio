#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二次增强的**要素保留方差**测量 —— 区分「被截断」与「模型采样波动遗漏」。

【为什么需要这个脚本】
审查要求：不得把「模型采样波动导致的措辞差异」说成「截断」。
所以这里对同一条提示词跑 N 次，逐次记录：
  - finish_reason          （length = 被硬切；stop = 模型自己收尾）
  - 结构完整性              （末段标题是否存在）
  - 每个要素的命中情况       （精确词组 vs 同义改写）

【判定口径】
  截断（truncation）  <=> finish_reason == "length" 或 末段标题缺失
  波动（variance）    <=> finish_reason == "stop" 且结构完整，但个别要素措辞变了

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_refine_element_variance.py \
        --url http://127.0.0.1:8199 --runs 3 [--out <json>]
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from verify_refine_keeps_background_live import (  # noqa: E402
    HK_PROMPT, HK_ELEMENTS, check_elements,
)

# 语义同义组：模型换措辞时，语义仍在 -> 判为「波动」而非「丢失」
SYNONYMS = {
    "胶片颗粒质感": [["胶片", "颗粒"], ["film", "grain"], ["胶片感"], ["颗粒感"],
                     ["grainy"], ["film grain"], ["grain"]],
    "低饱和怀旧色调": [["低饱和"], ["怀旧"], ["复古"], ["低饱和度"], ["vintage"],
                       ["muted"], ["desaturated"], [" nostalgic"]],
    "高光橙黄暖调": [["橙黄"], ["暖调"], ["暖黄"], ["amber"], ["warm"], ["高光"],
                     ["highlight"], ["橙色调"], ["暖色调"]],
}


def call_enhance(url, prompt, spec="image-refine", length="long", timeout=900):
    payload = {"prompt": prompt, "mode": "replace", "spec_id": spec,
               "params": {"length": length}, "mode_ctx": "t2i",
               "model_key": "z_image_turbo_bf16"}
    req = urllib.request.Request(
        url.rstrip("/") + "/api/enhance",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), time.time() - t0
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:400]}
        return exc.code, body, time.time() - t0


def semantic_hit(name, enhanced_low):
    """要素是否以同义措辞保留。"""
    for group in SYNONYMS.get(name, []):
        if all(token.strip().lower() in enhanced_low for token in group):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--spec", default="image-refine")
    ap.add_argument("--length", default="long")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    runs = []
    for i in range(args.runs):
        status, body, el = call_enhance(args.url, HK_PROMPT, args.spec, args.length)
        if status != 200 or not isinstance(body, dict):
            runs.append({"run": i, "status": status, "ok": False,
                         "error": (str(body.get("error"))[:160]
                                   if isinstance(body, dict) else str(body)[:160]),
                         "elapsed": round(el, 1)})
            print("run%-2d HTTP %s  %s" % (i, status, runs[-1]["error"]))
            continue
        enhanced = body.get("enhanced", "")
        enhanced_low = enhanced.lower()
        rows, all_ok = check_elements(enhanced, HK_ELEMENTS)
        finish = body.get("finish_reason") or ""
        cov = body.get("coverage") or {}
        truncated = (finish == "length") or bool(cov.get("truncated"))

        exact_miss, semantic_miss = [], []
        for r in rows:
            if r["ok"]:
                continue
            name = r["element"]
            if semantic_hit(name, enhanced_low):
                semantic_miss.append(name)
            else:
                exact_miss.append(name)

        # 末段标题是否在（结构完整性）
        has_tail = ("【主体细化】" in enhanced) or ("【整体细化】" in enhanced)
        runs.append({
            "run": i, "status": status, "ok": True,
            "elapsed": round(el, 1),
            "chars": len(enhanced),
            "finish_reason": finish,
            "truncated": truncated,
            "tail_section_present": has_tail,
            "coverage_ratio": cov.get("ratio"),
            "exact_miss": exact_miss,
            "paraphrase_miss": semantic_miss,
            "total_miss": len(exact_miss) + len(semantic_miss),
        })
        print("run%-2d finish=%-6s trunc=%-5s tail=%-5s chars=%-5d "
              "exact_miss=%-2d paraphrase_miss=%-2d %s"
              % (i, finish, truncated, has_tail, len(enhanced),
                 len(exact_miss), len(semantic_miss),
                 (exact_miss + semantic_miss) or ""))

    ok_runs = [r for r in runs if r.get("ok")]
    trunc_runs = [r for r in ok_runs if r["truncated"]]
    summary = {
        "runs": len(runs),
        "http_ok": len(ok_runs),
        "truncated_runs": len(trunc_runs),
        "truncation_rate": len(trunc_runs) / max(1, len(ok_runs)),
        "runs_with_any_miss": sum(1 for r in ok_runs if r["total_miss"] > 0),
        "runs_all_elements_kept": sum(1 for r in ok_runs if r["total_miss"] == 0),
        "elements_total": len(HK_ELEMENTS),
        "miss_distribution": {},
    }
    dist = {}
    for r in ok_runs:
        for name in r["exact_miss"] + r["paraphrase_miss"]:
            dist[name] = dist.get(name, 0) + 1
    summary["miss_distribution"] = dist

    print()
    print("summary:", json.dumps(summary, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "runs": runs}, f,
                      ensure_ascii=False, indent=2)
        print("saved ->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
