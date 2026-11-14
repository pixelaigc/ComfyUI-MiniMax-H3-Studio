#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史任务的「产物实际元数据」回填（2026-11-15）。

【为什么需要】
2026-11-15 之前生成的任务，jobs.params 里**没有**产物实际参数
（步数 / 调度器 / 采样器 / 实际挂载的 LoRA）。它们是 worker 在
「构建工作流时算默认值、但没把算完的值落库」的漏洞产物。

修好之后的任务由 worker 自动回写（server.py 的
`artifact_meta.apply_to_params`）。但**存量**任务不会自己变 ——
要么补，要么让它们诚实地显示「未记录」。

【本脚本做什么】
从 ComfyUI 的 /history/{prompt_id} 里取出**当时实际执行的 workflow**，
用 h3studio/artifact_meta 反解出真实参数，回写进 jobs.params。

【关键限制 —— 必须如实告知】
ComfyUI 的 history 是**有限保留**的（默认只留最近的若干条）。
取不到的那条 -> **无法回填**，该任务只能显示「未记录」。
脚本**不会猜**：宁可留空，也不写一个可能是错的 LoRA 名。

【用法】
    python3 scripts/backfill_artifact_meta.py --db <jobs.db 路径> [--dry-run]
    python3 scripts/backfill_artifact_meta.py --db <jobs.db 路径> --comfy http://127.0.0.1:8188

【输出】
一份 JSON 报告（stdout）：{"scanned": N, "backfilled": N, "no_history": N, ...}
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from h3studio import artifact_meta  # noqa: E402


def fetch_history(comfy_url: str, prompt_id: str, timeout: float = 20.0):
    """取一条 ComfyUI history；取不到返回 None（**不猜**）。"""
    url = f"{comfy_url.rstrip('/')}/history/{prompt_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    entry = data.get(prompt_id)
    if not isinstance(entry, dict):
        return None
    prompt = entry.get("prompt")
    # ComfyUI 的 prompt 是 [number, prompt_id, workflow, extra, outputs]
    if isinstance(prompt, list) and len(prompt) > 2 and isinstance(prompt[2], dict):
        return prompt[2]
    if isinstance(prompt, dict):
        return prompt
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="回填历史任务的产物实际元数据")
    parser.add_argument("--db", required=True, help="jobs.db 路径")
    parser.add_argument("--comfy", default="http://127.0.0.1:8188",
                        help="ComfyUI 地址")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告不写库")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多处理多少条（0 = 不限）")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"[backfill] 找不到任务库：{db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, mode, status, prompt_id, params FROM jobs "
        "WHERE prompt_id IS NOT NULL AND prompt_id != ''").fetchall()

    report = {
        "scanned": 0,
        "backfilled": 0,
        "already_present": 0,
        "no_history": 0,
        "no_meta_in_history": 0,
        "failed": 0,
        "details": [],
    }

    for row in rows:
        report["scanned"] += 1
        if args.limit and report["scanned"] > args.limit:
            break
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        if not isinstance(params, dict):
            params = {}
        # 已经回填过的 -> 跳过
        if "artifact_loras" in params:
            report["already_present"] += 1
            continue

        workflow = fetch_history(args.comfy, row["prompt_id"])
        if workflow is None:
            report["no_history"] += 1
            continue
        extracted = artifact_meta.extract_from_workflow(workflow)
        if not extracted.get("loras") and extracted.get("steps") is None:
            report["no_meta_in_history"] += 1
            continue

        artifact_meta.apply_to_params(params, extracted)
        if not args.dry_run:
            conn.execute("UPDATE jobs SET params=? WHERE id=?",
                         (json.dumps(params, ensure_ascii=False), row["id"]))
        report["backfilled"] += 1
        report["details"].append({
            "id": row["id"],
            "mode": row["mode"],
            "steps": extracted.get("steps"),
            "scheduler": extracted.get("scheduler"),
            "sampler": extracted.get("sampler"),
            "loras": [item["name"] for item in extracted.get("loras") or []],
        })

    if not args.dry_run:
        conn.commit()
    conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
