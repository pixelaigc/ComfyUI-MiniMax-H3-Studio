#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证：带 sigma 精修的工作流节点参数是否满足 ComfyUI 的真实 schema。

【为什么需要这一步】
「节点存在」不等于「参数名写对了」。ComfyUI 会按 object_info 校验每个节点的
inputs 键名与取值范围 —— 键名写错会直接被 ComfyUI 拒绝（400），
值越界同理。所以这里拿**本机 ComfyUI 真实的 object_info**逐节点校验。

不是「跑一次出片」（那要占 GPU 且会产生真实任务），而是把 schema 契约先验掉。

【本脚本验证的三个场景】
  A 单采（基线，18 节点，无 346）
  B 二采（18 节点，171.sigmas = ["9",0]）
  C 二采 + sigma 精修（19 节点，346 插在 9 与 171 之间，
    171.sigmas = ["346",0]）

运行：python3 tests/verify_sigma_refine_schema.py
退出码：0 = 三个场景全部通过 ComfyUI 真实 schema 校验
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from h3studio import video_workflow as vw

oi = json.load(urllib.request.urlopen("http://127.0.0.1:8188/object_info", timeout=90))

base = {
    "job_id": "t", "model_key": "fl2va_pruned_int8",
    "width": 1344, "height": 768, "aspect": "16:9",
    "duration": 5, "steps": 4, "sampler": "euler", "scheduler": "beta",
    "seed": 1, "shift_video": 12.0, "shift_audio": 3.0,
    "fixed_accel_lora": "ckpt850", "accel_loras": [], "attention": "default",
}

SCENARIOS = [
    ("A 单采（基线）", dict(base)),
    ("B 二采（无精修）", dict(base, twopass={"enabled": True, "transition_step": 3})),
    ("C 二采 + sigma 精修", dict(base, twopass={
        "enabled": True, "transition_step": 3,
        "sigma_refine": {"enabled": True, "extra_steps": 1,
                         "start_at_sigma": 2.5, "spacing": "cosine"}})),
]


def check(wf):
    errors = []
    for nid, node in wf.items():
        ct = node.get("class_type")
        if ct not in oi:
            errors.append(f"{nid}: node {ct} NOT in ComfyUI")
            continue
        spec = oi[ct].get("input", {})
        allowed = set(spec.get("required", {})) | set(spec.get("optional", {}))
        got = set(node.get("inputs", {}))
        # 【动态输入】COMFY_AUTOGROW_V3 类型接受任意后缀的动态输入名
        # （ComfyMathExpression 的 "values.a" 就是这种）-> 不能按固定 schema 判非法。
        dynamic_ok = any(
            isinstance(w, list) and w and w[0] == "COMFY_AUTOGROW_V3"
            for w in (spec.get("required", {}).values())
            if isinstance(w, list)
        )
        extra = got - allowed
        if extra and not dynamic_ok:
            errors.append(f"{nid} ({ct}): unexpected input keys {sorted(extra)}")
        for k in got & allowed:
            want = spec["required"].get(k) or spec["optional"].get(k)
            if not (isinstance(want, list) and want):
                continue
            v = node["inputs"][k]
            # 【连接引用】值是 [node_id, output_index] -> 说明这个槽位接的是
            # 别的节点的输出，不是字面量 -> 不做类型/范围校验。
            # （本项目 105:104.length 就是这种：接 ComfyMathExpression 的输出。）
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                continue
            if want[0] == "COMBO":
                opts = want[1].get("options") if isinstance(want[1], dict) else None
                if opts and v not in opts:
                    errors.append(f"{nid} ({ct}).{k}={v!r} not in {opts}")
            elif want[0] == "INT":
                lo = want[1].get("min", -10**9)
                hi = want[1].get("max", 10**9)
                if not isinstance(v, int) or isinstance(v, bool) or not (lo <= v <= hi):
                    errors.append(f"{nid} ({ct}).{k}={v!r} out of INT [{lo},{hi}]")
            elif want[0] == "FLOAT":
                lo = want[1].get("min", -10**18)
                hi = want[1].get("max", 10**18)
                if not isinstance(v, (int, float)) or not (lo <= v <= hi):
                    errors.append(f"{nid} ({ct}).{k}={v!r} out of FLOAT [{lo},{hi}]")
    return errors


all_ok = True
for name, params in SCENARIOS:
    wf = vw.build_t2v_panel(dict(params))
    errs = check(wf)
    has346 = "346" in wf
    s = wf.get("171", {}).get("inputs", {})
    print(f"=== {name} ===")
    print(f"  nodes={len(wf)}  346_present={has346}  "
          f"171.sigmas={s.get('sigmas')}  171.ts={s.get('transition_step')}")
    print(f"  schema errors: {len(errs)}")
    for e in errs:
        print("    -", e)
    if errs:
        all_ok = False

print()
print("ALL SCENARIOS SCHEMA-VALID:", all_ok)
sys.exit(0 if all_ok else 1)
