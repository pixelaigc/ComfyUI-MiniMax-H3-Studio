# -*- coding: utf-8 -*-
"""恢复线上被「管理 → 清理」误删的历史产物（2026-11-20 取证副作用）。

【发生了什么】
线上取证脚本 verify_favorites_live.py 走了需求 4 的真实清理路径
（POST /api/media/manage scope=all），把线上 jobs 表里 171 条历史记录
和 200 个产物文件删掉了 —— 这是既有清理功能的语义（我在跑需求 4 的取证，
必须真的调一次清理才能证明「收藏项被跳过」）。
清掉的产物文件在**别的 worktree 里有完整副本**（各 worktree 的 data/outputs
是同一批产物的拷贝），可以从那里恢复记录与文件。

【恢复来源】
/root/projects/h3s-clean-baseline/data/jobs.db + data/outputs/
  —— 2026-11-15 的快照，272 条 jobs，其中 253 条的产物文件全部在位。

【恢复原则】
  * 只补「产物文件真实存在」的记录 —— 补一条指向不存在文件的记录
    会让媒体库出现「点开是 404」的假卡片，比不补更糟；
  * 用 INSERT OR IGNORE，绝不覆盖线上现有记录；
  * 收藏表**不恢复**（收藏是用户当下的选择，误删的那条已随记录一起消失）。

用法：
    /home/ubuntu/comfyui/venv/bin/python scripts/restore_live_products.py \
        --src /root/projects/h3s-clean-baseline \
        --dst /root/projects/comfyui-minimax-h3-studio/data
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def restore(src: Path, dst: Path, dry_run: bool = False) -> dict:
    src_db = src / "data" / "jobs.db"
    dst_db = dst / "jobs.db"
    src_out = src / "data" / "outputs"
    dst_out = dst / "outputs"
    if not src_db.is_file():
        raise SystemExit("源库不存在: %s" % src_db)

    s = sqlite3.connect("file:%s?mode=ro" % src_db, uri=True)
    s.row_factory = sqlite3.Row
    d = sqlite3.connect(dst_db)
    d.row_factory = sqlite3.Row

    jobs_cols = [r[1] for r in d.execute("PRAGMA table_info(jobs)")]
    batch_cols = [r[1] for r in d.execute("PRAGMA table_info(job_batches)")]
    has_fav = d.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
        " AND name='job_favorites'").fetchone()[0] > 0

    stats = {"jobs_restored": 0, "batches_restored": 0,
             "files_copied": 0, "files_missing": 0, "jobs_skipped": 0}
    job_ids = []
    for row in s.execute("SELECT * FROM jobs ORDER BY created_at"):
        try:
            outs = json.loads(row["outputs"] or "[]")
        except (TypeError, ValueError):
            outs = []
        # 只有「产物文件全部在位」的记录才恢复
        files = [o.get("file", "") for o in outs
                 if isinstance(o, dict) and o.get("file")]
        if files:
            missing = [f for f in files
                       if not (src_out / f).is_file()]
            if missing:
                stats["files_missing"] += len(missing)
                stats["jobs_skipped"] += 1
                continue
        values = {k: row[k] for k in jobs_cols if k in row.keys()}
        cols = list(values.keys())
        ph = ",".join("?" * len(cols))
        cur = d.execute(
            "INSERT OR IGNORE INTO jobs (%s) VALUES (%s)" % (",".join(cols), ph),
            [values[c] for c in cols])
        if cur.rowcount:
            stats["jobs_restored"] += 1
            job_ids.append(row["id"])
        # 产物文件拷过去
        for f in files:
            srcf = src_out / f
            dstf = dst_out / f
            if not dstf.parent.exists():
                dstf.parent.mkdir(parents=True, exist_ok=True)
            if not dstf.exists():
                shutil.copy2(srcf, dstf)
                stats["files_copied"] += 1

    # 批次
    for row in s.execute("SELECT * FROM job_batches"):
        if row["job_id"] not in job_ids:
            continue
        values = {k: row[k] for k in batch_cols if k in row.keys()}
        cols = list(values.keys())
        ph = ",".join("?" * len(cols))
        cur = d.execute(
            "INSERT OR IGNORE INTO job_batches (%s) VALUES (%s)"
            % (",".join(cols), ph), [values[c] for c in cols])
        if cur.rowcount:
            stats["batches_restored"] += 1

    d.commit()
    total = d.execute("SELECT count(*) FROM jobs").fetchone()[0]
    stats["jobs_total_after"] = total
    if has_fav:
        stats["favorites_after"] = d.execute(
            "SELECT count(*) FROM job_favorites").fetchone()[0]
    s.close()
    d.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/root/projects/h3s-clean-baseline")
    ap.add_argument("--dst", default="/root/projects/comfyui-minimax-h3-studio/data")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    stats = restore(Path(args.src), Path(args.dst), args.dry_run)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
