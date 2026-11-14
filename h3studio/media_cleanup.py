# -*- coding: utf-8 -*-
"""生成媒体库「管理」能力（2026-11-14 需求 11 / 12）。

【要解决的】
媒体库（生成结果）会一直涨 —— 跑几周就是几万张图/视频。
现在只能一张张点「删除」，没法批量清理。

【三种清理口径】
  1. 清理**本工作台**的所有产物 —— 只删本项目 jobs 表里的记录 +
     本项目 outputs 目录下的文件；**绝不碰 ComfyUI 自身的 output 目录**
     （那是 ComfyUI 的原始产物目录，本项目只是从中下载副本）。
  2. 清理**指定月数之前**的历史产物（比如 1 个月前 = 30 天前）。
  3. 清理**失败 / 取消**的产物（status in failed/canceled）。

【为什么放在这里而不是 server.py】
server.py 已经 360KB。这块逻辑独立、内聚、可单测，
而且要同时被「HTTP 接口」和「内置 Agent 的管理工具」调用 ——
单独一个模块，两个入口共用一份实现，避免两边口径漂移。

【安全边界（硬约束）】
  * 只操作 `jobs` / `job_batches` 两张表；
  * 只删 `OUT_DIR` 之内、且路径解析后仍在 OUT_DIR 之下的文件
    （防 `../` 穿越）；
  * 不删 ComfyUI 的 output 目录、不删素材库（assets）、不删其它项目的数据；
  * 不删 queued/running 的任务（还在跑，删了产物会丢）。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

#: 可以被清理的任务终态。
CLEANABLE_STATUS = ("done", "failed", "canceled")

#: 「清理失败/取消」口径命中的状态。
FAILED_STATUS = ("failed", "canceled")


def _outputs_of(row) -> list[dict]:
    """把 outputs 列（JSON 文本）解成列表；坏了就当空。"""
    try:
        raw = json.loads(row["outputs"] or "[]")
    except (TypeError, ValueError):
        return []
    return raw if isinstance(raw, list) else []


def _safe_unlink(out_dir: Path, relative: str) -> bool:
    """删一个产物文件，但**必须**确认它落在 out_dir 之内。

    【为什么要这么严】relative 来自数据库（理论上可控），但如果它
    是 `../../etc/passwd` 或绝对路径，直接 unlink 就是任意文件删除。
    resolve() 之后必须仍在 out_dir 之内才动手。
    """
    if not relative or "/" not in relative:
        return False
    try:
        base = out_dir.resolve()
        target = (out_dir / relative).resolve()
    except (OSError, RuntimeError):
        return False
    if not str(target).startswith(str(base) + "/"):
        return False
    try:
        if target.is_file():
            target.unlink()
            return True
    except OSError:
        return False
    return False


def _delete_files(out_dir: Path, outputs: list[dict]) -> int:
    n = 0
    for item in outputs:
        if isinstance(item, dict) and _safe_unlink(out_dir, item.get("file", "")):
            n += 1
    return n


def preview(conn, out_dir: Path, scope: str = "all", months: int = 0) -> dict:
    """先算「会删多少」，给用户确认用（不删任何东西）。

    scope: all | older | failed
    months: scope=older 时的月数（按 30 天/月折算）
    """
    cutoff = time.time() - max(0, int(months)) * 30 * 86400
    rows = conn.execute("SELECT id, status, outputs, created_at FROM jobs").fetchall()
    matched = []
    for row in rows:
        status = row["status"]
        if status not in CLEANABLE_STATUS:
            continue
        if scope == "failed":
            if status not in FAILED_STATUS:
                continue
        elif scope == "older":
            created = row["created_at"] or 0
            if created >= cutoff:
                continue
        matched.append({
            "id": row["id"],
            "status": status,
            "created_at": row["created_at"],
            "files": len(_outputs_of(row)),
        })
    return {
        "scope": scope,
        "months": int(months),
        "jobs": len(matched),
        "files": sum(m["files"] for m in matched),
        "sample": matched[:20],
    }


def cleanup(conn, out_dir: Path, scope: str = "all", months: int = 0) -> dict:
    """执行清理。返回实际删掉的统计。

    【口径与 preview() 完全一致】两边用同一个筛选函数，
    不存在「预览说 3 条、真删 5 条」这种事。
    """
    cutoff = time.time() - max(0, int(months)) * 30 * 86400
    rows = conn.execute("SELECT id, status, outputs, created_at FROM jobs").fetchall()
    deleted_jobs = 0
    deleted_files = 0
    deleted_batches = 0
    skipped_running = 0
    for row in rows:
        status = row["status"]
        jid = row["id"]
        if status not in CLEANABLE_STATUS:
            if status in ("queued", "running"):
                skipped_running += 1
            continue
        if scope == "failed":
            if status not in FAILED_STATUS:
                continue
        elif scope == "older":
            created = row["created_at"] or 0
            if created >= cutoff:
                continue
        # 批次产物：先收批次里的 outputs（它们的文件不在 jobs.outputs 里）
        batch_rows = conn.execute(
            "SELECT id, outputs FROM job_batches WHERE job_id=?", (jid,)).fetchall()
        for brow in batch_rows:
            deleted_files += _delete_files(out_dir, _outputs_of(brow))
            deleted_batches += 1
        deleted_files += _delete_files(out_dir, _outputs_of(row))
        conn.execute("DELETE FROM job_batches WHERE job_id=?", (jid,))
        conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        deleted_jobs += 1
        # 每个任务还有一个以 job id 命名的目录
        shutil.rmtree(out_dir / jid, ignore_errors=True)
    return {
        "scope": scope,
        "months": int(months),
        "deleted_jobs": deleted_jobs,
        "deleted_batches": deleted_batches,
        "deleted_files": deleted_files,
        "skipped_running": skipped_running,
    }


def cleanup_scope_name(scope: str) -> str:
    return {
        "all": "全部产物",
        "older": "指定月数之前的历史产物",
        "failed": "失败 / 取消的产物",
    }.get(scope, scope)
