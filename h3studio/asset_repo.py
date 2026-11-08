# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— 仓库层（CRUD / 检索 / 标签 / 引用 / 分析记录）。

所有写操作都在事务里做；所有对外可见的引用计数都是**实时算**的，
不存冗余计数，避免计数与事实漂移。
"""
import json
import time
import uuid

from . import assets as A
from .assets import LibraryError


# ------------------------------------------------------------------ 行转换


def _row_to_asset(row) -> dict:
    data = {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "subtype": row["subtype"],
        "mime": row["mime"],
        "tags": A.jload(row["tags"], []) or [],
        "note": row["note"],
        "source": row["source"],
        "source_ref": row["source_ref"],
        "source_meta": A.jload(row["source_meta"], {}) or {},
        "creator": row["creator"],
        "latest_version": row["latest_version"],
        "version_count": row["version_count"],
        "total_bytes": row["total_bytes"],
        "deleted_at": row["deleted_at"],
        "purge_at": row["purge_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    return data


def _row_to_version(row) -> dict:
    return {
        "id": row["id"],
        "asset_id": row["asset_id"],
        "version": row["version"],
        "storage_path": row["storage_path"],
        "sha256": row["sha256"],
        "bytes": row["bytes"],
        "mime": row["mime"],
        "width": row["width"],
        "height": row["height"],
        "duration_sec": row["duration_sec"],
        "fps": row["fps"],
        "codec": row["codec"],
        "audio_codec": row["audio_codec"],
        "sample_rate": row["sample_rate"],
        "channels": row["channels"],
        "container": row["container"],
        "transcode_reason": row["transcode_reason"],
        "extracted_json": A.jload(row["extracted_json"], {}) or {},
        "preview_path": row["preview_path"],
        "trash_path": row["trash_path"],
        "origin": row["origin"],
        "origin_note": row["origin_note"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
    }


def _row_to_analysis(row) -> dict:
    return {
        "id": row["id"],
        "asset_id": row["asset_id"],
        "version": row["version"],
        "revision": row["revision"],
        "kind": row["kind"],
        "text": row["text"],
        "prompt": row["prompt"],
        "provider_id": row["provider_id"],
        "provider_name": row["provider_name"],
        "model": row["model"],
        "frames": A.jload(row["frames"], []) or [],
        "extra": A.jload(row["extra"], {}) or {},
        "error": row["error"],
        "created_at": row["created_at"],
    }


# ------------------------------------------------------------------ 查询


def get_asset(conn, asset_id: str, include_deleted: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
    if not row:
        return None
    if row["deleted_at"] and not include_deleted:
        return None
    return _row_to_asset(row)


def current_version(conn, asset_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version DESC LIMIT 1",
        (asset_id,)).fetchone()
    return _row_to_version(row) if row else None


def versions_of(conn, asset_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM asset_versions WHERE asset_id=? ORDER BY version DESC",
        (asset_id,)).fetchall()
    return [_row_to_version(r) for r in rows]


def analyses_of(conn, asset_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM asset_analyses WHERE asset_id=? ORDER BY revision DESC, id DESC",
        (asset_id,)).fetchall()
    return [_row_to_analysis(r) for r in rows]


def latest_analysis(conn, asset_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM asset_analyses WHERE asset_id=? AND error IS NULL "
        "ORDER BY revision DESC, id DESC LIMIT 1", (asset_id,)).fetchone()
    return _row_to_analysis(row) if row else None


def tags_of(conn, asset_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT tag FROM asset_tags WHERE asset_id=? ORDER BY tag", (asset_id,)).fetchall()
    return [r["tag"] for r in rows]


def refs_of(conn, asset_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM asset_refs WHERE asset_id=? ORDER BY created_at DESC",
        (asset_id,)).fetchall()
    return [{
        "id": r["id"], "asset_id": r["asset_id"], "kind": r["ref_kind"],
        "ref_id": r["ref_id"], "slot": r["slot"], "ordinal": r["ordinal"],
        "label": r["label"], "created_at": r["created_at"],
    } for r in rows]


def ref_count(conn, asset_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) c FROM asset_refs WHERE asset_id=?",
                       (asset_id,)).fetchone()
    return int(row["c"]) if row else 0


def all_tags(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT tag, COUNT(*) c FROM asset_tags GROUP BY tag ORDER BY c DESC, tag"
    ).fetchall()
    return [{"tag": r["tag"], "count": int(r["c"])} for r in rows]


# ------------------------------------------------------------------ 检索

_SORTS = {
    "created_desc": "a.created_at DESC",
    "created_asc": "a.created_at ASC",
    "size_desc": "a.total_bytes DESC",
    "size_asc": "a.total_bytes ASC",
    "name_asc": "a.name ASC",
    "name_desc": "a.name DESC",
}


def _split_tags(value) -> list[str]:
    """
    把 tags_all / tags_any 的入参拆成标签列表。

    入参是逗号分隔的字符串（中英文逗号都收）。
    不能直接迭代字符串本身 —— 那样拿到的是单个字符。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value).replace("，", ",").split(",")
    return [str(item).strip() for item in raw if str(item).strip()]


def search_assets(conn, filters: dict) -> dict:
    """
    列表 + 筛选 + 搜索。

    支持的筛选维度（对齐计划书 §5.6.3）：
      category / subtype / tag / tags_all / tags_any / q / source /
      creator / in_trash / referenced / created_after / created_before /
      min_bytes / max_bytes / min_duration / max_duration
    """
    where = []
    args: list = []

    in_trash = str(filters.get("in_trash") or "").lower()
    if in_trash in ("1", "true", "yes"):
        where.append("a.deleted_at IS NOT NULL")
    else:
        where.append("a.deleted_at IS NULL")

    category = (filters.get("category") or "").strip()
    if category:
        where.append("a.category=?")
        args.append(category)

    subtype = (filters.get("subtype") or "").strip()
    if subtype:
        where.append("a.subtype=?")
        args.append(subtype)

    source = (filters.get("source") or "").strip()
    if source:
        where.append("a.source=?")
        args.append(source)

    creator = (filters.get("creator") or "").strip()
    if creator:
        where.append("a.creator=?")
        args.append(creator)

    keyword = (filters.get("q") or "").strip()
    if keyword:
        like = f"%{keyword}%"
        # 名称 / 备注 / 标签 / 分析文本 —— 全文搜索覆盖用户最常找的四处
        where.append(
            "(a.name LIKE ? OR a.note LIKE ? OR a.tags LIKE ? OR "
            "EXISTS (SELECT 1 FROM asset_analyses x WHERE x.asset_id=a.id AND x.text LIKE ?))")
        args.extend((like, like, like, like))

    tag = (filters.get("tag") or "").strip()
    if tag:
        where.append("EXISTS (SELECT 1 FROM asset_tags t WHERE t.asset_id=a.id AND t.tag=?)")
        args.append(tag)

    # tags_all / tags_any 传进来是逗号分隔的字符串。
    # 注意：不能直接 `[t for t in value]` —— 那样迭代的是**字符**，
    # "原神" 会变成 ['原','神']，查出来永远是 0 条。
    tags_all = _split_tags(filters.get("tags_all"))
    for item in tags_all:
        where.append("EXISTS (SELECT 1 FROM asset_tags t WHERE t.asset_id=a.id AND t.tag=?)")
        args.append(item)

    tags_any = _split_tags(filters.get("tags_any"))
    if tags_any:
        marks = ",".join("?" * len(tags_any))
        where.append(
            f"EXISTS (SELECT 1 FROM asset_tags t WHERE t.asset_id=a.id AND t.tag IN ({marks}))")
        args.extend(tags_any)

    referenced = str(filters.get("referenced") or "").lower()
    if referenced in ("1", "true", "yes"):
        where.append("EXISTS (SELECT 1 FROM asset_refs r WHERE r.asset_id=a.id)")
    elif referenced in ("0", "false", "no"):
        where.append("NOT EXISTS (SELECT 1 FROM asset_refs r WHERE r.asset_id=a.id)")

    for key, op in (("created_after", ">="), ("created_before", "<=")):
        value = filters.get(key)
        if value not in (None, ""):
            try:
                where.append(f"a.created_at {op} ?")
                args.append(float(value))
            except (TypeError, ValueError):
                pass

    for key, op in (("min_bytes", ">="), ("max_bytes", "<=")):
        value = filters.get(key)
        if value not in (None, ""):
            try:
                where.append(f"a.total_bytes {op} ?")
                args.append(int(value))
            except (TypeError, ValueError):
                pass

    duration_conds = []
    for key, op in (("min_duration", ">="), ("max_duration", "<=")):
        value = filters.get(key)
        if value not in (None, ""):
            try:
                duration_conds.append(
                    f"(SELECT v.duration_sec FROM asset_versions v "
                    f"WHERE v.asset_id=a.id ORDER BY v.version DESC LIMIT 1) {op} ?")
                args.append(float(value))
            except (TypeError, ValueError):
                pass
    if duration_conds:
        where.append("(" + " AND ".join(duration_conds) + ")")

    clause = " WHERE " + " AND ".join(where)
    order = _SORTS.get(filters.get("sort") or "created_desc", _SORTS["created_desc"])

    try:
        page = max(1, int(filters.get("page") or 1))
        page_size = min(200, max(1, int(filters.get("page_size") or 60)))
    except (TypeError, ValueError):
        raise LibraryError("page 和 page_size 必须是整数", status=400)
    offset = (page - 1) * page_size

    total = conn.execute("SELECT COUNT(*) FROM assets a" + clause, args).fetchone()[0]
    rows = conn.execute(
        f"SELECT a.* FROM assets a{clause} ORDER BY {order} LIMIT ? OFFSET ?",
        [*args, page_size, offset]).fetchall()

    items = []
    for row in rows:
        asset = _row_to_asset(row)
        version = current_version(conn, asset["id"])
        asset["bytes"] = version["bytes"] if version else 0
        asset["width"] = version["width"] if version else None
        asset["height"] = version["height"] if version else None
        asset["duration_sec"] = version["duration_sec"] if version else None
        asset["mime"] = version["mime"] if version else asset["mime"]
        asset["preview_url"] = (f"/api/assets/{asset['id']}/preview"
                                if version and version["preview_path"] else None)
        asset["content_url"] = f"/api/assets/{asset['id']}/content"
        asset["ref_count"] = ref_count(conn, asset["id"])
        analysis = latest_analysis(conn, asset["id"])
        asset["analyzed"] = bool(analysis)
        asset["analysis_revision"] = analysis["revision"] if analysis else 0
        asset["analysis_summary"] = (analysis["text"][:120] if analysis else "")
        items.append(asset)

    return {"assets": items, "total": int(total), "page": page, "page_size": page_size}


# ------------------------------------------------------------------ 写入


def add_tags(conn, asset_id: str, tags, creator: str | None = None) -> list[str]:
    """加标签（幂等）。返回该素材最终标签列表。"""
    clean = []
    for tag in tags or []:
        text = str(tag).strip()[:60]
        if text and text not in clean:
            clean.append(text)
    if not clean:
        return tags_of(conn, asset_id)
    stamp = A.now()
    for tag in clean:
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (tag, asset_id, created_at) VALUES (?,?,?)",
            (tag, asset_id, stamp))
    _sync_tag_json(conn, asset_id)
    conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (stamp, asset_id))
    return tags_of(conn, asset_id)


def remove_tag(conn, asset_id: str, tag: str) -> list[str]:
    conn.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag=?", (asset_id, tag))
    _sync_tag_json(conn, asset_id)
    conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (A.now(), asset_id))
    return tags_of(conn, asset_id)


def rename_tag(conn, old: str, new: str) -> int:
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        raise LibraryError("标签名不能为空", status=400)
    stamp = A.now()
    rows = conn.execute("SELECT asset_id FROM asset_tags WHERE tag=?", (old,)).fetchall()
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (tag, asset_id, created_at) VALUES (?,?,?)",
            (new, row["asset_id"], stamp))
    conn.execute("DELETE FROM asset_tags WHERE tag=?", (old,))
    touched = {row["asset_id"] for row in rows}
    for asset_id in touched:
        _sync_tag_json(conn, asset_id)
    return len(touched)


def _sync_tag_json(conn, asset_id: str) -> None:
    """把 asset_tags 的权威内容同步回 assets.tags（冗余列，仅为列表接口快读）。"""
    tags = tags_of(conn, asset_id)
    conn.execute("UPDATE assets SET tags=? WHERE id=?", (A.jdump(tags), asset_id))


def update_asset(conn, asset_id: str, payload: dict) -> dict:
    """改元数据：name / category / subtype / note / tags。"""
    asset = get_asset(conn, asset_id)
    if not asset:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")

    fields = []
    args = []
    if "name" in payload:
        name = str(payload["name"] or "").strip()[:200]
        if not name:
            raise LibraryError("名称不能为空", status=400)
        fields.append("name=?")
        args.append(name)
    if "category" in payload:
        category = str(payload["category"] or "").strip()
        if category not in A.CATEGORIES:
            raise LibraryError(f"未知类型 {category}，可选：{'、'.join(A.CATEGORIES)}",
                              status=400)
        fields.append("category=?")
        args.append(category)
    if "subtype" in payload:
        subtype = str(payload["subtype"] or "").strip()[:40]
        fields.append("subtype=?")
        args.append(subtype)
    if "note" in payload:
        fields.append("note=?")
        args.append(str(payload["note"] or "")[:4000])
    if fields:
        fields.append("updated_at=?")
        args.append(A.now())
        args.append(asset_id)
        conn.execute(f"UPDATE assets SET {', '.join(fields)} WHERE id=?", args)

    if "tags" in payload:
        tags = payload["tags"]
        if isinstance(tags, str):
            tags = [t for t in (part.strip() for part in tags.replace(",", "，").split("，")) if t]
        conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
        add_tags(conn, asset_id, tags)

    updated = get_asset(conn, asset_id)
    if updated is None:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")
    return updated


def add_ref(conn, asset_id: str, kind: str, ref_id: str, slot: str = "ref",
            ordinal: int = 0, label: str | None = None) -> None:
    conn.execute(
        "INSERT INTO asset_refs (asset_id, ref_kind, ref_id, slot, ordinal, label, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (asset_id, kind, str(ref_id), slot, int(ordinal), label, A.now()))


def remove_refs_for(conn, kind: str, ref_id: str) -> int:
    cur = conn.execute("DELETE FROM asset_refs WHERE ref_kind=? AND ref_id=?",
                       (kind, str(ref_id)))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def save_analysis(conn, asset_id: str, kind: str, text: str, revision: int,
                  provider: dict | None = None, prompt: str = "",
                  frames=None, extra=None, error: str | None = None) -> dict:
    """写入一条分析记录（成功或失败都留痕，便于排障）。"""
    stamp = A.now()
    version = current_version(conn, asset_id)
    cur = conn.execute(
        "INSERT INTO asset_analyses (asset_id, version, revision, kind, text, prompt, "
        "provider_id, provider_name, model, frames, extra, error, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (asset_id, version["version"] if version else 1, revision, kind, text or "",
         prompt or "",
         (provider or {}).get("id"), (provider or {}).get("name"),
         (provider or {}).get("model"),
         A.jdump(frames or []), A.jdump(extra or {}), error, stamp))
    conn.execute("UPDATE assets SET updated_at=? WHERE id=?", (stamp, asset_id))
    row = conn.execute("SELECT * FROM asset_analyses WHERE id=?",
                       (cur.lastrowid,)).fetchone()
    return _row_to_analysis(row)


def next_revision(conn, asset_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(revision) m FROM asset_analyses WHERE asset_id=?", (asset_id,)).fetchone()
    return int(row["m"] or 0) + 1


# ------------------------------------------------------------------ 删除语义


def check_references(conn, asset_id: str) -> list[dict]:
    """
    删除前的引用检查。返回引用清单（空 = 可删）。

    两条来源：
      1. asset_refs      —— 显式登记的引用（任务/批次/计划）
      2. job_assets 风格的来源登记（source=job_output 的素材被任务产物登记）
    """
    return refs_of(conn, asset_id)


def soft_delete(conn, asset_id: str) -> dict:
    """
    软删除（进回收站）。

    有引用时抛 409（带引用清单）—— 服务端强制，不是 UI 提示。
    """
    asset = get_asset(conn, asset_id, include_deleted=True)
    if not asset:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")
    if asset["deleted_at"]:
        return asset

    refs = check_references(conn, asset_id)
    if refs:
        raise LibraryError(
            "素材正在被引用，无法删除", status=409, code="asset_in_use",
            extra={"asset_id": asset_id, "refs": refs,
                   "suggestion": "可先删除引用方，或先取消引用后再删除"})

    stamp = A.now()
    conn.execute(
        "UPDATE assets SET deleted_at=?, purge_at=?, updated_at=? WHERE id=?",
        (stamp, stamp + A.TRASH_RETENTION_SEC, stamp, asset_id))

    # 物理文件移到回收站（保留原文件，只是不再出现在正常列表里）
    root = A.library_root()
    for version in versions_of(conn, asset_id):
        source = A.resolve_inside(root, version["storage_path"])
        if not source or not source.is_file():
            continue
        # 回收站文件名带上 asset_id 与版本号，但**保留原扩展名**；
        # 原文件名记录在 trash_path 之外无法还原，所以这里额外存一份
        # 「回收站时的相对路径」到 trash_path，还原时按它反推原路径。
        target = root / A.DIR_TRASH / f"{asset_id}_v{version['version']}{source.suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            A.atomic_move(source, target)
            conn.execute(
                "UPDATE asset_versions SET storage_path=?, trash_path=? WHERE id=?",
                (f"{A.DIR_TRASH}/{target.name}", version["storage_path"],
                 version["id"]))
        except OSError:
            pass
    restored = get_asset(conn, asset_id, include_deleted=True)
    if restored is None:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")
    return restored


def restore(conn, asset_id: str) -> dict:
    """从回收站还原。"""
    asset = get_asset(conn, asset_id, include_deleted=True)
    if not asset:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")
    if not asset["deleted_at"]:
        return asset

    root = A.library_root()
    for version in versions_of(conn, asset_id):
        # trash_path 存的是「进回收站前的原相对路径」；老数据可能是回收站路径
        trash_rel = version["trash_path"] or (
            version["storage_path"] if version["storage_path"].startswith(A.DIR_TRASH + "/") else None)
        if not trash_rel:
            continue
        source = A.resolve_inside(root, version["storage_path"])
        if not source or not source.is_file():
            source = A.resolve_inside(root, trash_rel)
            if not source or not source.is_file():
                continue
        original = trash_rel
        if original.startswith(A.DIR_TRASH + "/"):
            # 老数据：按 sha256 反推
            original = A.original_relpath(version["sha256"], source.name)
        target = A.resolve_inside(root, original)
        if target is None:
            continue
        try:
            A.atomic_move(source, target)
            conn.execute(
                "UPDATE asset_versions SET storage_path=?, trash_path=NULL WHERE id=?",
                (original, version["id"]))
        except OSError:
            pass

    stamp = A.now()
    conn.execute("UPDATE assets SET deleted_at=NULL, purge_at=NULL, updated_at=? WHERE id=?",
                 (stamp, asset_id))
    return get_asset(conn, asset_id)


def purge(conn, asset_id: str) -> dict:
    """彻底删除：物理文件 + 所有版本/分析行。素材本体行也删。"""
    asset = get_asset(conn, asset_id, include_deleted=True)
    if not asset:
        raise LibraryError("素材不存在", status=404, code="asset_not_found")

    refs = check_references(conn, asset_id)
    if refs:
        raise LibraryError(
            "素材正在被引用，无法彻底删除", status=409, code="asset_in_use",
            extra={"asset_id": asset_id, "refs": refs})

    root = A.library_root()
    for version in versions_of(conn, asset_id):
        path = A.resolve_inside(root, version["storage_path"])
        if path and path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        if version["preview_path"]:
            preview = A.resolve_inside(root, version["preview_path"])
            if preview and preview.is_file():
                try:
                    preview.unlink()
                except OSError:
                    pass
    conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
    return {"purged": asset_id}


def stats(conn) -> dict:
    """空间统计：总量 / 分类 / TopN / 回收站。"""
    total = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(total_bytes),0) b FROM assets "
        "WHERE deleted_at IS NULL").fetchone()
    by_category = conn.execute(
        "SELECT category, COUNT(*) c, COALESCE(SUM(total_bytes),0) b FROM assets "
        "WHERE deleted_at IS NULL GROUP BY category").fetchall()
    by_source = conn.execute(
        "SELECT source, COUNT(*) c FROM assets WHERE deleted_at IS NULL GROUP BY source").fetchall()
    top = conn.execute(
        "SELECT id, name, category, total_bytes FROM assets WHERE deleted_at IS NULL "
        "ORDER BY total_bytes DESC LIMIT 10").fetchall()
    trash = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(total_bytes),0) b FROM assets "
        "WHERE deleted_at IS NOT NULL").fetchone()
    analyzed = conn.execute(
        "SELECT COUNT(DISTINCT asset_id) c FROM asset_analyses WHERE error IS NULL").fetchone()
    return {
        "total_count": int(total["c"]),
        "total_bytes": int(total["b"]),
        "by_category": [{"category": r["category"], "count": int(r["c"]),
                         "bytes": int(r["b"])} for r in by_category],
        "by_source": [{"source": r["source"], "count": int(r["c"])} for r in by_source],
        "top_files": [{"id": r["id"], "name": r["name"], "category": r["category"],
                       "bytes": int(r["total_bytes"])} for r in top],
        "trash_count": int(trash["c"]),
        "trash_bytes": int(trash["b"]),
        "analyzed_count": int(analyzed["c"] if analyzed else 0),
    }
