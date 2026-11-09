#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
普通增强（official PE / image 模板）的截断率统计 —— 审查 P1-3 的复验脚本。

审查实测（旧实现，medium 档固定 300 tokens）：
    8199 basic(medium/300) run0..run9 => {'TRUNC': 6, 'OK': 4}  （成功率仅 40%）

本脚本用**完全相同**的提示词、模型、实例跑 N 次，统计：
    OK    —— 正常返回且未被截断
    TRUNC —— 被 max_tokens 硬切（服务端返回 502 + truncated:true）
    ERR   —— 其它错误

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_basic_enhance_truncation_rate.py \
        --url http://127.0.0.1:8199 --runs 10 --out <json>
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# 用户原始提示词（线上 jobs.db job 38aab008f81d，逐字一致，381 字）
HK_PROMPT = (
    "一位年轻亚洲女性站立在狭窄的香港后街人行道边缘。她身穿复古风格的米白色高领针织衫，"
    "外搭一件深红色灯芯绒西装外套，下身穿着高腰蓝色直筒牛仔裤，腰间系着黑色皮带，"
    "脚踩白色帆布鞋。她的头发是蓬松的黑色长卷发，涂着鲜艳的哑光红唇，"
    "眼神略带疏离地望向镜头右侧。背景是典型的港式老旧街道，"
    "左侧是一排带有生锈铁栏杆和霓虹灯招牌的密集骑楼建筑，右侧是斑驳的灰色混凝土墙壁，"
    "墙上贴着褪色的繁体中文海报。地面是潮湿的沥青路面，反射着周围昏暗的光线。"
    "空中悬浮着细微的雨滴雾气。光线为低色温的暖黄色调，"
    "来自头顶上方悬挂的“金”、“記”、“酒”家招牌发出的霓虹灯光，"
    "以及远处街道尽头透进的冷青色自然光，形成强烈的冷暖对比光影。"
    "画面构图采用中景视角，轻微的低角度仰拍，突出人物的孤独感与环境的压迫感。"
    "胶片颗粒质感明显，色彩呈现低饱和度的怀旧色调，暗部带有青绿色偏移，"
    "高光部分保留橙黄色暖调。"
)


def call_enhance(url, prompt, spec="image", timeout=900):
    payload = {"prompt": prompt, "mode": "replace", "spec_id": spec,
               "mode_ctx": "t2i", "model_key": "z_image_turbo_bf16"}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8199")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--spec", default="image")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    results = []
    summary = {"OK": 0, "TRUNC": 0, "ERR": 0}
    for i in range(args.runs):
        status, body, el = call_enhance(args.url, HK_PROMPT, args.spec)
        if not isinstance(body, dict):
            body = {"error": str(body)[:200]}
        bparams = body.get("params") or {}
        if status == 200 and "enhanced" in body:
            kind = "OK"
            info = ("max_tokens=%s finish=%s chars=%d" % (
                bparams.get("max_tokens"),
                body.get("finish_reason"), len(body.get("enhanced", ""))))
        elif status == 502 and body.get("truncated"):
            kind = "TRUNC"
            info = "truncated: " + str(body.get("truncation_reason"))[:90]
        else:
            kind = "ERR"
            info = "HTTP %s %s" % (status, str(body.get("error"))[:110])
        summary[kind] += 1
        results.append({"run": i, "kind": kind, "status": status,
                        "elapsed": round(el, 1), "info": info,
                        "max_tokens": bparams.get("max_tokens"),
                        "finish_reason": body.get("finish_reason"),
                        "chars": len(body.get("enhanced", ""))})
        print("run%-2d %-6s %-6.1fs %s" % (i, kind, el, info))
        # 让出 llama.cpp 的唯一 slot
        time.sleep(0.3)

    total = sum(summary.values())
    print()
    print("summary:", summary)
    print("success rate: %d/%d = %.0f%%" % (
        summary["OK"], total, 100.0 * summary["OK"] / max(1, total)))
    print("truncation rate: %d/%d = %.0f%%" % (
        summary["TRUNC"], total, 100.0 * summary["TRUNC"] / max(1, total)))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "results": results},
                      f, ensure_ascii=False, indent=2)
        print("saved ->", args.out)
    return 0 if summary["TRUNC"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
