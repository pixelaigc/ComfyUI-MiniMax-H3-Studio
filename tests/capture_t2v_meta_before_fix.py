# -*- coding: utf-8 -*-
"""把「修复前的错误行为」固化成一个**可复现**的证据脚本。

【为什么需要这个】

用户报的现象是「面板选了混合版 + DMAD LoRA，卡片却显示 ckpt850」。
修复之后这条链路就对了 —— 但「错的时候到底错成什么样」需要**留档**，
否则以后没人能说清这个 bug 的具体形态。

这个脚本用**修复前的旧逻辑**（读遗留字段 p.model / p.lora），
对线上真实任务 410655e60532 的记录算一遍卡片文案，
并与「面板实际所选」「ComfyUI 实际收到」三方对照。

注意：这里**不改动**仓库里的 app.js —— 旧逻辑以副本形式内联在本脚本里，
保持历史形态可复现。

运行：python3 tests/capture_t2v_meta_before_fix.py
"""
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

#: 线上真实任务（用户 2026-11-12 09:33 跑的混合版 + DMAD 那条）
JOB_ID = "410655e60532"
#: 线上任务库（config.json 的 data_dir）
LIVE_DB = Path("/root/projects/comfyui-minimax-h3-studio/data/jobs.db")


def old_model_label(p: dict) -> str:
    """修复前 web/app.js 的 modelLabel(p) —— 逐字复刻。"""
    steps = p.get("steps") or (20 if p.get("model") == "standard" else 4)
    if p.get("model") == "standard":
        return f"标准 H3 · {steps}步"
    lora = "ckpt850" if p.get("lora") == "ckpt850" else "v4-step600"
    return f"H3 + {lora} · {steps}步"


def main() -> int:
    if not LIVE_DB.exists():
        print(f"找不到线上任务库：{LIVE_DB}")
        return 2
    conn = sqlite3.connect(LIVE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, mode, status, params FROM jobs WHERE id=?",
                       (JOB_ID,)).fetchone()
    if row is None:
        print(f"任务库里没有 {JOB_ID}")
        return 2
    p = json.loads(row["params"] or "{}")

    from h3studio import job_meta
    from h3studio import video_models as VM

    model_meta = VM.VIDEO_MODELS.get(p.get("model_key") or "", {})
    panel_label = model_meta.get("label", "")
    panel_unet = model_meta.get("unet", "")
    accel = (p.get("accel_loras") or [{}])[0]
    accel_meta = VM.VIDEO_ACCEL_LORA_COMBOS.get(accel.get("key") or "", {})
    panel_lora_label = accel_meta.get("display", "")
    panel_lora_name = accel_meta.get("name", "")
    panel_lora_strength = accel.get("strength")

    old = old_model_label(p)
    new = job_meta.summarize(row["mode"], p)

    report = {
        "job_id": JOB_ID,
        "mode": row["mode"],
        "status": row["status"],
        "panel_selected": {
            "model_label": panel_label,
            "model_unet": panel_unet,
            "accel_lora_label": panel_lora_label,
            "accel_lora_name": panel_lora_name,
            "accel_lora_strength": panel_lora_strength,
        },
        "legacy_fields_stored_in_db": {
            "model": p.get("model"),
            "lora": p.get("lora"),
            "lora_strength": p.get("lora_strength"),
        },
        "correct_fields_stored_in_db": {
            "model_key": p.get("model_key"),
            "accel_loras": p.get("accel_loras"),
            "fixed_accel_lora": p.get("fixed_accel_lora"),
        },
        "card_before_fix": old,
        "card_after_fix": new["model_label"],
        "lora_row_after_fix": (new["lora_label"] or "空"),
        "verdict": {
            "panel_and_db_agree": (p.get("model_key") == (panel_unet and p.get("model_key"))),
            "card_before_fix_wrong": old != panel_label,
            "card_after_fix_correct": new["model_label"] == panel_label,
        },
    }

    out = Path(__file__).resolve().parent.parent.parent / (
        "evidence-t2v-meta/before-fix-behavior.json")
    out = Path("/root/.hermes-web-ui/.ekko/workspace/default/"
               "person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/"
               "evidence-t2v-meta/before-fix-behavior.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    print("=" * 74)
    print(f"修复前后的行为对照 —— 线上真实任务 {JOB_ID}")
    print("=" * 74)
    print(f"面板实际所选底模    : {panel_label}")
    print(f"面板实际所选加速LoRA: {panel_lora_label} ×{panel_lora_strength}")
    print(f"落库里的遗留字段     : model={p.get('model')!r} lora={p.get('lora')!r}"
          f" lora_strength={p.get('lora_strength')!r}   ← 污染源")
    print(f"落库里的正确字段     : model_key={p.get('model_key')!r}")
    print(f"                      accel_loras={p.get('accel_loras')}")
    print()
    print(f"修复前 卡片「模型」行 : {old}          ← 错（用户看到的）")
    print(f"修复后 卡片「模型」行 : {new['model_label']}")
    print(f"修复后 卡片「LoRA」行 : {report['lora_row_after_fix']}")
    print()
    print(f"证据已写入: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
