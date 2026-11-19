#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 修复补丁（第二轮）：「出处」/「说明」里夹带的**源码表达式**片段。

【缺陷】h3studio/model_recommendations.py 的 4 条官方权重推荐里，
「出处」字段原文照抄了工作流里的 if 表达式：

    （default_steps = 4 if lora != 'none' else 20；
     default_scheduler = 'beta' if lora != 'none' else 'simple'；
     default_sampler = 'euler' if lora != 'none' else 'res_multistep'）

这段**源码**会原样渲染到前端的「出处：」里（悬停提示 / 任务详情弹窗）
—— 实测线上 DOM 可见（evidence/r7-dom-visible-audit/dom_visible_attrs.txt）。

【修法】改成人话（与 ref2va 那两条的既有写法一致）：
    （不挂加速 LoRA 时 = 20 步 / simple / res_multistep）

【为什么保留文件名+行号】第 2 轮（R2）已定口径：「出处」的价值是
可追溯（哪个文件第几行），只砍目录层级、保留文件名+行号。
本轮只砍**代码表达式**，不动溯源锚点。

用法：python3 scripts/r7_apply_fixes2.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK_ONLY = "--check" in sys.argv
MR = REPO / "h3studio" / "model_recommendations.py"


OLD_CODE = (
    '            "（default_steps = 4 if lora != \'none\' else 20；"\n'
    '            "default_scheduler = \'beta\' if lora != \'none\' else \'simple\'；"\n'
    '            "default_sampler = \'euler\' if lora != \'none\' else \'res_multistep\'）"\n'
)

NEW_CODE = (
    '            "（不挂加速 LoRA 时 = 20 步 / simple / res_multistep）"\n'
)

OLD_NOTE = '            "（h3studio/workflows.py:347-350，lora==\'none\' 分支），"\n'
NEW_NOTE = '            "（h3studio/workflows.py:347-350，不挂加速 LoRA 的分支），"\n'


def main() -> int:
    with open(MR, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()

    n_code = text.count(OLD_CODE)
    n_note = text.count(OLD_NOTE)
    if n_code != 4:
        raise SystemExit(f"[F6] 期望 4 处源码表达式，实际 {n_code} 处")
    if n_note != 4:
        raise SystemExit(f"[F6b] 期望 4 处 note 里的 lora=='none'，实际 {n_note} 处")

    text = text.replace(OLD_CODE, NEW_CODE)
    text = text.replace(OLD_NOTE, NEW_NOTE)

    if not CHECK_ONLY:
        with open(MR, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    print(("检查模式（未落盘）: " if CHECK_ONLY else "已应用: ")
          + f"F6 「出处」去掉源码表达式（{n_code} 处）；"
          + f"F6b 「说明」里的 lora=='none' 改成人话（{n_note} 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
