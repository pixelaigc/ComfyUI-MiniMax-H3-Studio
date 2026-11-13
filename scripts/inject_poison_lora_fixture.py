# -*- coding: utf-8 -*-
"""
向**验收实例**的 jobs.db 注入一条「LoRA 字段被提示词污染」的任务。

【为什么需要这个脚本】
用户 2026-11-13 报出的 BUG 1 是：「卡片显示的 LoRA 根本不是该任务用过的 LoRA
（例如显示 integrated_multimodal_description 之类的提示词类字段）」。

线上库（8190 / /root/projects/comfyui-minimax-h3-studio/data/jobs.db）里
**没有**这样一条被污染的历史记录 —— 实测 157 条任务里 params.lora 全是
合法值（ckpt850 / v4step600 / none），非提示词字段里也搜不到
integrated_multimodal。

所以要证明「修复真的挡住了这个场景」，必须在验收实例的**副本**库里
造出一条被污染的任务，然后看卡片上显示什么。

【安全边界】
- 只写 H3STUDIO_DATA_DIR 指向的**验收实例副本**（/tmp/h3media-verify/data/jobs.db）；
- 绝不碰线上 8190 的库。
- 脚本默认拒绝写非 /tmp/ 下的路径（防止误伤线上库）。

用法：
    python3 scripts/inject_poison_lora_fixture.py /tmp/h3media-verify/data/jobs.db
"""

import json
import sqlite3
import sys
import time
from pathlib import Path


#: 用户报出的那段提示词（原样取自真实任务的 params.prompt）。
POISON_PROMPT = (
    "integrated_multimodal_description: [Shot 1] Live-action, cinematic Chinese "
    "wuxia period film. At night in a moonlit bamboo forest, a tight close-up "
    "frames an adult swordsman in black robes (S1) from the white-robed "
    "swordsman's shoulder, forming the first angle of a shot-reverse-shot "
    "sequence. Cold mist drifts behind him."
)

POISON_JOB_ID = "poisonlora0001"


def build_params() -> dict:
    """构造一条「除 lora 字段外一切正常」的 t2v 任务参数。"""
    return {
        "prompt": POISON_PROMPT,
        "original_prompt": POISON_PROMPT,
        # <- 污染点：Agent 把提示词误填进了 lora 字段
        "lora": POISON_PROMPT,
        "lora_strength": 1.0,
        "aspect": "16:9",
        "resolution": "768p",
        "duration": 5.0,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "shift_video": 7.0,
        "shift_audio": 3.0,
        "seed": 123456,
        "gen_type": "t2v",
        "model": "lora",
    }


def inject(db_path: Path) -> dict:
    """插入（或覆盖）那条被污染的任务。"""
    params = build_params()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """insert or replace into jobs
               (id, created_at, mode, params, status, progress, progress_text,
                error, outputs, started_at, finished_at, session_id)
               values (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (POISON_JOB_ID, time.time() - 3600, "t2v",
             json.dumps(params, ensure_ascii=False),
             "done", 1.0, "", None, "[]",
             time.time() - 3500, time.time() - 3400, ""))
        conn.commit()
        row = conn.execute(
            "select id, mode, params from jobs where id = ?",
            (POISON_JOB_ID,)).fetchone()
        stored = json.loads(row[2])
        return {
            "id": row[0],
            "mode": row[1],
            "lora_field_is_prompt": "integrated_multimodal" in str(stored["lora"]),
        }
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    db_path = Path(argv[1]).resolve()
    # 安全边界：只允许写 /tmp 下的库（验收实例副本），挡住线上库。
    if not str(db_path).startswith("/tmp/"):
        print(f"拒绝写入非 /tmp 路径：{db_path}\n"
              "（线上 8190 的库不能被本脚本改动）")
        return 1
    if not db_path.exists():
        print(f"找不到库：{db_path}")
        return 1
    result = inject(db_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
