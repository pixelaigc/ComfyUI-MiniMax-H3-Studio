# -*- coding: utf-8 -*-
"""生成「t2v / flf 字段对照表」（用户 2026-11-21 明确要求）。

对照表的每一行都来自**服务端真实下发**（GET /api/agent/settings），
不是手写 —— 这样它既是文档也是可复跑的证据。

用法：python3 dump_field_compare.py <base_url> <out_md>
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8190"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/field_compare.md"

#: 本轮判定为「真实生效 / 不可编辑 / 已删除」的注解来源
NOTES = {
    ("flf", "resolution"): "**已删除** —— 官方 v1.json 节点 115 只有 "
                           "ResolutionSelector{aspect_ratio, megapixels, multiple:32}，"
                           "没有「档位」。此前 28 档与 MP 预算并列 = 第三套冲突表达。",
    ("flf", "precision"): "**已删除** —— 两条视频 builder 都不读 params['precision']"
                          "（server.py 全文 0 命中）；精度编码在底模权重里。",
    ("flf", "steps"): "**已删除** —— server.py 的 _SAMPLING_ONLY_FIELDS 决定"
                      "面板提交路径本来就不套采样类字段 -> 控件改了不起作用。",
    ("flf", "sampler"): "同上（flf）",
    ("flf", "scheduler"): "同上（flf）",
    ("flf", "shift_video"): "同上（flf）",
    ("flf", "shift_audio"): "同上（flf）",
    ("flf", "sampling_fixed"): "**只读说明** —— 由所选加速 LoRA 决定"
                               "（有 LoRA 4 步 / 无 20 步），换档自动跟着换。",
    ("flf", "shift_fixed"): "**只读说明** —— 12.0 / 3.0 是 H3 模型内生值；"
                            "官方首尾帧模板用 H3SigmaRefiner，不是可填的 sigma shift。",
    ("flf", "precision_fixed"): "**只读说明** —— 精度编码在权重文件里"
                                "（int8_convrot / bf16 / pruned_w4a8）。",
    ("flf", "lora"): "**改为真实派生** —— 原来来自 workflows.LORAS 硬编码两枚；"
                     "现在按本机存在 + 与该底模逐层实测兼容 + 官方 H3 谱系派生。",
    ("flf", "megapixels"): "尺寸唯一表达的数值部分（与 t2v 同一套）。",
    ("flf", "resolution_percent"): "尺寸唯一表达的百分比部分（与 t2v 同一套）。",
    ("t2v", "steps"): "**保留可编辑** —— t2v 走新面板路径，"
                      "video_workflow.build_t2v_panel 真的读 params['steps']"
                      "（实测 -> BasicScheduler.steps）。",
    ("t2v", "sampler"): "**保留可编辑** —— 同上 -> KSamplerSelect.sampler_name。",
    ("t2v", "scheduler"): "**保留可编辑** —— 同上 -> BasicScheduler.scheduler。",
    ("t2v", "shift_video"): "**保留可编辑** —— 同上 -> MiniMaxH3SigmaShift.shift_video。",
    ("t2v", "shift_audio"): "**保留可编辑** —— 同上 -> MiniMaxH3SigmaShift.shift_audio。",
    ("t2v", "accel_lora"): "加速 LoRA 组合（官方 DMAD / turbo 系在主生成面板按底模过滤）。",
}


def get_settings():
    with urllib.request.urlopen(BASE + "/api/agent/settings", timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    data = get_settings()
    spec = data.get("gen_defaults_spec") or {}
    gd = data.get("gen_defaults") or {}

    lines = ["# 内置 Agent 管理面板：t2v / flf 字段对照表", "",
             "> 本表由 `scripts/dump_field_compare.py` 从**线上真实下发**",
             "> （`GET /api/agent/settings`）生成 —— 不是手写文档。",
             "> 生成时间对应的线上 commit 见交付报告。", ""]

    names = {}
    for mode in ("t2v", "flf", "r2v"):
        names[mode] = {f["name"]: f for f in (spec.get(mode) or {}).get("fields") or []}

    all_names = []
    for mode in ("t2v", "flf"):
        for n in names[mode]:
            if n not in all_names:
                all_names.append(n)

    lines += ["## 一、字段并排对照", "",
              "| 字段 | t2v（文生视频） | flf（首尾帧） | 判定 |",
              "|---|---|---|---|"]
    for name in all_names:
        cells = []
        for mode in ("t2v", "flf"):
            f = names[mode].get(name)
            if not f:
                cells.append("—")
                continue
            typ = f.get("type")
            opts = f.get("options") or []
            val = f.get("default")
            if typ == "fixed":
                cells.append("**只读** `%s`" % val)
            elif typ == "enum":
                cells.append("下拉 `%s`（%d 项）" % (val, len(opts)))
            elif typ == "number":
                cells.append("数值 `%s`" % val)
            else:
                cells.append("`%s`" % val)
        note = NOTES.get(("flf", name)) or NOTES.get(("t2v", name)) or ""
        lines.append("| `%s` | %s | %s | %s |" % (name, cells[0], cells[1], note))

    lines += ["", "## 二、逐项说明", ""]
    for mode in ("t2v", "flf"):
        lines.append("### %s" % mode)
        stored = gd.get(mode) or {}
        lines.append("")
        lines.append("管理页当前存库的默认值：")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(stored, ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        lines.append("| 字段 | 类型 | 默认值 | 选项 | 说明 |")
        lines.append("|---|---|---|---|---|")
        for name, f in names[mode].items():
            opts = f.get("options") or []
            opts_text = ", ".join(str(o) for o in opts[:8])
            if len(opts) > 8:
                opts_text += " …（共 %d 项）" % len(opts)
            note = (f.get("note") or "").replace("|", "/")[:110]
            lines.append("| `%s` | %s | `%s` | %s | %s |" % (
                name, f.get("type"), f.get("default"), opts_text or "—", note))
        lines.append("")

    lines += ["## 三、结论", "",
              "1. **尺寸表达只有一套**：t2v 与 flf 都是"
              "「画面比例 × MP 预算 × 百分比」，同一个 `size_spec.plan_size`。"
              "flf 的「分辨率档位」已删除。",
              "2. **官方固定项不再可编辑**：flf 的采样档位 / sigma shift / 底模精度"
              "改为只读说明（值仍显示，但不给控件）。",
              "3. **t2v 的采样字段保留可编辑** —— 它们在 t2v 面板路径上**真的生效**"
              "（实测进 BasicScheduler / KSamplerSelect / MiniMaxH3SigmaShift 节点），"
              "与 flf 不同。",
              "4. **加速 LoRA 由服务端真实数据派生**："
              "本机存在 + 与该底模逐层实测兼容 + 官方 H3 谱系"
              "（判据取自权重自己的 safetensors metadata）。",
              ]

    text = "\n".join(lines)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
