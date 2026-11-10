#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校验器「括号权重语法」真伪阳性复核 —— 审查 P1-x 的专项验证。

【要证明两件事】
  A. 术语注释**不误判**（不该被当 Compel 权重语法拒掉）：
       (arcade) / (tong lau) / (shophouse) / (Tong Lau) / (gold) ...
  B. 真正的强调语法**不放过**：
       (word:1.5) / (word) / ((word)) / [word] / word++ / word---

【走的是真实入口】validate.check_weight_syntax(text, mode) ——
即校验器实际用来判「拒收 / 不拒收」的那条路径，
不是只测内部的 _is_weight_paren()（那条只管圆括号一种）。

【判据来源】validate.py 的 _is_weight_paren()：单个词只有在
**括号外也出现**（词频 >= 2）时才判为权重语法 ——
这是一条**语义**判据，不依赖具体上下文列表。

用法：
    /home/ubuntu/comfyui/venv/bin/python tests/verify_weight_syntax_rules.py \
        --validate <path-to-validate.py> [--out <json>]
"""
import argparse
import importlib.util
import json
import sys

# (标签, 文本, 期望是否被拒收)
#   False = 是说明性注释，**不该**被拒（误判）
#   True  = 是真权重语法，**应该**被拒（漏判）
CASES = [
    # ---- A. 术语注释：不该误判 ----
    ("术语英文名", "a row of cramped T-tong lau (shophouse) buildings", False),
    ("术语英文名-小写", "Tong lau (arcade) buildings on the left", False),
    ("术语转写-粤拼", "arcade buildings (tong lau) featuring rusty bars", False),
    ("术语转写-大写", "arcade buildings (Tong Lau) featuring rusty bars", False),
    ("逐字注音", 'neon signs reading "金" (gold) and "記" (record)', False),
    ("逐字注音-酒", 'a hanging sign "酒" (wine) above her head', False),
    ("纯中文注释", "（红色霓虹灯招牌）在头顶上方", False),
    ("中文+英文混合注释", "骑楼 (tong lau) 建筑的生锈铁栏杆", False),
    ("普通英文补充", "partially obscured or fragmented signage", False),
    ("普通英文补充2", "a (partially obscured) poster on the wall", False),
    ("术语英文名-无连字符", "the old tong lau (arcade) street", False),

    # ---- B. 真权重语法：不该放过 ----
    ("显式倍率", "a cat (cat:1.5) sitting on a mat", True),
    ("显式倍率-数字", "a cat (cat:1.2) in the street", True),
    ("纯数字倍率", "a cat (1.5) in the street", True),
    ("单词强调-重复", "a cat sitting nearby, (cat) on a stool", True),
    ("单词强调-重复-两次", "neon (neon) signs everywhere", True),
    ("括号套括号", "a ((cat)) sitting on a mat", True),
    ("方括号强调", "a [cat] sitting on a mat", True),
    ("双下划线强调", "cat++ in the frame", True),
    ("双减号强调", "cat--- in the frame", True),
]


def load_validate(path):
    spec = importlib.util.spec_from_file_location("h3_validate", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 validate.py: %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", required=True, help="validate.py 的路径")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    mod = load_validate(args.validate)
    check = mod.check_weight_syntax

    results = []
    passed = 0
    failed = 0
    for label, text, expect in CASES:
        hits = check(text, mode="pe")
        got = bool(hits)
        ok = (got == expect)
        passed += 1 if ok else 0
        failed += 0 if ok else 1
        results.append({"case": label, "text": text,
                        "expect_reject": expect, "got_reject": got,
                        "hits": hits, "ok": ok})
        print("%s %-22s expect=%-5s got=%-5s hits=%-18s %s"
              % ("OK  " if ok else "FAIL", label, expect, got,
                 str(hits)[:18], text[:56]))

    print()
    print("passed=%d failed=%d" % (passed, failed))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"passed": passed, "failed": failed,
                       "results": results}, f, ensure_ascii=False, indent=2)
        print("saved ->", args.out)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
