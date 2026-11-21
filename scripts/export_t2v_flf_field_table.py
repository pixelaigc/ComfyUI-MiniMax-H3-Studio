#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产出 **t2v / flf / r2v 面板字段对照表**（2026-11-21 用户要求）。

【为什么需要这张表】
用户 2026-11-21 驳回 flf 面板的原话：「同时存在百万像素和分辨率挡位和比例，
还有底模精度，全部都是冲突的。和文生视频完全不一样好吧。」

修正之后必须能**一眼看出**三个面板的差异与一致 —— 靠人肉对比字段表
不现实（本表就是那张对比表）。

【这张表是从**运行时真源**导出的】
字段表真源 = h3studio/gen_defaults.py 的 _mode_fields(mode)，
与服务端下发给前端的 /api/generation-types 是同一份 ——
所以表里的内容就是用户界面上真实看到的内容。

用法：
  python3 scripts/export_t2v_flf_field_table.py
  # 输出 JSON + Markdown 到 evidence/t2v-flf-field-table/
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from h3studio import gen_defaults as g  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    ROOT / "evidence" / "t2v-flf-field-table")
OUT.mkdir(parents=True, exist_ok=True)

MODES = ["t2v", "flf", "r2v"]
MODE_LABEL = {
    "t2v": "文生视频",
    "flf": "首尾帧生视频",
    "r2v": "参考生视频",
}

#: 用户明确点名的几组语义对照（用于表格的「判定」列）
SIZE_FIELDS = {"aspect", "megapixels", "resolution_percent", "resolution"}
LORA_FIELDS = {"accel_lora", "lora", "lora_strength"}
FIXED_FIELDS = {"steps", "sampler", "scheduler", "shift_video", "shift_audio",
                "precision", "sampling_fixed", "shift_fixed", "precision_fixed"}


def collect():
    out = {}
    for mode in MODES:
        fields, _ = g._mode_fields(mode)
        rows = []
        for f in fields:
            name = f.get("name")
            opts = f.get("options") or []
            rows.append({
                "name": name,
                "label": f.get("label"),
                "type": f.get("type"),
                "default": f.get("default"),
                "options": [o.get("value") for o in opts if isinstance(o, dict)],
                "option_labels": [o.get("label") for o in opts if isinstance(o, dict)],
                "note": (f.get("note") or "").strip(),
            })
        out[mode] = rows
    return out


def build_table(data):
    """逐字段横向对照三个面板。"""
    names = []
    for mode in MODES:
        for row in data[mode]:
            if row["name"] not in names:
                names.append(row["name"])

    index = {mode: {r["name"]: r for r in data[mode]} for mode in MODES}
    lines = []
    lines.append("# t2v / flf / r2v 面板字段对照表")
    lines.append("")
    lines.append(f"字段表真源：`h3studio/gen_defaults.py::_mode_fields(mode)`"
                 f"（与服务端 `/api/generation-types` 下发给前端的同一份）。")
    lines.append("")
    header = "| 字段 | " + " | ".join(
        f"{MODE_LABEL[m]}（{m}）" for m in MODES) + " | 语义归类 |"
    lines.append(header)
    lines.append("|---|---|---|---|---|")
    for name in names:
        cells = []
        kinds = set()
        for mode in MODES:
            row = index[mode].get(name)
            if row is None:
                cells.append("—")
                continue
            kinds.add(row["type"])
            if row["type"] == "fixed":
                cells.append(f"`fixed` {row['label']}")
            elif row["type"] == "enum":
                cells.append(f"`enum` {row['label']}（{len(row['options'])} 项）")
            else:
                cells.append(f"`{row['type']}` {row['label']}")
        if name in SIZE_FIELDS:
            kind = "尺寸"
        elif name in LORA_FIELDS:
            kind = "加速 LoRA"
        elif name in FIXED_FIELDS:
            kind = "固定项（只读）"
        else:
            kind = "其他"
        lines.append(f"| `{name}` | " + " | ".join(cells) + f" | {kind} |")
    return "\n".join(lines)


def build_findings(data):
    """机器可判定的结论（供自动化断言用）。"""
    index = {mode: {r["name"]: r for r in data[mode]} for mode in MODES}
    findings = {}

    # 1) 尺寸表达唯一：flf/r2v 不得再出现「分辨率档位」
    for mode in ("flf", "r2v"):
        findings[f"{mode}_no_resolution_tier"] = (
            "resolution" not in index[mode])

    # 2) 尺寸字段与 t2v 同名同义
    for mode in ("flf", "r2v"):
        for key in ("aspect", "megapixels", "resolution_percent"):
            findings[f"{mode}_has_{key}"] = key in index[mode]

    # 3) 固定项只读：flf/r2v 的采样/精度项必须是 type=fixed
    for mode in ("flf", "r2v"):
        for key in ("sampling_fixed", "shift_fixed", "precision_fixed"):
            row = index[mode].get(key)
            findings[f"{mode}_{key}_is_fixed"] = bool(row) and row["type"] == "fixed"

    # 4) flf/r2v 不得暴露可编辑的精度 / steps / sampler / scheduler
    for mode in ("flf", "r2v"):
        for key in ("precision", "steps", "sampler", "scheduler",
                    "shift_video", "shift_audio"):
            findings[f"{mode}_no_editable_{key}"] = key not in index[mode]

    # 5) 加速 LoRA：flf/r2v 走「加速 LoRA」单枚 + 强度
    for mode in ("flf", "r2v"):
        findings[f"{mode}_has_lora"] = "lora" in index[mode]
        findings[f"{mode}_has_lora_strength"] = "lora_strength" in index[mode]

    # 6) 加速 LoRA 列表必须含 none，且不含第三方蒸馏档
    for mode in ("flf", "r2v"):
        row = index[mode].get("lora")
        opts = (row or {}).get("options") or []
        findings[f"{mode}_lora_has_none"] = "none" in opts
        third_party = [o for o in opts
                       if o and any(m in str(o).lower()
                                    for m in ("lightx2v", "comfy-org", "dmad"))]
        findings[f"{mode}_lora_no_third_party"] = not third_party
        findings[f"{mode}_lora_third_party_hits"] = third_party

    return findings


def main():
    data = collect()
    findings = build_findings(data)

    (OUT / "fields.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "findings.json").write_text(
        json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "t2v-flf-field-table.md").write_text(
        build_table(data), encoding="utf-8")

    print("=== 字段对照表 ===")
    print(build_table(data))
    print()
    print("=== 判定 ===")
    bad = [k for k, v in findings.items()
           if isinstance(v, bool) and not v]
    for k, v in findings.items():
        if isinstance(v, bool):
            print(f"  [{'OK ' if v else '!! '}] {k}")
    print(f"  （非布尔项）flf/r2v 的第三方 LoRA 命中："
          f"{findings.get('flf_lora_third_party_hits')} / "
          f"{findings.get('r2v_lora_third_party_hits')}")
    print()
    print(f"输出目录：{OUT}")
    if bad:
        print(f"!! {len(bad)} 项未通过：{bad}")
        return 1
    print("=== 全部通过 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
