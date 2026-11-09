#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「二次增强不丢背景」的真机回归脚本。

用途：对**线上 8190**（或任意指向同一 Provider 的 Studio 实例）跑真实的
二次增强，逐项核对原文里的背景 / 环境 / 氛围 / 构图 / 文字要素是否还在输出里。

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_refine_keeps_background_live.py \
        --url http://127.0.0.1:8190

    # 也可指定模板 / 长度档
    ... --spec image-refine --length long

输出：JSON（每项要素的命中情况）+ 人类可读表格。
退出码 0 = 全部要素保留；1 = 有要素丢失。
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# ---- 用户原始提示词（取自线上 jobs.db job 38aab008f81d，逐字一致，381 字）----
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

# 第二段提示词：含**明确的中文文字元素**（招牌上写着 XX），
# 用来验证「文字类要求被保留」。
TEXT_PROMPT = (
    "夜里的老城区街道，路边一家小面馆的灯箱招牌上写着「老王记牛肉面」，"
    "招牌是白底红字的塑料灯箱，边缘有一处裂口。店门口的塑料凳上坐着一位穿灰色工装的外卖骑手，"
    "头盔放在膝盖上，保温箱立在脚边，箱体侧面贴着黄色封条。地面有积水，"
    "积水中倒映着招牌的红字和旁边药店的绿色灯箱。远处有一辆出租车驶过，"
    "尾灯拖出红色光带。空气里有雨后的雾气，路灯是暖橙色的钠灯，"
    "与药店的绿色冷光形成对比。构图为中远景，人物在画面右侧三分之一处。"
)

# 要素核对表：原文里的每一项 -> 期望在输出里能看到的标记
# 外层每一组都必须命中至少一个标记（组间是「与」关系）。
HK_ELEMENTS = [
    ("人物/服装-针织衫", [["针织衫"]]),
    ("人物/服装-灯芯绒外套", [["灯芯绒"]]),
    ("人物/服装-牛仔裤", [["牛仔裤"]]),
    ("人物/服装-皮带", [["皮带"]]),
    ("人物/服装-帆布鞋", [["帆布鞋"]]),
    ("发型-黑色长卷发", [["长卷发"]]),
    ("妆容-哑光红唇", [["红唇", "哑光口红", "唇色"]]),
    ("背景-骑楼建筑", [["骑楼"]]),
    ("背景-生锈铁栏杆", [["铁栏杆", "栏杆"], ["锈"]]),
    ("背景-混凝土墙", [["混凝土"]]),
    ("背景-褪色繁体海报", [["海报"], ["繁体", "褪色"]]),
    ("霓虹招牌", [["霓虹"]]),
    ("招牌文字-金/記/酒", [["金"], ["記"], ["酒"]]),
    ("潮湿沥青路面", [["沥青"]]),
    ("积水/反射", [["积水", "反射", "倒影"]]),
    ("雨雾天气", [["雨"], ["雾"]]),
    ("暖黄色调", [["暖黄", "色温"]]),
    ("冷青色自然光", [["冷青", "青色"]]),
    ("冷暖对比光影", [["冷暖", "对比"]]),
    ("构图-中景视角", [["中景"]]),
    ("构图-低角度仰拍", [["仰拍", "低角度"]]),
    ("胶片颗粒质感", [["胶片", "颗粒"]]),
    ("低饱和怀旧色调", [["低饱和"]]),
    ("暗部青绿色偏移", [["青绿"]]),
    ("高光橙黄暖调", [["橙黄", "暖调"]]),
]

TEXT_ELEMENTS = [
    # 招牌上写着 XX —— 逐字文字必须被保留（这是本次要验证的重点）
    ("招牌文字-老王记牛肉面", [["老王记牛肉面"]]),
    # 复合要求：模型改写成「底色为高亮白，文字为鲜红色」也算保留
    ("招牌-白底红字", [["白底", "高亮白", "白色"], ["红字", "鲜红", "红色"]]),
    ("招牌-塑料灯箱", [["灯箱"], ["塑料", "亚克力"]]),
    ("招牌-边缘裂口", [["裂口"]]),
    ("外卖骑手", [["骑手", "外卖"]]),
    ("灰色工装", [["灰色"], ["工装"]]),
    ("头盔放膝盖上", [["头盔"], ["膝盖", "大腿"]]),
    ("保温箱", [["保温箱"]]),
    ("黄色封条", [["封条"], ["黄色"]]),
    ("地面积水", [["积水"]]),
    ("积水倒映招牌红字", [["倒映", "倒影", "反射"]]),
    ("药店绿色灯箱", [["药店"], ["绿色"]]),
    ("出租车驶过", [["出租车"]]),
    ("尾灯红色光带", [["尾灯"], ["光带", "拖影"]]),
    ("雨后雾气", [["雾"]]),
    ("暖橙钠灯", [["钠灯"], ["暖橙", "暖黄"]]),
    ("药店绿色冷光对比", [["对比", "冷光"]]),
    ("构图中远景", [["中远景", "中景"]]),
    ("人物右侧三分之一", [["右"], ["三分之一"]]),
]


def call_enhance(url, prompt, spec, length, mode_ctx="t2i", model_key="z_image_turbo_bf16"):
    payload = {
        "prompt": prompt,
        "mode": "replace",
        "spec_id": spec,
        "mode_ctx": mode_ctx,
        "model_key": model_key,
        "params": {"length": length},
    }
    req = urllib.request.Request(
        url.rstrip("/") + "/api/enhance",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": raw[:500]}


def check_elements(text, table):
    """
    逐项核对要素是否还在输出里。

    每个要素的 markers 是一个**分组列表**：外层每一组都必须在输出里命中至少一个标记。
    这样能表达「白底红字」这种复合要求 —— 模型会改写成
    「底色为高亮白，文字为鲜红色」，逐字找「白底红字」会误报丢失，
    但「白」和「红」两个词都在，就说明要求确实被保留了。

    返回 (rows, all_ok)。
    """
    rows = []
    all_ok = True
    for name, groups in table:
        group_hits = []
        ok = True
        for markers in groups:
            hit = [m for m in markers if m in text]
            group_hits.append(hit)
            if not hit:
                ok = False
        if not ok:
            all_ok = False
        rows.append({"element": name, "ok": ok, "found": group_hits,
                     "expect_groups": groups})
    return rows, all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8190")
    ap.add_argument("--spec", default="image-refine")
    ap.add_argument("--length", default="long")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    results = {}
    overall_ok = True

    for label, prompt, table in (
            ("hk_street", HK_PROMPT, HK_ELEMENTS),
            ("chinese_text", TEXT_PROMPT, TEXT_ELEMENTS)):
        status, body = call_enhance(args.url, prompt, args.spec, args.length)
        print("=" * 78)
        print("### %s  (HTTP %s)" % (label, status))
        print("=" * 78)
        if status != 200 or "enhanced" not in body:
            print("!! 调用失败：", json.dumps(body, ensure_ascii=False)[:800])
            overall_ok = False
            results[label] = {"ok": False, "status": status, "body": body}
            continue

        enhanced = body.get("enhanced", "")
        params = body.get("params") or {}
        coverage = body.get("coverage") or {}
        print("max_tokens      :", params.get("max_tokens"))
        print("finish_reason   :", body.get("finish_reason"))
        print("coverage ratio  :", coverage.get("ratio"))
        print("output chars    :", len(enhanced))
        print("-" * 78)
        rows, ok = check_elements(enhanced, table)
        for r in rows:
            flag = "OK  " if r["ok"] else "MISS"
            print("  [%s] %-26s found=%s" % (flag, r["element"], r["found"]))
        print("-" * 78)
        print("ALL ELEMENTS KEPT:", ok)
        print()
        print("---------- FULL OUTPUT ----------")
        print(enhanced)
        print("---------- END OUTPUT ----------")
        print()

        results[label] = {"ok": ok, "status": status, "rows": rows,
                          "max_tokens": params.get("max_tokens"),
                          "finish_reason": body.get("finish_reason"),
                          "coverage": coverage,
                          "enhanced": enhanced}
        overall_ok = overall_ok and ok

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("saved ->", args.out)

    print("OVERALL:", "PASS" if overall_ok else "FAIL")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
