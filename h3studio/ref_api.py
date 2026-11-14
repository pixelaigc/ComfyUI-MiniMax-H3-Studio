# -*- coding: utf-8 -*-
"""
Qwen-Image 2.1「图片编辑」的参考图 —— 素材库来源的 HTTP 接口层。

接口清单（统一前缀 /api/refs）：
  GET  /api/refs/assets          参考图选择器数据源（带描述/标签/入库时间/失效标记）
  POST /api/refs/resolve         批量把 asset_id 解析成参考图元数据（含描述、标签）
  POST /api/refs/sort            智能排序（可降级为确定性基础排序）
  POST /api/refs/validate        一致性校验（占位符 vs 图片数）
  POST /api/refs/prompt          生成带占位标记的提示词（<imageN> + 描述）

设计约束（来自需求）：
  * 参考图**必须**来自素材库 —— 所以对外只有 asset_id，没有裸路径入口；
  * 复用素材库现有的数据源与筛选能力（/api/assets 的搜索/标签筛选），
    不另建一套列表 —— 这里只做「按 asset_id 取参考图元数据」的补充；
  * 素材被删除时参考图要**明确失效**，不能静默。
"""
from __future__ import annotations


from aiohttp import web

from h3studio import asset_repo
from h3studio import assets as A
from h3studio import ref_images as RI
from h3studio.assets import LibraryError


def _err(exc: LibraryError):
    payload = {"error": exc.message, "code": exc.code}
    payload.update(exc.extra or {})
    return web.json_response(payload, status=exc.status)


def _library_conn():
    return A.connect()


# ------------------------------------------------------------------ 描述提取


def _describe(asset: dict, analysis: dict | None, tags: list[str]) -> tuple[str, str]:
    """
    从素材库条目里取出「参考图描述」。

    返回 (描述文本, 来源标记)：
      analysis  来自素材库的模型分析结果（首选 —— 参考图通常入库时已分析过）
      user      用户手写（前端传 user_description 覆盖时用）
      empty     素材库没提供描述 —— **不要编造**，由前端给手动补充入口

    取分析文本时只取首行/前若干字：参考图的占位描述要短，
    否则提示词会被大段分析文字淹没。完整分析仍在素材库详情页里可看。
    """
    if analysis:
        text = (analysis.get("text") or "").strip()
        if text:
            return _condense(text), "analysis"
    note = (asset.get("note") or "").strip()
    if note:
        return _condense(note), "analysis"
    if tags:
        return "、".join(tags[:6]), "analysis"
    return "", "empty"


def _condense(text: str, limit: int = 80) -> str:
    """把长描述压成一行短句（占位描述用）。

    素材库的分析文本动辄几百字（分【主体人物】【服装与配饰】等小节），
    直接塞进提示词会淹没用户的编辑指令。这里只取**第一个有内容的小节**
    并截断到 limit 字。
    """
    text = (text or "").strip()
    if not text:
        return ""
    # 分析文本常见结构：【主体人物】…  取第一段有实际内容的
    for chunk in text.replace("【", "\n【").split("\n"):
        chunk = chunk.strip()
        if len(chunk) > 6:
            text = chunk
            break
    # 去掉残留的方括号标记
    text = text.replace("【", "").replace("】", " ")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _ref_entry(conn, asset_id: str, *, user_description: str = "") -> dict:
    """
    把一个 asset_id 解析成参考图元数据。

    素材不存在 / 已进回收站 -> 返回 missing=True 的条目（**不抛异常**），
    这样前端能明确显示「素材已删除」，而不是提交时才炸。
    """
    asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
    if not asset:
        return {
            "asset_id": asset_id, "name": "", "description": "",
            "description_source": "empty", "tags": [], "created_at": None,
            "preview_url": None, "missing": True,
            "version": None, "reason": "素材不存在",
        }
    if asset.get("deleted_at"):
        return {
            "asset_id": asset_id, "name": asset.get("name") or "",
            "description": "", "description_source": "empty", "tags": [],
            "created_at": asset.get("created_at"), "preview_url": None,
            "missing": True, "version": None,
            "reason": "素材已在素材库回收站中",
        }

    version = asset_repo.current_version(conn, asset_id)
    if not version:
        return {
            "asset_id": asset_id, "name": asset.get("name") or "",
            "description": "", "description_source": "empty", "tags": [],
            "created_at": asset.get("created_at"), "preview_url": None,
            "missing": True, "version": None,
            "reason": "素材没有可用版本（文件可能已丢失）",
        }

    tags = asset_repo.tags_of(conn, asset_id)
    analysis = asset_repo.latest_analysis(conn, asset_id)
    description, source = _describe(asset, analysis, tags)
    if user_description.strip():
        # 用户手写优先 —— 标成 user，界面据此显示「手动补充」
        description = user_description.strip()
        source = "user"

    preview_url = (f"/api/assets/{asset_id}/preview"
                   if version.get("preview_path") else None)
    return {
        "asset_id": asset_id,
        "version": version.get("version"),
        "name": asset.get("name") or "",
        "description": description,
        "description_source": source,
        "tags": tags,
        "created_at": asset.get("created_at"),
        "preview_url": preview_url,
        "missing": False,
        "width": version.get("width"),
        "height": version.get("height"),
    }


# ------------------------------------------------------------------ 接口


async def api_refs_assets(request):
    """
    GET /api/refs/assets —— 参考图选择器数据源。

    复用素材库既有的搜索/标签/类型筛选能力（asset_repo.search_assets），
    只做两件事：
      1. 默认只看 image 类（参考图必须是图片）；
      2. 额外带上「描述 + 标签 + 入库时间 + 是否已分析」，
         让选择器能直接显示「这张图是干啥的」。

    查询参数（与 /api/assets 一致）：
      q / tag / tags_any / tags_all / category / subtype / source /
      sort / page / page_size
    """
    query = request.query
    filters = {
        "category": (query.get("category") or "image").strip(),
        "subtype": query.get("subtype", ""),
        "tag": query.get("tag", ""),
        "tags_all": query.get("tags_all", ""),
        "tags_any": query.get("tags_any", ""),
        "q": query.get("q", ""),
        "source": query.get("source", ""),
        "creator": query.get("creator", ""),
        "sort": query.get("sort", "created_desc"),
        "page": query.get("page", "1"),
        "page_size": query.get("page_size", "60"),
    }
    try:
        with _library_conn() as conn:
            result = asset_repo.search_assets(conn, filters)
            items = []
            for asset in result.get("assets", []):
                analysis = asset_repo.latest_analysis(conn, asset["id"])
                tags = asset_repo.tags_of(conn, asset["id"])
                description, source = _describe(asset, analysis, tags)
                items.append({
                    "id": asset["id"],
                    "name": asset["name"],
                    "category": asset["category"],
                    "subtype": asset.get("subtype"),
                    "width": asset.get("width"),
                    "height": asset.get("height"),
                    "preview_url": asset.get("preview_url"),
                    "description": description,
                    "description_source": source,
                    "tags": tags,
                    "created_at": asset.get("created_at"),
                    "analyzed": bool(analysis),
                    "analysis_revision": asset.get("analysis_revision"),
                    "ref_count": asset.get("ref_count"),
                })
    except LibraryError as exc:
        return _err(exc)
    return web.json_response({
        "assets": items,
        "total": result.get("total", len(items)),
        "page": result.get("page", 1),
        "page_size": result.get("page_size", len(items)),
        "max_ref_images": RI.MAX_REF_IMAGES,
    })


async def api_refs_resolve(request):
    """
    POST /api/refs/resolve —— 批量把 asset_id 解析成参考图元数据。

    body: {"refs": [{"asset_id": "...", "user_description": "..."}, ...]}
    返回归一化后的参考图列表（顺序即占位顺序）。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    raw = payload.get("refs")
    if not isinstance(raw, list):
        raw = []
    with _library_conn() as conn:
        entries = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            asset_id = str(item.get("asset_id") or "").strip()
            if not asset_id:
                continue
            entries.append(_ref_entry(
                conn, asset_id,
                user_description=str(item.get("user_description") or "")))
    refs = RI.normalize_refs(entries)
    return web.json_response({
        "refs": refs,
        "max_ref_images": RI.MAX_REF_IMAGES,
        "min_ref_images": RI.MIN_REF_IMAGES,
    })


async def api_refs_sort(request):
    """
    POST /api/refs/sort —— 智能排序。

    body: {"refs": [...], "prompt": "...", "use_model": true/false}

    **降级保证**：use_model=false 或内部异常时，退化为确定性基础排序
    （入库时间倒序），返回 degraded=true，**不报错**。
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    raw = payload.get("refs")
    if not isinstance(raw, list):
        raw = []
    refs = RI.normalize_refs(raw)
    prompt = str(payload.get("prompt") or "")
    use_model = bool(payload.get("use_model", True))

    try:
        result = RI.smart_sort(refs, prompt=prompt, use_model=use_model)
    except Exception as exc:
        # 排序是可选功能 —— 绝不能因为它失败就挡住用户提交
        ordered = RI.basic_sort(refs)
        return web.json_response({
            "refs": ordered, "ranking": [], "degraded": True,
            "reason": f"智能排序不可用，已降级为入库时间排序：{type(exc).__name__}",
        })
    return web.json_response({
        "refs": result["refs"],
        "ranking": result["ranking"],
        "degraded": result["degraded"],
    })


async def api_refs_validate(request):
    """
    POST /api/refs/validate —— 一致性校验。

    body: {"refs": [...], "prompt": "..."}
    返回 {"ok": bool, "errors": [...], "warnings": [...]}
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    raw = payload.get("refs")
    if not isinstance(raw, list):
        raw = []
    refs = RI.normalize_refs(raw)
    prompt = str(payload.get("prompt") or "")
    result = RI.validate_refs(refs, prompt=prompt, require=bool(payload.get("require", True)))
    return web.json_response(result)


async def api_refs_prompt(request):
    """
    POST /api/refs/prompt —— 生成带占位标记的提示词。

    body: {"refs": [...], "prompt": "...", "style": "inline"|"bullet"}

    返回 {"prompt": "<注入占位后的提示词>", "block": "<占位块本身>",
           "placeholders": [...], "validation": {...}}
    """
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求格式错误"}, status=400)
    raw = payload.get("refs")
    if not isinstance(raw, list):
        raw = []
    refs = RI.normalize_refs(raw)
    prompt = str(payload.get("prompt") or "")
    style = str(payload.get("style") or "inline")
    block = RI.build_reference_block(refs, style=style)
    merged = RI.inject_reference_block(prompt, refs, style=style)
    validation = RI.validate_refs(refs, prompt=merged, require=False)
    return web.json_response({
        "prompt": merged,
        "block": block,
        "placeholders": RI.parse_placeholders(merged),
        "validation": validation,
    })


def register(app):
    """把参考图路由挂到 app 上。"""
    app.router.add_get("/api/refs/assets", api_refs_assets)
    app.router.add_post("/api/refs/resolve", api_refs_resolve)
    app.router.add_post("/api/refs/sort", api_refs_sort)
    app.router.add_post("/api/refs/validate", api_refs_validate)
    app.router.add_post("/api/refs/prompt", api_refs_prompt)
