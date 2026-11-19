#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
素材库 / 生成库清理 —— **2026-11-19 测试阶段收尾**。

【清理范围（只有这两类）】
  1. 生成库：任务记录（jobs / job_batches / prompt_history）+ 产物文件
  2. 素材库：素材记录（assets 及其关联表）+ 素材文件

【绝对不碰】
  * 模型权重 / LoRA / 工作流 JSON / 配置 / 代码 / 测试资产
  * evidence / docs / audits
  * **保护名单**里的素材（判定为「用户本人产出」，见 PROTECT_ASSET_IDS）

【安全措施】
  1. 清理前把「清单 + 文件列表 + 数据库快照」备份到
     /root/projects/h3studio-live/evidence-20261119-cleanup/
  2. 逐条比对删除范围 —— 只删「素材库」与「生成库」两个根下的东西
  3. 删完自检：列表接口返回空、目录无残留、页面不再显示历史

【用法】
    python3 scripts/cleanup_assets_jobs.py --backup-only    # 只备份
    python3 scripts/cleanup_assets_jobs.py --apply           # 执行清理
    python3 scripts/cleanup_assets_jobs.py --verify          # 只自检
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 备份根目录（**独立目录**，不在被清理的目录树里）
BACKUP_ROOT = Path("/root/projects/h3studio-live/evidence-20261119-cleanup")

#: 生成库（任务库 + 产物）—— 线上 8190 的 config.json 里 data_dir 指向
#: 主仓的 data/，**不是** live worktree 的 data/。
JOBS_DATA_DIR = Path("/root/projects/comfyui-minimax-h3-studio/data")
JOBS_DB = JOBS_DATA_DIR / "jobs.db"
JOBS_OUTPUTS = JOBS_DATA_DIR / "outputs"
JOBS_UPLOADS = JOBS_DATA_DIR / "uploads"

#: 素材库（外挂盘）
ASSET_ROOT = Path("/mnt/usb2t/asset_library")
ASSET_DB = ASSET_ROOT / "asset_library.db"

#: 素材库里**判定为用户本人产出**的条目 -> **不删**，单独报告。
#:
#: 判定依据（2026-11-19 逐条看图 + 看来源）：
#:   * source=upload（用户从网页上传，不是程序生成的）
#:   * creator=web
#:   * 视觉上是**角色设定图 / 四视图 / 定妆照**（有创作意图的素材），
#:     不是程序产的样例图、不是测试夹具。
PROTECT_ASSET_IDS = (
    "asset_c48be15ecac2",   # 雷神三视图 · 定妆照（角色设定图）
    "asset_8540e52c0732",   # 林黛玉四视图（角色四视图设定图）
)

#: 生成库库里要清空的表（**只有这些**）。
#: model_providers / model_provider_prefs 是**配置**，不动。
JOB_TABLES = ("job_batches", "prompt_history", "jobs")

#: 素材库里要清空的表（**只有这些**）。
ASSET_TABLES = (
    "player_entries",
    "player_playlists",
    "asset_analysis_jobs",
    "asset_analyses",
    "asset_refs",
    "asset_tags",
    "asset_versions",
    "assets",
)


def _sqlite_backup(src: Path, dst: Path) -> None:
    """用 SQLite 自己的 backup API 做**一致性**快照（不停服也能拿到）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    with dst_conn:
        src_conn.backup(dst_conn)
    src_conn.close()
    dst_conn.close()


def _table_counts(conn: sqlite3.Connection, tables) -> dict:
    out = {}
    for table in tables:
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.Error:
            out[table] = -1
    return out


def _file_list(root: Path) -> list[dict]:
    out = []
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            out.append({
                "path": str(path),
                "rel": str(path.relative_to(root)),
                "bytes": size,
                "mtime": path.stat().st_mtime,
            })
    return out


def backup() -> dict:
    """把清单 + 文件列表 + 数据库快照备份到 BACKUP_ROOT。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = BACKUP_ROOT / f"backup-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    report: dict = {"backup_root": str(root), "stamp": stamp}

    # ---- 1. 数据库快照 ----
    db_dir = root / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    if JOBS_DB.exists():
        _sqlite_backup(JOBS_DB, db_dir / "jobs.db")
        report["jobs_db_backup"] = str(db_dir / "jobs.db")
    if ASSET_DB.exists():
        _sqlite_backup(ASSET_DB, db_dir / "asset_library.db")
        report["asset_db_backup"] = str(db_dir / "asset_library.db")

    # ---- 2. 清单（数据库行）----
    manifest_dir = root / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    for name, db_path, tables in (("jobs", JOBS_DB, JOB_TABLES),
                                  ("assets", ASSET_DB, ASSET_TABLES)):
        if not db_path.exists():
            continue
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        counts = _table_counts(conn, tables)
        report[f"{name}_table_counts_before"] = counts
        for table in tables:
            try:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            except sqlite3.Error:
                continue
            (manifest_dir / f"{name}__{table}.json").write_text(
                json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
        conn.close()

    # ---- 3. 文件列表 ----
    lists_dir = root / "filelists"
    lists_dir.mkdir(parents=True, exist_ok=True)
    for label, target in (("jobs_outputs", JOBS_OUTPUTS),
                          ("jobs_uploads", JOBS_UPLOADS),
                          ("asset_originals", ASSET_ROOT / "originals"),
                          ("asset_derived", ASSET_ROOT / "derived"),
                          ("asset_previews", ASSET_ROOT / "previews"),
                          ("asset_trash", ASSET_ROOT / "trash")):
        entries = _file_list(target)
        (lists_dir / f"{label}.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8")
        report[f"filelist_{label}"] = {
            "root": str(target),
            "count": len(entries),
            "bytes": sum(max(0, e["bytes"]) for e in entries),
            "list_file": str(lists_dir / f"{label}.json"),
        }

    (root / "cleanup_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _protected_files() -> set[str]:
    """保护名单对应的**磁盘文件**（绝对路径集合）。"""
    out: set[str] = set()
    if not ASSET_DB.exists():
        return out
    conn = sqlite3.connect(f"file:{ASSET_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    for asset_id in PROTECT_ASSET_IDS:
        for row in conn.execute(
                "SELECT storage_path FROM asset_versions WHERE asset_id=?",
                (asset_id,)):
            out.add(str((ASSET_ROOT / row["storage_path"]).resolve()))
    conn.close()
    return out


def apply() -> dict:
    """执行清理。"""
    report: dict = {}
    protected = _protected_files()
    report["protected_asset_ids"] = list(PROTECT_ASSET_IDS)
    report["protected_files"] = sorted(protected)

    # ---- 1. 生成库：数据库表 ----
    if JOBS_DB.exists():
        conn = sqlite3.connect(str(JOBS_DB))
        conn.row_factory = sqlite3.Row
        report["jobs_table_counts_before"] = _table_counts(conn, JOB_TABLES)
        for table in JOB_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     f"({','.join('?' * len(JOB_TABLES))})", JOB_TABLES)
        conn.commit()
        report["jobs_table_counts_after"] = _table_counts(conn, JOB_TABLES)
        conn.close()

    # ---- 2. 生成库：产物文件 ----
    for label, target in (("jobs_outputs", JOBS_OUTPUTS),
                          ("jobs_uploads", JOBS_UPLOADS)):
        removed = 0
        freed = 0
        if target.exists():
            for path in sorted(target.rglob("*"), reverse=True):
                if path.is_file():
                    try:
                        freed += path.stat().st_size
                    except OSError:
                        pass
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    try:
                        path.rmdir()
                    except OSError:
                        pass
        report[f"{label}_removed_files"] = removed
        report[f"{label}_freed_bytes"] = freed

    # ---- 3. 素材库：数据库表 ----
    if ASSET_DB.exists():
        conn = sqlite3.connect(str(ASSET_DB))
        conn.row_factory = sqlite3.Row
        report["asset_table_counts_before"] = _table_counts(conn, ASSET_TABLES)
        # 保护名单的条目**先摘出来**（清表后重新插回）
        keep: dict[str, list[dict]] = {}
        for table in ASSET_TABLES:
            try:
                keep[table] = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            except sqlite3.Error:
                keep[table] = []
        for table in ASSET_TABLES:
            conn.execute(f"DELETE FROM {table}")
        # 只把保护名单对应的行放回去。
        #
        # 【主键列的坑】`assets` 表的主键叫 `id`（值就是 asset_id），
        # 其余表叫 `asset_id` —— 所以这里两个名字都要试，
        # 否则保护名单的 `assets` 行会被漏掉（孤儿行）。
        for table in ASSET_TABLES:
            for row in keep.get(table) or []:
                asset_id = row.get("asset_id") or row.get("id")
                if asset_id not in PROTECT_ASSET_IDS:
                    continue
                cols = list(row.keys())
                placeholders = ",".join("?" * len(cols))
                conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                    f"VALUES ({placeholders})",
                    [row[c] for c in cols])
        conn.commit()
        report["asset_table_counts_after"] = _table_counts(conn, ASSET_TABLES)
        conn.close()

    # ---- 4. 素材库：磁盘文件（保护名单除外）----
    removed = 0
    freed = 0
    for sub in ("originals", "derived", "previews", "trash"):
        target = ASSET_ROOT / sub
        if not target.exists():
            continue
        for path in sorted(target.rglob("*"), reverse=True):
            if path.is_file():
                resolved = str(path.resolve())
                if resolved in protected:
                    continue
                try:
                    freed += path.stat().st_size
                except OSError:
                    pass
                path.unlink()
                removed += 1
            elif path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass
    report["asset_removed_files"] = removed
    report["asset_freed_bytes"] = freed

    # ---- 5. 保护名单的预览图：清文件时一并删了 -> **重新生成** ----
    #
    # 【为什么必须补】素材卡片的缩略图走 /api/assets/{id}/preview，
    # 读的是 asset_versions.preview_path 指向的文件。清理时 previews/
    # 整个目录被清空，而保护名单的行还在（preview_path 还指着那个路径）
    # -> 接口返回 404。素材本身（originals/）是完好的，只是缩略图没了。
    # 这里用**生产代码路径**（media_probe.make_image_preview）重建。
    report["protected_previews_regenerated"] = _rebuild_previews()
    return report


def _rebuild_previews() -> list[dict]:
    """给保护名单的素材重建缩略图（用生产代码路径）。"""
    out: list[dict] = []
    if not ASSET_DB.exists():
        return out
    try:
        sys.path.insert(0, str(REPO))
        from h3studio import assets as A
        from h3studio import media_probe
    except Exception as exc:
        return [{"error": f"导入生产模块失败：{exc}"}]
    conn = sqlite3.connect(f"file:{ASSET_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT asset_id, storage_path FROM asset_versions"))
    conn.close()
    write = sqlite3.connect(str(ASSET_DB))
    for row in rows:
        asset_id = row["asset_id"]
        if asset_id not in PROTECT_ASSET_IDS:
            continue
        source = ASSET_ROOT / row["storage_path"]
        if not source.is_file():
            continue
        rel = A.preview_relpath(asset_id, ".jpg")
        dest = A.resolve_inside(ASSET_ROOT, rel)
        written = False
        if dest is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = bool(media_probe.make_image_preview(source, dest))
        if written:
            write.execute(
                "UPDATE asset_versions SET preview_path=? WHERE asset_id=?",
                (rel, asset_id))
        out.append({"asset_id": asset_id, "preview": rel, "written": written})
    write.commit()
    write.close()
    return out


def verify() -> dict:
    """清理后自检。"""
    out: dict = {}
    # ---- 数据库 ----
    if JOBS_DB.exists():
        conn = sqlite3.connect(f"file:{JOBS_DB}?mode=ro", uri=True)
        out["jobs_table_counts_after"] = _table_counts(conn, JOB_TABLES)
        conn.close()
    if ASSET_DB.exists():
        conn = sqlite3.connect(f"file:{ASSET_DB}?mode=ro", uri=True)
        out["asset_table_counts_after"] = _table_counts(conn, ASSET_TABLES)
        conn.close()
    # ---- 磁盘残留 ----
    for label, target in (("jobs_outputs", JOBS_OUTPUTS),
                          ("jobs_uploads", JOBS_UPLOADS),
                          ("asset_originals", ASSET_ROOT / "originals"),
                          ("asset_derived", ASSET_ROOT / "derived"),
                          ("asset_previews", ASSET_ROOT / "previews"),
                          ("asset_trash", ASSET_ROOT / "trash")):
        out[f"residual_{label}"] = len(_file_list(target))
    # ---- 线上接口 ----
    for label, url in (("jobs", "http://127.0.0.1:8190/api/jobs?limit=1"),
                       ("assets", "http://127.0.0.1:8190/api/assets?limit=1"),
                       ("assets_stats",
                        "http://127.0.0.1:8190/api/assets/stats")):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                body = json.loads(resp.read().decode())
            out[f"api_{label}"] = body.get("total", body.get("total_count"))
        except Exception as exc:
            out[f"api_{label}_error"] = f"{type(exc).__name__}: {exc}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup-only", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    if not (args.backup_only or args.apply or args.verify):
        ap.error("至少给一个动作：--backup-only / --apply / --verify")

    if args.backup_only:
        report = backup()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.apply:
        report = apply()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
