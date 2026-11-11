# -*- coding: utf-8 -*-
"""
内置 Agent —— 聊天区**媒体上传**接口。

【为什么单独一个模块】
上传是「HTTP 层的边界」（multipart 解析 / 类型校验 / 大小上限 / 落盘），
与 Agent 的编排逻辑（多步推理 / 工具链 / 任务下发）是**两种关注点**。
混进 agent.py 会让那个已经很长的文件更难读，而两者又必须共享
「素材库」这一份权威数据源 —— 所以：
  * 本模块只负责「把上传的图片变成素材库条目（asset_id）」
  * 落库/去重/出缩略图**复用** h3studio.asset_service.register_asset
    （与素材库页的 /api/assets 上传**完全同一条链路**，不另造一套）
  * 上下文注入在 h3studio/agent.py 里做（那是 Agent 的地盘）

【与素材库页上传的差异 —— 为什么需要这个接口而不是直接用 /api/assets】
素材库页的上传是「批量入库 + 打标签 + 分类」，语义是「整理素材库」。
Agent 聊天区的上传语义是「**这一条消息的附件**」，有额外约束：
  * 只收图片（用户是要给参考图，不是要归档视频）
  * 数量上限（一次对话能挂几张参考图）
  * 单文件大小上限比素材库宽松？不 —— **更严**（见 MAX_IMAGE_BYTES）
  * 入库来源标记成 agent_upload，界面上能看出是聊天附件
但**落库本身完全复用** register_asset —— 这样去重、缩略图、
元数据探测、转码兜底全部一致，不会出现「Agent 传的图和素材库传的图
行为不一样」。

【安全边界】
  * 扩展名白名单 + **魔数嗅探**双重校验（只看扩展名会被
    「把 .exe 改名成 .png」骗过去）
  * 大小上限按**流式读取时**累计，超了就立刻停并删临时文件
  * 文件名不落盘（原名只作为素材名存进库），杜绝路径穿越
  * 落在素材库 tmp 区，靠 register_asset 做原子移动
"""
from __future__ import annotations

import uuid
from pathlib import Path

from h3studio import analysis_jobs
from h3studio import asset_repo
from h3studio import asset_service
from h3studio import assets as asset_store
from h3studio.assets import LibraryError

#: 允许上传的图片扩展名（用户要求：至少 png/jpg/webp）。
#: 这里再带上 jpeg/bmp/gif —— 它们都是标准图片格式，浏览器能预览，
#: PIL 也能解码；把它们拒掉对用户没有任何好处。
ALLOWED_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif",
}

#: 单张图片大小上限（字节）。
#:
#: 【为什么比素材库的 8 GB 严得多】
#: 素材库是「归档」，用户可能丢几个 G 的视频进去；
#: 而聊天附件是「参考图」，Qwen-Image / MOSS 的参考图会被整张读进
#: 推理上下文 —— 一张 50 MB 的图会直接把推理服务的显存/内存吃爆。
#: 20 MB 足够覆盖手机原图与绝大多数设计稿，且足够安全。
MAX_IMAGE_BYTES = 20 * 1024 * 1024

#: 一次上传最多几张（用户要求：建议上限 9 张，与 Qwen-Image 参考图对齐）。
#:
#: 【为什么不直接用 ref_images.MAX_REF_IMAGES = 10】
#: Qwen-Image 官方能力上限是 10 张，而**素材库槽位**的上限也是 10。
#: 但 Agent 一次要「上传 + 引用 + 建任务」，给用户在交互层留 1 张余量
#: 可以避免「刚好卡在 10 张边界」时的边界困惑（9 → 10 还能再加 1 张，
#: 10 → 11 直接被拒，用户会觉得莫名其妙）。
#: 真实能力上限仍然是 10（服务端会再校验一次），这里只是交互层的软上限。
MAX_IMAGES_PER_UPLOAD = 9

#: 魔数嗅探表：(扩展名, 头部字节, 最小长度)。
#:
#: 【为什么要嗅探 —— 不能只信扩展名】
#: 扩展名是客户端给的，攻击者可以把任意内容改名成 .png 上传。
#: 只看扩展名 = 素材库里会出现「名为 png 实为任意二进制」的条目。
#: 所以这里核对**文件真实格式**的头部字节：
#:   PNG  -> \x89PNG\r\n\x1a\n
#:   JPEG -> \xff\xd8\xff
#:   WEBP -> RIFF....WEBP
#:   BMP  -> BM
#:   GIF  -> GIF87a / GIF89a
#: 只接受**确实是对应格式**的文件；GIF 只查头（动画 GIF 合法）。
MAGIC_TABLE = (
    (".png", (b"\x89PNG\r\n\x1a\n",), 8),
    (".jpg", (b"\xff\xd8\xff",), 3),
    (".jpeg", (b"\xff\xd8\xff",), 3),
    (".webp", (b"RIFF",), 12),      # 真正的 WEBP 标记在 offset 8
    (".bmp", (b"BM",), 2),
    (".gif", (b"GIF87a", b"GIF89a"), 6),
)

#: WEBP 的完整魔数：offset 0 是 RIFF，offset 8 是 WEBP。
WEBP_RIFF = b"RIFF"
WEBP_TAG = b"WEBP"


class UploadError(Exception):
    """上传被拒。message 面向用户，可直接读。"""

    def __init__(self, message: str, code: str = "invalid_upload",
                 status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _detect_image_kind(filename: str, head: bytes) -> str | None:
    """
    按**头部字节**判断这是什么图片格式。

    返回规范化扩展名（".png" / ".jpg" / ".webp" / ".bmp" / ".gif"），
    不匹配任何已知格式时返回 None。

    【为什么不信任扩展名】见 MAGIC_TABLE 的说明。
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".webp":
        # WEBP 必须同时满足 RIFF 头 + offset 8 的 WEBP 标记
        if (len(head) >= 12 and head[:4] == WEBP_RIFF
                and head[8:12] == WEBP_TAG):
            return ".webp"
        return None
    for ext, magics, min_len in MAGIC_TABLE:
        if ext == ".webp":
            continue
        if len(head) < min_len:
            continue
        for magic in magics:
            if head.startswith(magic):
                return ext
    return None


def _validate_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        allowed = "、".join(sorted(ALLOWED_IMAGE_SUFFIXES))
        raise UploadError(
            f"不支持的图片类型 {suffix or '(无扩展名)'}，"
            f"只接受 {allowed}",
            code="unsupported_type")
    return suffix


async def _read_and_validate_part(part, tmp_dir: Path) -> dict:
    """
    把一个 multipart 文件 part 流式落到临时区并做全部校验。

    返回 {"tmp": Path, "filename": str, "suffix": str, "bytes": int,
          "sha256": str}；被拒时抛 UploadError（临时文件已清理）。
    """
    filename = Path(part.filename or "upload.png").name  # 只取 basename
    suffix = _validate_suffix(filename)

    tmp = tmp_dir / f".agent-upload-{uuid.uuid4().hex}.tmp"
    total = 0
    head = b""
    overflow = False
    try:
        with tmp.open("wb") as handle:
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                if not head:
                    head = chunk[:32]
                handle.write(chunk)
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    overflow = True
                    break
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise UploadError(f"读取文件失败：{type(exc).__name__}: {exc}"[:200],
                          code="read_failed", status=500) from exc

    if overflow:
        tmp.unlink(missing_ok=True)
        raise UploadError(
            f"图片超过上限 {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
            code="file_too_large", status=413)
    if total <= 0:
        tmp.unlink(missing_ok=True)
        raise UploadError("文件为空", code="empty_file")

    # --- 魔数嗅探：文件**真的**是那个格式吗？ ---
    detected = _detect_image_kind(filename, head)
    if detected is None:
        tmp.unlink(missing_ok=True)
        raise UploadError(
            "文件内容不是有效的图片（扩展名与真实格式不符）",
            code="invalid_image_content")
    # 真实格式与扩展名不一致时以**真实格式**为准（例如 .png 其实是 jpeg），
    # 避免 mime 写错导致下游 PIL/ComfyUI 读图失败。
    real_suffix = detected

    return {"tmp": tmp, "filename": filename, "suffix": real_suffix,
            "bytes": total}


def _register_one(item: dict) -> dict:
    """
    把一个已落盘的临时文件入库（复用素材库的 register_asset）。

    返回 {"asset_id", "name", "preview_url", "duplicated", "bytes"}。
    """
    with asset_store.connect() as conn:
        try:
            result = asset_service.register_asset(
                conn,
                source_path=item["tmp"],
                filename=item["filename"],
                category="image",
                subtype="photo",
                # 【刻意不写 note】
                # ref_api._describe 会把 note 当成**描述的兜底**（还标成
                # "analysis" 来源）。如果这里写「Agent 聊天区上传的附件」，
                # 素材库里每张附件的「描述」都会变成这句固定文案 ——
                # 模型会以为那就是图片的内容。
                # 来源信息应该走 source 字段（= "agent_upload"），
                # 描述只能来自**真正的分析结果**。
                note=None,
                source="agent_upload",
                creator="agent",
            )
        except LibraryError as exc:
            raise UploadError(exc.message, code=exc.code,
                              status=exc.status) from exc

        asset = result.get("asset") or {}
        asset_id = asset.get("id") or ""
        version = result.get("version") or {}
        if not version:
            # 【去重路径 —— 实测踩过的坑】
            # register_asset 命中 sha256 去重时返回的是
            #   {"asset": <既有条目>, "duplicated": True}
            # **没有 version 键** —— 于是 version.get("preview_path") 是 None，
            # preview_url 变成空 -> 前端 <img src=""> -> 缩略图**显示不出来**。
            #
            # 实测：同一张图传两次，第二次缩略图是坏的。
            # 修法：去重时**自己去查一次当前版本**（current_version）。
            version = asset_repo.current_version(conn, asset_id) or {}
        preview_url = (f"/api/assets/{asset_id}/preview"
                       if version.get("preview_path") else None)
        return {
            "asset_id": asset_id,
            "name": asset.get("name") or item["filename"],
            "preview_url": preview_url,
            "duplicated": bool(result.get("duplicated")),
            "bytes": item["bytes"],
            "width": version.get("width"),
            "height": version.get("height"),
        }


def _maybe_start_analysis(asset_id: str) -> dict:
    """
    给刚上传的图排一次多模态分析（**异步**，立刻返回）。

    【为什么要分析】
    Agent 引用参考图时，提示词/描述来自**素材库已有的分析结论**
    （ref_api._describe / agent._tool_describe_asset）。
    如果不分析，模型看到的是「一个没有描述的图」，
    无法做「复刻这张图的风格」「把人物换成…」这类需要理解图内容的操作。

    【为什么不自己看图】
    用户明确要求：走已有素材分析结果，不要自己看片/自行跑 ComfyUI。
    所以这里**复用素材库的分析管线**（analysis_jobs.start_analysis），
    与素材库页「深度分析」按钮是同一条路。

    【失败了怎么办】
    分析失败**不影响上传成功** —— 素材照样入库，用户照样能把它当参考图。
    只是描述会空着，模型会看到「analyzed=false」。

    【重复上传同一张图时】
    第二次命中 sha256 去重 -> 复用同一个素材 -> 如果它已经在分析中，
    再排一次就是浪费 CPU（分析很吃资源）。
    所以先查有没有在跑的分析任务（active_job_of），有就不再排。
    """
    try:
        with asset_store.connect() as conn:
            active = asset_repo.active_job_of(conn, asset_id)
        if active:
            return {"analysis_started": False,
                    "analysis_already_running": True,
                    "analysis_job_id": active.get("id")}
        job = analysis_jobs.start_analysis(asset_id, "image", mode="analyze")
        return {"analysis_started": True,
                "analysis_job_id": (job or {}).get("id")}
    except Exception:  # noqa: BLE001
        # 分析是「锦上添花」，失败不能连累上传本身
        return {"analysis_started": False}


def _describe_asset(asset_id: str) -> dict:
    """
    取素材的**已有**描述（来自素材库分析结果，不是现编的）。

    与 agent._tool_describe_asset 用同一个解析入口（ref_api._ref_entry），
    保证「Agent 聊天区上传的图」和「素材库里的图」对模型来说长得一样。
    """
    from h3studio import ref_api

    with asset_store.connect() as conn:
        entry = ref_api._ref_entry(conn, asset_id)
    return {
        "asset_id": asset_id,
        "name": entry.get("name") or "",
        "description": entry.get("description") or "",
        "description_source": entry.get("description_source") or "empty",
        "tags": entry.get("tags") or [],
        "preview_url": entry.get("preview_url"),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "missing": bool(entry.get("missing")),
        "reason": entry.get("reason") or "",
    }


async def handle_upload(request) -> dict:
    """
    处理一次 multipart 上传请求。

    期望的表单字段：
      files[] / file / image —— 一个或多个图片文件

    返回 {"ok": True, "results": [...], "errors": [...]}。
    单个文件失败**不影响**其它文件（与素材库上传同一语义）。
    """
    report = asset_store.ensure_layout()
    if not report.get("ok"):
        raise UploadError("；".join(report.get("errors") or ["素材库不可用"]),
                          code="library_unavailable", status=503)

    reader = await request.multipart()
    tmp_dir = asset_store.library_root() / asset_store.DIR_TMP
    tmp_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    errors: list[dict] = []

    async for part in reader:
        if not part.filename:
            continue
        if len(results) >= MAX_IMAGES_PER_UPLOAD:
            errors.append({
                "filename": part.filename,
                "error": (f"一次最多上传 {MAX_IMAGES_PER_UPLOAD} 张图片，"
                          f"多余的已忽略"),
            })
            continue
        try:
            item = await _read_and_validate_part(part, tmp_dir)
        except UploadError as exc:
            errors.append({"filename": part.filename, "error": exc.message,
                           "code": exc.code})
            continue

        try:
            entry = _register_one(item)
        except UploadError as exc:
            item["tmp"].unlink(missing_ok=True)
            errors.append({"filename": item["filename"], "error": exc.message,
                           "code": exc.code})
            continue
        finally:
            # register_asset 已把文件原子移动到正式位置；这里兜底清理
            item["tmp"].unlink(missing_ok=True)

        # 分析（异步）；失败不影响上传成功
        analysis = _maybe_start_analysis(entry["asset_id"])
        described = _describe_asset(entry["asset_id"])
        entry.update({
            "analysis_started": analysis.get("analysis_started", False),
            "analyzed": bool(described.get("description")),
            "description": described.get("description") or "",
            "width": described.get("width") or entry.get("width"),
            "height": described.get("height") or entry.get("height"),
        })
        results.append(entry)

    return {
        "ok": bool(results) or not errors,
        "results": results,
        "errors": errors,
        "uploaded": len(results),
        "failed": len(errors),
        "max_images": MAX_IMAGES_PER_UPLOAD,
        "max_bytes": MAX_IMAGE_BYTES,
    }
