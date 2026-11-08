# -*- coding: utf-8 -*-
"""
H3 Studio 资产素材库 —— HTTP 接口层。

单独成文件（而不是塞进 server.py）的原因：
  server.py 正被并发任务修改（图像/LoRA 面板），素材库的接口集中在这里，
  server.py 只需要在 make_app() 里加几行路由 + 启动时做一次自检，
  冲突面最小。

接口清单（统一前缀 /api/assets）：
  GET    /api/assets                  列表（分页 + 筛选 + 搜索）
  POST   /api/assets                  批量上传（multipart）
  GET    /api/assets/stats            空间统计
  GET    /api/assets/tags             标签清单
  POST   /api/assets/tags/rename      标签重命名
  GET    /api/assets/picker           轻量选择器数据源
  POST   /api/assets/reconcile        孤儿文件对账（只报告不删）
  GET    /api/assets/{id}             详情（版本 / 引用 / 分析历史）
  PATCH  /api/assets/{id}             改元数据（含标签）
  DELETE /api/assets/{id}             删除（→ 回收站；?purge=1 彻底删）
  POST   /api/assets/{id}/restore     从回收站还原
  GET    /api/assets/{id}/content     下载原文件
  GET    /api/assets/{id}/preview     缩略图
  GET    /api/assets/{id}/refs        引用反查
  POST   /api/assets/{id}/tags        加标签
  DELETE /api/assets/{id}/tags/{tag}  删标签
  POST   /api/assets/{id}/analyze     触发模型深度分析
  POST   /api/assets/{id}/corrections 追加交互纠错（多轮）
  POST   /api/assets/import/from-media  从媒体库（生成结果）右键入库
  GET    /api/assets/{id}/analyses    分析修订历史
"""
import hashlib
import json
import time
import uuid
from pathlib import Path

from aiohttp import web

from h3studio import analysis, asset_repo, asset_service
from h3studio import assets as A
from h3studio.assets import LibraryError

#: 允许的上传扩展名（空 = 不限制，只按 mime 分类型）
ALLOWED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif",
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg",
    ".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma",
    ".json", ".md", ".txt", ".srt", ".vtt", ".yaml", ".yml",
}


def _library_conn():
    """打开素材库数据库连接。"""
    return A.connect()


def _err(exc: LibraryError):
    payload = {"error": exc.message, "code": exc.code}
    payload.update(exc.extra or {})
    return web.json_response(payload, status=exc.status)


def _client_id(request) -> str:
    return request.headers.get("X-Client-Id") or "web"


# ------------------------------------------------------------------ 列表 / 搜索


async def api_assets_list(request):
    """GET /api/assets —— 列表（分页 + 筛选 + 搜索）"""
    query = request.query
    filters = {
        "category": query.get("category", ""),
        "subtype": query.get("subtype", ""),
        "tag": query.get("tag", ""),
        "tags_all": query.get("tags_all", ""),
        "tags_any": query.get("tags_any", ""),
        "q": query.get("q", ""),
        "source": query.get("source", ""),
        "creator": query.get("creator", ""),
        "in_trash": query.get("in_trash", ""),
        "referenced": query.get("referenced", ""),
        "sort": query.get("sort", "created_desc"),
        "page": query.get("page", "1"),
        "page_size": query.get("page_size", "60"),
    }
    for key in ("created_after", "created_before", "min_bytes", "max_bytes",
                "min_duration", "max_duration"):
        value = query.get(key)
        if value not in (None, ""):
            filters[key] = value
    try:
        with _library_conn() as conn:
            result = asset_repo.search_assets(conn, filters)
    except LibraryError as exc:
        return _err(exc)
    return web.json_response(result)


async def api_assets_stats(request):
    """GET /api/assets/stats —— 空间统计"""
    with _library_conn() as conn:
        stats = asset_repo.stats(conn)
    stats["library_root"] = str(A.library_root())
    stats["max_upload_bytes"] = A.MAX_UPLOAD_BYTES
    return web.json_response(stats)


async def api_assets_tags(request):
    """GET /api/assets/tags —— 全局标签清单"""
    with _library_conn() as conn:
        return web.json_response({"tags": asset_repo.all_tags(conn)})


async def api_assets_tag_rename(request):
    """POST /api/assets/tags/rename —— 标签重命名"""
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    old = str(payload.get("old") or payload.get("from") or "").strip()
    new = str(payload.get("new") or payload.get("to") or "").strip()
    if not old or not new:
        return web.json_response({"error": "旧标签名和新标签名都不能为空"}, status=400)
    with _library_conn() as conn:
        touched = asset_repo.rename_tag(conn, old, new)
    return web.json_response({"renamed": old, "to": new, "affected": touched})


async def api_assets_picker(request):
    """
    GET /api/assets/picker —— 轻量选择器数据源。

    给任务参数面板用：只返回 id / name / 预览 / 类型 / 时长，避免拉全量。
    """
    category = request.query.get("category", "").strip()
    limit = min(500, max(1, int(request.query.get("limit", "200") or 200)))
    where = "a.deleted_at IS NULL"
    args: list = []
    if category:
        where += " AND a.category=?"
        args.append(category)
    with _library_conn() as conn:
        # duration_sec / preview_path 都在 asset_versions 上，不在 assets 表 ——
        # 这里用子查询取当前版本的值。
        rows = conn.execute(
            f"SELECT a.id, a.name, a.category, a.subtype, "
            f"(SELECT v.duration_sec FROM asset_versions v WHERE v.asset_id=a.id "
            f" ORDER BY v.version DESC LIMIT 1) AS duration_sec, "
            f"(SELECT v.preview_path FROM asset_versions v WHERE v.asset_id=a.id "
            f" ORDER BY v.version DESC LIMIT 1) AS preview_path "
            f"FROM assets a WHERE {where} ORDER BY a.updated_at DESC LIMIT ?",
            [*args, limit]).fetchall()
    items = [{
        "id": r["id"], "name": r["name"], "category": r["category"],
        "subtype": r["subtype"], "duration_sec": r["duration_sec"],
        "preview_url": f"/api/assets/{r['id']}/preview" if r["preview_path"] else None,
    } for r in rows]
    return web.json_response({"assets": items, "total": len(items)})


async def api_assets_reconcile(request):
    """POST /api/assets/reconcile —— 孤儿文件对账（只报告不删）"""
    root = A.library_root()
    with _library_conn() as conn:
        known = {r["storage_path"] for r in conn.execute(
            "SELECT storage_path FROM asset_versions").fetchall()}
        known |= {r["preview_path"] for r in conn.execute(
            "SELECT preview_path FROM asset_versions WHERE preview_path IS NOT NULL").fetchall()}
    orphans = []
    for name in A.SUBDIRS:
        folder = root / name
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            relative = path.relative_to(root).as_posix()
            if relative not in known:
                orphans.append({"path": relative, "bytes": path.stat().st_size})
    return web.json_response({"orphan_count": len(orphans), "orphans": orphans[:500],
                              "note": "只报告不删除；确认后可手动清理"})


# ------------------------------------------------------------------ 上传


async def api_assets_upload(request):
    """
    POST /api/assets —— 批量上传（multipart）。

    要求：
      * 多文件一次提交，逐个独立入库（一个失败不影响其他）
      * 大文件流式落盘（不整文件读进内存）
      * 校验大小与类型
      * **不做二次转码**，原文件原样保留
    """
    report = A.ensure_layout()
    if not report["ok"]:
        return web.json_response({"error": "；".join(report["errors"]),
                                  "code": "library_unavailable",
                                  "report": report}, status=503)

    reader = await request.multipart()
    options = {"name": "", "category": "", "subtype": "", "tags": "", "note": ""}
    results = []
    errors = []

    root = A.library_root()
    tmp_dir = root / A.DIR_TMP
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 第一遍：把文件流式落到临时区，把选项字段读出来。
    #
    # 为什么分两遍：multipart 的字段顺序由客户端决定。前端按
    # 「文件先、选项后」提交时，若在读文件的同时就用 options，
    # 此刻 options 还是空的 —— 标签会静默丢失。
    # 所以先把文件全部落盘（流式，不进内存），等整个表单读完、
    # options 齐全后再统一入库。
    staged: list[dict] = []
    async for part in reader:
        if part.name in ("name", "category", "subtype", "tags", "note"):
            options[part.name] = (await part.text()).strip()
            continue
        if not part.filename:
            continue

        suffix = Path(part.filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            errors.append({"filename": part.filename,
                           "error": f"不支持的文件类型 {suffix or '(无扩展名)'}"})
            continue

        # --- 流式落临时区 + 算哈希（不整文件进内存） -------------------
        tmp = tmp_dir / f".upload-{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        total = 0
        overflow = False
        try:
            with tmp.open("wb") as handle:
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
                    total += len(chunk)
                    if total > A.MAX_UPLOAD_BYTES:
                        overflow = True
                        break
            if overflow:
                errors.append({"filename": part.filename,
                               "error": f"文件超过上限 {A.MAX_UPLOAD_BYTES // (1024 ** 3)} GB"})
                tmp.unlink(missing_ok=True)
                continue
            if total <= 0:
                errors.append({"filename": part.filename, "error": "文件为空"})
                tmp.unlink(missing_ok=True)
                continue
            staged.append({"tmp": tmp, "filename": part.filename,
                           "sha256": digest.hexdigest(), "bytes": total})
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            errors.append({"filename": part.filename,
                           "error": f"{type(exc).__name__}: {exc}"[:200]})

    # 第二遍：options 已齐全，逐个入库（一个失败不影响其他）
    tags = [t.strip() for t in options["tags"].replace(",", "，").split("，") if t.strip()]
    for item in staged:
        tmp = item["tmp"]
        fingerprint = item["sha256"]
        try:
            with _library_conn() as conn:
                dup = conn.execute(
                    "SELECT asset_id FROM asset_versions WHERE sha256=? "
                    "ORDER BY version DESC LIMIT 1", (fingerprint,)).fetchone()
                if dup:
                    existing = asset_repo.get_asset(conn, dup["asset_id"], include_deleted=True)
                    if existing and not existing["deleted_at"]:
                        tmp.unlink(missing_ok=True)
                        results.append({"filename": item["filename"], "ok": True,
                                        "duplicated": True, "asset_id": existing["id"],
                                        "name": existing["name"],
                                        "message": "素材已存在，已复用原记录"})
                        continue

                result = asset_service.register_asset(
                    conn,
                    source_path=tmp,
                    filename=item["filename"],
                    name=options["name"] or None,
                    category=options["category"] or None,
                    subtype=options["subtype"] or None,
                    tags=tags,
                    note=options["note"] or None,
                    source="upload",
                    creator=_client_id(request))
                tmp.unlink(missing_ok=True)   # register_asset 已把它移走，这里是兜底
                asset = result["asset"]
                results.append({
                    "filename": item["filename"], "ok": True, "duplicated": False,
                    "asset_id": asset["id"], "name": asset["name"],
                    "category": asset["category"], "subtype": asset["subtype"],
                    "bytes": result.get("version", {}).get("bytes"),
                    "sha256": result.get("sha256"),
                    "message": "已入库",
                })
        except LibraryError as exc:
            tmp.unlink(missing_ok=True)
            errors.append({"filename": item["filename"], "error": exc.message})
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            errors.append({"filename": item["filename"],
                           "error": f"{type(exc).__name__}: {exc}"[:200]})

    return web.json_response({
        "ok": not errors or bool(results),
        "results": results,
        "errors": errors,
        "uploaded": len(results),
        "failed": len(errors),
    })


# ------------------------------------------------------------------ 详情 / 元数据


async def api_asset_detail(request):
    """GET /api/assets/{id} —— 详情"""
    asset_id = request.match_info["id"]
    with _library_conn() as conn:
        asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
        if not asset:
            return web.json_response({"error": "素材不存在", "code": "asset_not_found"},
                                      status=404)
        version = asset_repo.current_version(conn, asset_id)
        versions = asset_repo.versions_of(conn, asset_id)
        analyses = asset_repo.analyses_of(conn, asset_id)
        refs = asset_service.asset_reference_summary(conn, asset_id)
    asset["versions"] = versions
    asset["current_version"] = version
    asset["analyses"] = analyses
    asset["latest_analysis"] = analyses[0] if analyses else None
    asset["references"] = refs
    asset["preview_url"] = (f"/api/assets/{asset_id}/preview"
                            if version and version["preview_path"] else None)
    asset["content_url"] = f"/api/assets/{asset_id}/content"
    return web.json_response(asset)


async def api_asset_update(request):
    """PATCH /api/assets/{id} —— 改元数据（含标签）"""
    asset_id = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    try:
        with _library_conn() as conn:
            asset = asset_repo.update_asset(conn, asset_id, payload)
    except LibraryError as exc:
        return _err(exc)
    return web.json_response(asset)


async def api_asset_tags_add(request):
    """POST /api/assets/{id}/tags —— 加标签"""
    asset_id = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    tags = payload.get("tags")
    if isinstance(tags, str):
        tags = [t for t in (part.strip() for part in tags.replace(",", "，").split("，")) if t]
    if not tags:
        return web.json_response({"error": "请提供标签"}, status=400)
    try:
        with _library_conn() as conn:
            if not asset_repo.get_asset(conn, asset_id):
                return web.json_response({"error": "素材不存在"}, status=404)
            result = asset_repo.add_tags(conn, asset_id, tags)
    except LibraryError as exc:
        return _err(exc)
    return web.json_response({"tags": result})


async def api_asset_tag_delete(request):
    """DELETE /api/assets/{id}/tags/{tag} —— 删标签"""
    asset_id = request.match_info["id"]
    tag = request.match_info["tag"]
    try:
        with _library_conn() as conn:
            if not asset_repo.get_asset(conn, asset_id):
                return web.json_response({"error": "素材不存在"}, status=404)
            result = asset_repo.remove_tag(conn, asset_id, tag)
    except LibraryError as exc:
        return _err(exc)
    return web.json_response({"tags": result})


# ------------------------------------------------------------------ 内容 / 预览


async def api_asset_content(request):
    """GET /api/assets/{id}/content —— 下载/播放原文件"""
    asset_id = request.match_info["id"]
    with _library_conn() as conn:
        version = asset_repo.current_version(conn, asset_id)
        asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
    if not version or not asset:
        return web.json_response({"error": "素材不存在"}, status=404)
    path = A.resolve_inside(A.library_root(), version["storage_path"])
    if not path or not path.is_file():
        return web.json_response({"error": "素材文件已丢失"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def api_asset_preview(request):
    """GET /api/assets/{id}/preview —— 缩略图"""
    asset_id = request.match_info["id"]
    with _library_conn() as conn:
        version = asset_repo.current_version(conn, asset_id)
    if not version or not version["preview_path"]:
        return web.json_response({"error": "没有缩略图"}, status=404)
    path = A.resolve_inside(A.library_root(), version["preview_path"])
    if not path or not path.is_file():
        return web.json_response({"error": "缩略图文件已丢失"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=300"})


# ------------------------------------------------------------------ 删除 / 还原


async def api_asset_delete(request):
    """
    DELETE /api/assets/{id} —— 删除（→ 回收站）

    - 有引用 -> 409 + 引用清单（服务端强制）
    - ?purge=1 -> 彻底删除
    """
    asset_id = request.match_info["id"]
    purge = request.query.get("purge") in ("1", "true", "yes")
    try:
        with _library_conn() as conn:
            if purge:
                result = asset_repo.purge(conn, asset_id)
            else:
                asset = asset_repo.soft_delete(conn, asset_id)
                result = {"deleted": asset_id, "deleted_at": asset["deleted_at"],
                          "purge_at": asset["purge_at"]}
    except LibraryError as exc:
        return _err(exc)
    return web.json_response(result)


async def api_asset_restore(request):
    """POST /api/assets/{id}/restore —— 从回收站还原"""
    asset_id = request.match_info["id"]
    try:
        with _library_conn() as conn:
            asset = asset_repo.restore(conn, asset_id)
    except LibraryError as exc:
        return _err(exc)
    return web.json_response(asset)


async def api_asset_refs(request):
    """GET /api/assets/{id}/refs —— 引用反查"""
    asset_id = request.match_info["id"]
    with _library_conn() as conn:
        summary = asset_service.asset_reference_summary(conn, asset_id)
    return web.json_response(summary)


# ------------------------------------------------------------------ 分析 / 纠错


def _analysis_paths(asset_id: str, version: dict):
    """分析用的文件路径：原件 + 派生目录。"""
    root = A.library_root()
    source = A.resolve_inside(root, version["storage_path"])
    derived = root / A.DIR_DERIVED / asset_id[:2] / f"{asset_id}_analysis"
    return source, derived


async def api_asset_analyze(request):
    """
    POST /api/assets/{id}/analyze —— 触发模型深度分析。

    body（可选）：
      provider_id      指定 Provider；不给则用默认多模态模型
      frame_interval   视频抽帧间隔（秒），默认 2.0
      max_frames       视频抽帧上限，默认 24
    """
    asset_id = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    options = {
        "frame_interval": payload.get("frame_interval"),
        "max_frames": payload.get("max_frames"),
        "derived_dir": None,
    }
    provider_id = payload.get("provider_id")

    with _library_conn() as conn:
        asset = asset_repo.get_asset(conn, asset_id)
        if not asset:
            return web.json_response({"error": "素材不存在", "code": "asset_not_found"},
                                      status=404)
        version = asset_repo.current_version(conn, asset_id)
        if not version:
            return web.json_response({"error": "素材没有可用版本"}, status=400)
        source, derived = _analysis_paths(asset_id, version)
        if source is None or not source.is_file():
            return web.json_response({"error": "素材文件已丢失"}, status=404)
        options["derived_dir"] = str(derived)

        revision = asset_repo.next_revision(conn, asset_id)
        try:
            result = await analysis.run_analysis(conn, asset, version, source,
                                                 options, provider_id)
        except LibraryError as exc:
            asset_repo.save_analysis(conn, asset_id, asset["category"], "", revision,
                                     provider=None, prompt="", error=exc.message)
            return _err(exc)

        record = asset_repo.save_analysis(
            conn, asset_id, result["kind"], result["text"], revision,
            provider=result["provider"], prompt=result["prompt"],
            frames=result["frames"], extra=result["extra"])
        analysis_text = record["text"]

    return web.json_response({
        "asset_id": asset_id,
        "revision": record["revision"],
        "text": analysis_text,
        "provider": record["provider_name"],
        "model": record["model"],
        "frames": record["frames"],
        "extra": record["extra"],
    })


async def api_asset_correction(request):
    """
    POST /api/assets/{id}/corrections —— 追加交互纠错（多轮）。

    body:
      question     用户的追问/纠正内容（必填）
      provider_id  可选
    """
    asset_id = request.match_info["id"]
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    question = str(payload.get("question") or payload.get("text") or "").strip()
    if not question:
        return web.json_response({"error": "请填写要纠正或追问的内容"}, status=400)

    provider_id = payload.get("provider_id")
    options = {"derived_dir": None,
               "frame_interval": payload.get("frame_interval"),
               "max_frames": payload.get("max_frames")}

    with _library_conn() as conn:
        asset = asset_repo.get_asset(conn, asset_id)
        if not asset:
            return web.json_response({"error": "素材不存在", "code": "asset_not_found"},
                                      status=404)
        version = asset_repo.current_version(conn, asset_id)
        if not version:
            return web.json_response({"error": "素材没有可用版本"}, status=400)
        source, derived = _analysis_paths(asset_id, version)
        if source is None or not source.is_file():
            return web.json_response({"error": "素材文件已丢失"}, status=404)
        options["derived_dir"] = str(derived)

        revision = asset_repo.next_revision(conn, asset_id)
        try:
            result = await analysis.run_correction(conn, asset, version, source,
                                                  question, options, provider_id)
        except LibraryError as exc:
            asset_repo.save_analysis(conn, asset_id, asset["category"], "", revision,
                                     provider=None, prompt=question, error=exc.message)
            return _err(exc)

        record = asset_repo.save_analysis(
            conn, asset_id, result["kind"], result["text"], revision,
            provider=result["provider"], prompt=question,
            frames=result["frames"], extra=result["extra"])

    return web.json_response({
        "asset_id": asset_id,
        "revision": record["revision"],
        "text": record["text"],
        "provider": record["provider_name"],
        "model": record["model"],
        "history": [{"revision": r["revision"], "prompt": r["prompt"],
                     "text": r["text"], "created_at": r["created_at"]}
                    for r in asset_repo.analyses_of(conn, asset_id)][:20],
    })


async def api_asset_analyses(request):
    """GET /api/assets/{id}/analyses —— 分析修订历史"""
    asset_id = request.match_info["id"]
    with _library_conn() as conn:
        records = asset_repo.analyses_of(conn, asset_id)
    return web.json_response({"analyses": records})


# ------------------------------------------------------------------ 媒体库右键入库


async def api_asset_from_media(request):
    """
    POST /api/assets/import/from-media —— 从媒体库（生成结果）右键入库。

    注意路由：这里用 /api/assets/import/from-media（4 段）而不是
    /api/assets/{id}/from-media。后者与 /api/assets/{id} 冲突时，
    aiohttp 会把 "from-media" 当成 {id} 匹配，导致 405。实测确认。

    body:
      job_id     任务 id（必填）
      batch_id   批次 id（可选；给了就是「有批次」来源）
      file       输出文件名（可选；不填取该任务第一个产物）
      tags       可选，逗号分隔
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)

    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        return web.json_response({"error": "缺少 job_id"}, status=400)
    batch_id = str(payload.get("batch_id") or "").strip() or None
    wanted_file = str(payload.get("file") or "").strip()
    tags_raw = payload.get("tags") or ""
    tags = [t.strip() for t in str(tags_raw).replace(",", "，").split("，") if t.strip()]

    with db_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return web.json_response({"error": "任务不存在"}, status=404)
        job = {
            "id": row["id"], "mode": row["mode"], "created_at": row["created_at"],
            "params": json.loads(row["params"] or "{}"),
            "outputs": json.loads(row["outputs"] or "[]"),
        }
        batch = None
        outputs = job["outputs"]
        if batch_id:
            brow = conn.execute("SELECT * FROM job_batches WHERE id=? AND job_id=?",
                                (batch_id, job_id)).fetchone()
            if not brow:
                return web.json_response({"error": "批次不存在或不属于该任务"}, status=404)
            batch = {"id": brow["id"], "idx": brow["idx"], "seed": brow["seed"],
                     "status": brow["status"],
                     "outputs": json.loads(brow["outputs"] or "[]")}
            outputs = batch["outputs"]
        if not outputs:
            return web.json_response({"error": "该条目没有产物可入库"}, status=400)

        chosen = None
        for item in outputs:
            if not isinstance(item, dict):
                continue
            if wanted_file and item.get("file") == wanted_file:
                chosen = item
                break
        if chosen is None:
            chosen = next((i for i in outputs if isinstance(i, dict)), None)
        if chosen is None:
            return web.json_response({"error": "找不到对应的产物"}, status=404)

    try:
        with _library_conn() as lib:
            result = asset_service.add_from_media_output(
                lib, job=job, output=chosen, batch=batch,
                creator=_client_id(request), tags=tags, outputs_dir=outputs_root())
    except LibraryError as exc:
        return _err(exc)

    asset = result["asset"]
    return web.json_response({
        "ok": True,
        "asset_id": asset["id"],
        "name": asset["name"],
        "duplicated": result.get("duplicated", False),
        "source": asset["source"],
        "source_ref": asset["source_ref"],
        "source_meta": asset["source_meta"],
        "message": "已添加到素材库" if not result.get("duplicated") else "素材已存在，已复用原记录",
    })


async def api_job_assets(request):
    """
    GET /api/jobs/{id}/assets —— 该任务的输入素材 + 产物素材。

    输入素材：asset_refs 里登记的引用（素材库 -> 任务）。
    产物素材：source=job_output 且 source_ref=该任务 id 的素材。
    """
    from h3studio import asset_repo as _repo

    job_id = request.match_info["id"]
    with db_conn() as conn:
        row = conn.execute("SELECT id, mode FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return web.json_response({"error": "任务不存在"}, status=404)

    with _library_conn() as lib:
        inputs = _repo.refs_of(lib, job_id)
        inputs = [r for r in inputs if r["kind"] == "job"]
        outputs = lib.execute(
            "SELECT id, name, category, subtype, preview_path, total_bytes FROM assets "
            "WHERE source='job_output' AND source_ref=?", (job_id,)).fetchall()
        output_items = [{
            "id": r["id"], "name": r["name"], "category": r["category"],
            "subtype": r["subtype"], "bytes": r["total_bytes"],
            "preview_url": (f"/api/assets/{r['id']}/preview" if r["preview_path"] else None),
        } for r in outputs]
    return web.json_response({"job_id": job_id, "inputs": inputs, "outputs": output_items})


# ------------------------------------------------------------------ 接线


def db_conn():
    """任务库连接。

    优先用注入的 server 模块（避免 import server 造成循环依赖）；
    注入的模块上有 db 就用它，否则回退到延迟 import。
    """
    if _SERVER is not None:
        getter = getattr(_SERVER, "db", None)
        if getter is not None:
            return getter()
    from server import db
    return db()


def outputs_root():
    if _SERVER is not None:
        out_dir = getattr(_SERVER, "OUT_DIR", None)
        if out_dir is not None:
            return out_dir
    from server import OUT_DIR
    return OUT_DIR


def register(app, server_module=None):
    """
    把素材库路由挂到 app 上。

    server_module 传入 server 模块本身（留作将来直接取 db()/OUT_DIR 的通道），
    素材库当前用延迟 import 取 db()，避免 asset_api 反向 import server 的循环依赖。
    """
    global _SERVER
    _SERVER = server_module

    app.router.add_get("/api/assets", api_assets_list)
    app.router.add_post("/api/assets", api_assets_upload)
    app.router.add_get("/api/assets/stats", api_assets_stats)
    app.router.add_get("/api/assets/tags", api_assets_tags)
    app.router.add_post("/api/assets/tags/rename", api_assets_tag_rename)
    app.router.add_get("/api/assets/picker", api_assets_picker)
    app.router.add_post("/api/assets/reconcile", api_assets_reconcile)
    app.router.add_get("/api/assets/{id}", api_asset_detail)
    app.router.add_patch("/api/assets/{id}", api_asset_update)
    app.router.add_delete("/api/assets/{id}", api_asset_delete)
    app.router.add_post("/api/assets/{id}/restore", api_asset_restore)
    app.router.add_get("/api/assets/{id}/content", api_asset_content)
    app.router.add_get("/api/assets/{id}/preview", api_asset_preview)
    app.router.add_get("/api/assets/{id}/refs", api_asset_refs)
    app.router.add_post("/api/assets/{id}/tags", api_asset_tags_add)
    app.router.add_delete("/api/assets/{id}/tags/{tag}", api_asset_tag_delete)
    app.router.add_post("/api/assets/{id}/analyze", api_asset_analyze)
    app.router.add_post("/api/assets/{id}/corrections", api_asset_correction)
    app.router.add_get("/api/assets/{id}/analyses", api_asset_analyses)
    app.router.add_post("/api/assets/import/from-media", api_asset_from_media)
    app.router.add_get("/api/jobs/{id}/assets", api_job_assets)


_SERVER = None
