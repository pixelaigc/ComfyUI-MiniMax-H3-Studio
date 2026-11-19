#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「线上生产 OOM 形状」的 ComfyUI history 条目（供 evidence_oom_failure_e2e.py 回放）。

【为什么要合成这条】

线上库里有三条**真实的误报样本**（修复前）：628d92cfc062 / 86db94915467 /
c5c3f63343b8 —— status='done' + outputs=[] + error 里明确写着「显存不足
（Allocation on device 0 would exceed allowed memory. (out of memory)）」。

它们的 ComfyUI 侧 history 在本机已经**被清掉**（ComfyUI 重启会丢 history），
所以拿不到原始条目。本脚本按 ComfyUI 的**真实形状**重建一条：

    status.status_str = 'error'
    status.messages   = [['execution_error', {... 'out of memory' ...}]]
    outputs           = {'301': {'text': [...]}}   <- **只有文本节点**

最后那行是关键：**ComfyUI 在执行失败时也会往 outputs 里塞已执行完节点的
输出**（哪怕只是一个文本节点）。这正是原缺陷的触发条件 ——
旧逻辑 `if hist.get("outputs"):` 非空 -> 判「成功」。

error 文本取自线上真实样本 628d92cfc062 的 jobs.error（原文，未改写）。

用法：python3 scripts/build_oom_history_fixture.py
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path("/root/projects/h3s-merge-20261119/evidence/gen-status-20261119")

#: 线上真实误报样本 628d92cfc062 的 ComfyUI prompt_id
PROMPT_ID = "4e8e6b0a-9cd8-4171-92ca-041a7abe1b39"

#: ComfyUI 报的原始 OOM 文本（与线上样本 jobs.error 里的英文原文一致）
OOM_MESSAGE = (
    "ERROR: Allocation on device 0 would exceed allowed memory. "
    "(out of memory).\n\n"
)


def build() -> dict:
    return {
        "prompt": [
            11,
            PROMPT_ID,
            {
                "14": {
                    "class_type": "SamplerCustomAdvanced",
                    "inputs": {},
                    "_meta": {"title": "采样"},
                },
            },
        ],
        # 【原缺陷的触发条件】失败时 outputs 里**仍然非空**（文本节点）。
        # 旧逻辑只看它非空 -> 判「成功」-> 前端显示全黑预览。
        "outputs": {
            "301": {"text": ["Prompt executed in 12.4 seconds\n"]},
        },
        "status": {
            "status_str": "error",
            "completed": False,
            "messages": [
                ["execution_start", {
                    "prompt_id": PROMPT_ID,
                    "timestamp": 1795071200000,
                }],
                ["execution_cached", {
                    "nodes": [],
                    "prompt_id": PROMPT_ID,
                    "timestamp": 1795071200004,
                }],
                ["execution_error", {
                    "prompt_id": PROMPT_ID,
                    "node_id": "14",
                    "node_type": "SamplerCustomAdvanced",
                    "exception_message": OOM_MESSAGE,
                    "exception_type": "torch.cuda.OutOfMemoryError",
                    "traceback": [],
                    "executed": ["13", "105:107", "17", "6", "136:301",
                                 "105:111", "301"],
                }],
            ],
        },
        "meta": {
            "note": (
                "按 ComfyUI 真实形状重建的生产 OOM 条目。"
                "prompt_id / error 文本取自线上误报样本 628d92cfc062；"
                "outputs 里只有文本节点 —— 这正是旧逻辑"
                "（`if hist.get(\"outputs\")`）误判「成功」的触发条件。"
            ),
            "source_job_id": "628d92cfc062",
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    entry = build()
    path = OUT / "oom_history_production_shape.json"
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    print(f"已写: {path}")
    print(f"  status_str        = {entry['status']['status_str']!r}")
    print(f"  outputs keys      = {list(entry['outputs'].keys())}")
    print(f"  outputs 非空       = {bool(entry['outputs'])}"
          "  <- 旧逻辑会据此误判「成功」")
    print(f"  含 out of memory  = "
          f"{'out of memory' in json.dumps(entry, ensure_ascii=False).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
