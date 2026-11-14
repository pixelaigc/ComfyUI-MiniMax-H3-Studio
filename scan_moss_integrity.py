#!/usr/bin/env python3
"""MOSS 族全量完整性扫描（2026-11-15 修复轮）。

用途：核实「D2 是不是同类静默失效/损坏的唯一一条」。
口径 = h3studio.moss_lora_compat.read_safetensors_header_detail
      （文件实际大小必须覆盖头部声明的数据区）。
"""
import glob
import json
import os
import sys

sys.path.insert(0, '/root/projects/h3studio-moss-lora-fix')
from h3studio import moss_lora_compat as mlc  # noqa: E402

DIRS = [
    "/mnt/usb2t/comfyui-models/loras/moss",
    "/mnt/usb2t/comfyui-models/loras/moss-effects",
]

rows = []
for d in DIRS:
    for p in sorted(glob.glob(f"{d}/*.safetensors")):
        det = mlc.read_safetensors_header_detail(p)
        row = {
            "dir": os.path.basename(d),
            "file": os.path.basename(p),
            "size": os.path.getsize(p),
            "readable": det["ok"],
            "reason": det["reason"],
        }
        if det["ok"]:
            header = det["header"]
            raw = [k for k in header if k != "__metadata__"]
            row["keys"] = len(raw)
            row["shape"] = mlc.detect_qkv_shape(header)
            row["needs_normalize"] = any(mlc.normalize_key(k) != k for k in raw)
        rows.append(row)

out = {
    "total": len(rows),
    "damaged": [r for r in rows if not r["readable"]],
    "damaged_count": sum(1 for r in rows if not r["readable"]),
    "rows": rows,
}
print(json.dumps(out, ensure_ascii=False, indent=1))
