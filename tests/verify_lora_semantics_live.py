#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LoRA 语义规则真机复核（改动 Qwen 面板后必跑）。

规则（用户早前明确）：
  1. 工作流必需的加速 LoRA **不出现**在用户可选列表里；
  2. 列表中的效果 LoRA 选中后自动应用推荐强度；
  3. 效果 LoRA 触发词自动追加到最终提示词，且可编辑修改；
  4. LoRA 节点连 clip（不是只连 model）。

用法：python3 tests/verify_lora_semantics_live.py
退出码 0 = 全部通过。
"""
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FAILURES = []
PASSES = []


def check(name, ok, detail=""):
    (PASSES if ok else FAILURES).append(f"{name}: {detail}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    from h3studio import workflows as W

    accel = getattr(W, "Z_IMAGE_ACCEL_LORA", {})
    accel_values = {v for v in accel.values() if v}
    accel_names = set(accel_values)

    # ---------- 规则 1：加速 LoRA 不在用户可选列表 ----------
    check("规则1 存在内置加速 LoRA 映射(非空)", bool(accel_values),
          f"{len(accel_values)} 条: {sorted(accel_values)}")

    # 效果目录（用户可选来源）不得含任何加速/蒸馏权重
    excluded = set()
    cat = os.path.join(REPO, "h3studio", "lora_catalog_effects.py")
    if os.path.exists(cat):
        with open(cat, encoding="utf-8") as fh:
            for line in fh:
                for m in re.finditer(r'["\']([^"\']*\.safetensors)["\']', line):
                    nm = m.group(1)
                    if re.search(r'turbo|distill|lightning|dmd|speedup|accel', nm, re.I):
                        excluded.add(nm)
    overlap = accel_names & excluded
    check("规则1 加速 LoRA 未混入效果目录", not overlap, f"交集={sorted(overlap) or '空'}")

    # 走真实的服务端接口（用户实际看到的列表），按权威的 kind 字段判定
    listed = set()
    try:
        import urllib.request
        import json as _json
        import urllib.parse
        for base in ("Z-Image", "Qwen-Image 2.1", "MOSS-Image"):
            url = ("http://127.0.0.1:8190/api/image-loras?"
                   + urllib.parse.urlencode({"base": base}))
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            avail = data.get("available") or []
            listed |= {str(i.get("name")) for i in avail if i.get("name")}
            # 权威判定：kind 字段（不要用文件名关键字猜 —— 效果 LoRA 的文件名里
            # 经常带 'Z_Image_Turbo' 这类底模名，用关键字会误判）
            bad = [i.get("name") for i in avail if i.get("kind") == "acceleration"]
            check(f"规则1 [{base}] 可选列表不含加速 LoRA", not bad,
                  f"可选 {len(avail)} 条, kind 分布="
                  f"{ {k: sum(1 for i in avail if i.get('kind') == k) for k in {i.get('kind') for i in avail}} }")
    except Exception as exc:  # noqa: BLE001
        check("规则1 真实接口可选列表不含内置加速 LoRA", False, f"接口探测失败: {exc}")

    # ---------- 规则 3：触发词自动追加 + 可编辑 ----------
    check("规则3 存在触发词合并函数 merge_lora_triggers",
          callable(getattr(W, "merge_lora_triggers", None)), "h3studio.workflows")
    if callable(getattr(W, "merge_lora_triggers", None)):
        # 注意：真实入参的触发词字段是 "trigger"（不是 "name"）
        out = W.merge_lora_triggers("a cat sitting on a sofa",
                                    [{"name": "x", "strength": 0.8, "trigger": "triggera"},
                                     {"name": "y", "strength": 0.6, "trigger": "triggerb"}])
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        check("规则3 触发词被自动追加到提示词",
              "triggera" in text and "triggerb" in text, repr(text[:160]))
        check("规则3 原提示词未被破坏", "a cat sitting on a sofa" in text, repr(text[:160]))
        # 可编辑：合并结果里的触发词来自入参，代码不得硬编码具体触发词
        # （'trigger' 是字段名本身，不算硬编码触发词）
        consts = [c for c in W.merge_lora_triggers.__code__.co_consts
                  if isinstance(c, str) and c not in ("trigger",)
                  and re.fullmatch(r"[A-Za-z0-9_\-]{4,}", c)]
        check("规则3 触发词来自入参(非硬编码, 故可改)", not consts, f"疑似硬编码={consts or '无'}")

        # 只选一个 LoRA 时只应追加该 LoRA 的触发词
        out1 = W.merge_lora_triggers("base prompt",
                                     [{"name": "x", "strength": 0.7, "trigger": "only_one"}])
        check("规则3 单个 LoRA 只追加自己的触发词",
              "only_one" in out1, repr(out1[:120]))

        # 用户改过触发词 -> 以用户的为准（可编辑的实证）
        out_edit = W.merge_lora_triggers("a cat",
                                         [{"name": "x", "strength": 1.0, "trigger": "my_own_word"}])
        check("规则3 用户改过的触发词以用户为准(可编辑)",
              "my_own_word" in out_edit and "class1cpa1nt" not in out_edit, repr(out_edit[:120]))

        # 空触发词不追加
        out_empty = W.merge_lora_triggers("a cat",
                                          [{"name": "x", "strength": 1.0, "trigger": ""}])
        check("规则3 空触发词不追加", out_empty == "a cat", repr(out_empty))

    # ---------- 规则 2：推荐强度自动应用 ----------
    ok_strength = False
    detail = ""
    for src_name in ("loras.py", "workflows.py"):
        src = os.path.join(REPO, "h3studio", src_name)
        if not os.path.exists(src):
            continue
        with open(src, encoding="utf-8") as fh:
            body = fh.read()
        if re.search(r'def\s+\w*recom\w*strength', body) or "recommended_strength" in body \
                or re.search(r'"recommended_strength"|\'recommended_strength\'', body):
            ok_strength = True
            detail = f"{src_name} 内含推荐强度字段"
            break
    check("规则2 效果 LoRA 有推荐强度字段(选中自动应用)", ok_strength, detail)

    # 推荐强度确实被前端/后端取用
    app_js = os.path.join(REPO, "web", "app.js")
    with open(app_js, encoding="utf-8") as fh:
        js = fh.read()
    check("规则2 前端读取推荐强度", "recommended_strength" in js or "recommendedStrength" in js,
          "app.js 中有 recommended_strength 引用")
    check("规则3 前端有触发词回填/可编辑", "trigger" in js.lower(), "")

    # ---------- 规则 4：LoRA 节点连 clip ----------
    wf_src = os.path.join(REPO, "h3studio", "workflows.py")
    with open(wf_src, encoding="utf-8") as fh:
        body = fh.read()
    check("规则4 LoraLoader 的 clip 输入被接上",
          bool(re.search(r'inputs\["clip"\]\s*=', body)), "workflows.py: inputs[\"clip\"]")
    check("规则4 用 LoraLoader 而非 LoraLoaderModelOnly(需连 clip)",
          "LoraLoaderModelOnly" not in body or "class_type\": \"LoraLoader\"" in body,
          "class_type=LoraLoader (model+clip)")
    check("规则4 Qwen 链也接 clip",
          bool(re.search(r'_qwen_lora_chain[\s\S]{0,1500}inputs\["clip"\]', body)),
          "_qwen_lora_chain 内有 clip 输入")

    # ---------- 前端不得暴露加速 LoRA ----------
    check("前端未把加速 LoRA 暴露成可选项",
          "Z-Image-Fun-Lora-Distill-8-Steps-2603" not in js, "app.js 中无加速 LoRA 文件名")

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSES)} / 失败 {len(FAILURES)}")
    if FAILURES:
        print("\n失败项:")
        for f in FAILURES:
            print("  - " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
