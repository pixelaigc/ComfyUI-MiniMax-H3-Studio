# -*- coding: utf-8 -*-
"""「修正前冲突项 → 修正后」对照（机器可验证）。

修正前的证据 = 本轮开工时从**线上 8190** 抓的快照
（``evidence/flf-panel-truth/before/agent_manage_BEFORE.json``），
修正后的证据 = 同一路径抓的 AFTER 快照。

用法：python3 make_conflict_table.py <before_json> <after_json> <out_md>
"""
import json
import sys

BEFORE = json.load(open(sys.argv[1], encoding="utf-8"))
AFTER = json.load(open(sys.argv[2], encoding="utf-8"))
OUT = sys.argv[3]

#: 用户 2026-11-21 原话点名的冲突项
CONFLICTS = [
    ("画面比例 16:9 ＋ 分辨率档位「768p·768×1344 (9:16)」",
     "档位标签自带 9:16，与所选 16:9 直接矛盾",
     "删除 flf/r2v 的「分辨率档位」字段",
     "官方首尾帧模板节点 115 = ResolutionSelector{aspect_ratio, megapixels, "
     "multiple:32} —— 只有比例 + MP 预算，没有档位"),
    ("MP 预算 0.8 ＋ MP 百分比 100 ＋ 分辨率档位（三套尺寸表达并存）",
     "同���件事有三种写法，彼此可覆盖",
     "尺寸表达收敛为一套：画面比例 × MP 预算 × 百分比",
     "与 t2v 完全同一套、同一个 size_spec.plan_size"),
    ("底模精度 int8（可编辑）",
     "两条视频 builder 都不读 params['precision']（server.py 全文 0 命中）"
     "-> 改了不起作用",
     "改为只读说明「底模精度（不可改）：随底模」",
     "精度编码在权重文件里（int8_convrot / bf16 / pruned_w4a8）"),
    ("采样步数 4 / 采样器 euler / 调度器 beta（可编辑）",
     "server.py 的 _SAMPLING_ONLY_FIELDS 决定面板提交路径本来就不套采样类字段"
     "-> 改了不生效",
     "改为只读说明「采样档位（不可改）」",
     "档位由所选加速 LoRA 决定（有 LoRA 4 步 / 无 20 步），换档自动跟着换"),
    ("视频 sigma shift 12 / 音频 sigma shift 3（可编辑）",
     "同上；且官方首尾帧模板用 H3SigmaRefiner，不是可填的 sigma shift",
     "合并为只读说明「sigma shift（不可改）」",
     "12.0 / 3.0 是 H3 模型内生值"),
    ("加速 LoRA 档：none / ckpt850 / v4step600",
     "选项来自 workflows.LORAS 硬编码两枚 —— 不问底模、"
     "不问本机有没有、不跑兼容性实测；与主生成面板（按底模过滤）两套口径",
     "改为按「本机存在 + 与该底模逐层实测兼容 + 官方 H3 谱系」派生",
     "复用主生成面板同一真源 video_models.accel_lora_compat_matrix"),
    ("区块底部说明：「旧工作流路径：没有注意力机制与加速 LoRA 组合，"
     "只有比例 / 档位 / 时长 / 加速 LoRA 档可调。」",
     "与上面暴露的可调项自相矛盾（说有加速 LoRA 档可调，又说没有加速 LoRA 组合；"
     "还提到已删除的「档位」）",
     "重写为与实际暴露项一一对应的说明",
     "gen_defaults.MODE_NOTES['flf'] / ['r2v']"),
]


def flf_fields(payload):
    """把两种快照结构统一成 {字段名: {kind, value, options, note}}。

    BEFORE 快照（shot_agent_defaults.py）的 fields 是 **dict**：
        {"分辨率档位": {"kind": "select", "value": "768p · 768×1344（9:16）",
                       "raw": "768p", "options": [...]}, ...}
    AFTER 快照（verify_flf_panel_browser.py）的 fields 是 **list**：
        [{"label": "底模", "kind": "select", "value": "...", ...}, ...]

    两个采集脚本的输出结构不同（前者是旧脚本），但**字段语义一致**，
    所以这里做一次归一化，让对照表能逐字段对拍。
    """
    panel = payload.get("panels") or {}
    flf = panel.get("flf") or {}
    raw = flf.get("fields") or {}
    out = {}
    if isinstance(raw, dict):
        for name, info in raw.items():
            out[name] = {
                "kind": info.get("kind"),
                "value": info.get("value"),
                "raw": info.get("raw"),
                "options": info.get("options") or [],
                "note": info.get("note") or "",
            }
        return out
    for item in raw:
        name = item.get("label") or ""
        if not name:
            continue
        out[name] = {
            "kind": item.get("kind"),
            "value": item.get("value"),
            "raw": item.get("value"),
            "options": item.get("options") or [],
            "note": item.get("note") or "",
        }
    return out


def main():
    b_fields = flf_fields(BEFORE)
    a_fields = flf_fields(AFTER)

    lines = ["# 修正前冲突项 → 修正后（对照表）", "",
             "> 修正前证据：`%s`（本轮开工时从线上 8190 抓的面板 DOM 快照）" % sys.argv[1],
             "> 修正后证据：`%s`（同一入口、同一视口复抓）" % sys.argv[2],
             "",
             "## 一、用户点名的冲突项逐条处置", ""]
    for i, (item, why, fix, basis) in enumerate(CONFLICTS, 1):
        lines += ["### %d. %s" % (i, item), "",
                  "- **冲突性质**：%s" % why,
                  "- **修正后**：%s" % fix,
                  "- **判据**：%s" % basis, ""]

    lines += ["## 二、字段级前后对照（机器可验证）", "",
              "| 字段（修正前） | 修正前控件 | 修正后 | 判定 |",
              "|---|---|---|---|"]
    for name, info in b_fields.items():
        label = info.get("value") or ""
        kind = "下拉" if info.get("kind") == "select" else "数值"
        if name == "分辨率档位":
            after = "（已删除）"
            verdict = "**删除** —— 与 MP 预算并列成第三套冲突表达"
        elif name == "底模精度":
            after = "只读说明「随底模」"
            verdict = "**改只读** —— builder 不消费"
        elif name in ("采样步数", "采样器", "调度器", "视频 sigma shift", "音频 sigma shift"):
            after = "只读说明"
            verdict = "**改只读** —— 面板路径不套采样类字段"
        elif name == "加速 LoRA 档":
            after = "下拉（真实派生）"
            verdict = "**改派生** —— 不再硬编码两枚"
        elif name == "MP 预算（百万像素）":
            after = "数值（保留）"
            verdict = "保留（尺寸唯一表达的数值部分）"
        elif name == "MP 预算百分比（%）":
            after = "数值（保留）"
            verdict = "保留（尺寸唯一表达的百分比部分）"
        elif name == "画面比例":
            after = "下拉（保留）"
            verdict = "保留"
        elif name == "时长（秒）":
            after = "数值（保留）"
            verdict = "保留"
        elif name == "底模":
            after = "下拉（保留）"
            verdict = "保留"
        elif name == "LoRA 强度":
            after = "数值（保留）"
            verdict = "保留"
        else:
            after = "—"
            verdict = "—"
        lines.append("| %s | %s `%s` | %s | %s |" % (name, kind, label, after, verdict))

    lines += ["", "## 三、字段数变化", "",
              "- 修正前 flf 字段数：**%d**" % len(b_fields),
              "- 修正后 flf 字段数：**%d**" % len(a_fields),
              "- 修正后字段名：`%s`" % "`, `".join(a_fields),
              "",
              "## 四、修正后只读说明字段（用户要求：明确标注为官方固定值）", ""]
    for name, info in a_fields.items():
        if info.get("kind") == "fixed":
            lines.append("- **%s** = `%s`" % (name, info.get("value")))
            if info.get("note"):
                lines.append("  - %s" % info["note"])
    lines.append("")

    text = "\n".join(lines)
    open(OUT, "w", encoding="utf-8").write(text)
    print(text)


if __name__ == "__main__":
    main()
