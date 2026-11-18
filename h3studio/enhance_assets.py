# -*- coding: utf-8 -*-
"""
提示词增强 —— **素材上下文**的解析与落地（独立模块）。

【为什么要有这个模块】
「参考生图 / 首尾帧 / 参考生视频」这类面板里，用户已经从**素材库**
引用了素材（首帧 / 尾帧 / 参考图 / 参考视频 / 参考音乐），并填了提示词，
然后点「增强」。增强出来的提示词必须**基于这些素材** —— 否则模型在
增强阶段根本没看过素材，扩写出来的内容与用户引用的画面无关，
用户后续出图时素材等于白引用。

原实现里 `/api/enhance` 虽然留了 `images` 参数（本地图片路径列表），
但**前端从来不传**（web/enhance-menu.js 的 runBasic / runRefine 都没带
`images`），而且浏览器本地路径服务端根本读不到 —— 于是这条链路是死的。

【模块职责】把「前端传来的素材引用」变成**服务端可读的图片路径列表**：

    前端（面板当前引用）
        ↓  asset_refs: [{"asset_id": "...", "slot": "ref_image", "ordinal": 0}]
    本模块
        ↓  解析素材库条目 -> 找到物理文件 -> 归一成模型能吃的图片
    增强模型（多模态）
        ↓  images: ["/abs/path/a.png", "/abs/path/frame_0001.jpg", ...]

【三类素材怎么变成模型能看的东西】
  * image —— 原图直接给模型（多模态接口读的就是图片本身）。
  * video —— 抽帧后把关键帧给模型（视频模型以外的 LLM 读不了视频）。
            复用 h3studio/media_probe.extract_frames（与素材库分析同一套）。
  * audio —— LLM **读不了音频**。这里给出明确降级：把素材库里已有的
            分析文本（如果有）作为**文字上下文**附到提示词里，并如实
            标注「音频内容以分析文本形式提供」。绝不假装模型听到了音频。

【为什么不用「上传到 ComfyUI 再取回文件名」那套】
那是出图链路（_apply_asset_refs）用的 —— 素材要变成 ComfyUI 的
input 文件名才能进工作流。增强链路是 **Studio 自己的进程**直接把图片
喂给 Provider（llm.py 的 `_data_url` 会把本地文件读成 base64 data URL），
所以这里只需要**服务端本地可读的绝对路径**，不需要经过 ComfyUI。

【硬约束】
  * 只接受**素材库里的 asset_id**（不接裸路径）—— 与参考图链路同纪律，
    避免任意文件读取。
  * 素材不存在 / 已删除 / 文件丢失 -> **如实报错**，不静默跳过
    （静默跳过 = 用户以为素材参与了增强，其实没有）。
  * 解析上限（图片张数、视频抽帧数）有硬顶，防止一次增强拖垮模型。
"""
from __future__ import annotations

import json
from pathlib import Path

from h3studio import asset_repo
from h3studio import assets as A
from h3studio import media_probe


#: 单次增强最多送多少张图给模型。
#:
#: 【为什么有上限】多模态接口会把每张图编码成 base64 塞进请求体。
#: 一张 1080p JPEG ≈ 200KB → base64 ≈ 270KB。20 张就是 5MB+ 的请求体，
#: 而且本地模型（llama.cpp / vLLM）的视觉 token 数也会爆。
#: 实测本地多模态模型能稳定吃 4~8 张 —— 所以取 8。
MAX_IMAGES = 8

#: 视频素材最多抽多少帧送进模型（每段视频）。
MAX_VIDEO_FRAMES = 4

#: 视频抽帧的时间间隔（秒）。
VIDEO_FRAME_INTERVAL = 1.0

#: 音频素材的**文字上下文**上限（字符）。
#:
#: 【为什么音频走文字而不是硬塞】本地多模态 LLM 没有音频输入通道
#: （OpenAI 兼容接口的 content 只支持 image_url / audio_url，而 llama.cpp
#: 侧只实现了 image_url）。把音频描述成「模型听到了」是**撒谎**，
#: 用户会以为增强结果基于音频内容。所以：能给文字就给文字
#: （素材库的分析文本），给不了就明确标注「音频未参与」。
MAX_AUDIO_TEXT_CHARS = 2000


class EnhanceAssetError(RuntimeError):
    """素材上下文解析失败。message 面向用户，直接可读。"""

    def __init__(self, message: str, code: str = "enhance_asset_error"):
        super().__init__(message)
        self.message = message
        self.code = code


# ------------------------------------------------------------------ 归一


def normalize_refs(raw) -> list[dict]:
    """
    规整前端传来的素材引用。

    输入（前端从面板当前状态收集）：
      [{"asset_id": "asset_xxx", "slot": "ref_image", "ordinal": 0}, ...]
      也接受纯字符串数组 ["asset_xxx", ...]（容错）。

    输出：`[{"asset_id", "slot", "ordinal"}, ...]`，已剔除空项与重复项。
    """
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        asset_id = ""
        slot = "ref_image"
        ordinal = 0
        if isinstance(item, dict):
            asset_id = str(item.get("asset_id") or "").strip()
            slot = str(item.get("slot") or slot).strip() or slot
            try:
                ordinal = int(item.get("ordinal") or 0)
            except (TypeError, ValueError):
                ordinal = 0
        elif isinstance(item, str):
            asset_id = item.strip()
        if not asset_id or asset_id in seen:
            continue
        seen.add(asset_id)
        out.append({"asset_id": asset_id, "slot": slot, "ordinal": ordinal})
    return out


# ------------------------------------------------------------------ 解析


def _asset_entry(conn, asset_id: str) -> tuple[dict, dict, Path] | tuple[None, None, None, str]:
    """
    取素材条目 + 当前版本 + 物理文件路径。

    返回 (asset, version, path) —— 正常；
    或 (None, None, None, reason) —— 不可用，reason 面向用户。
    """
    asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
    if not asset:
        return None, None, None, "素材不存在"
    if asset.get("deleted_at"):
        return None, None, None, "素材已在素材库回收站中"
    version = asset_repo.current_version(conn, asset_id)
    if not version:
        return None, None, None, "素材没有可用版本（文件可能已丢失）"
    path = A.resolve_inside(A.library_root(), version.get("storage_path") or "")
    if not path or not path.is_file():
        return None, None, None, "素材文件已丢失"
    return asset, version, path


def _category_of(asset: dict, version: dict) -> str:
    """素材类别：优先用 assets.category，其次看 mime。"""
    category = (asset.get("category") or "").strip().lower()
    if category:
        return category
    mime = (version.get("mime") or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return "other"


def _slot_label(slot: str) -> str:
    """槽位 -> 中文标签（用于提示词里告诉模型每张图是什么角色）。"""
    return {
        "first_frame": "首帧",
        "last_frame": "尾帧",
        "ref_image": "参考图",
        "character_ref": "角色参考图",
        "ref_video": "参考视频",
        "source_video": "源视频",
        "ref_audio": "参考音频",
        "bgm": "背景音乐",
        "voice": "配音",
    }.get(slot, "参考素材")


def _analysis_text(conn, asset_id: str) -> str:
    """素材库里已有的分析文本（音频素材降级成文字上下文时用）。"""
    try:
        analysis = asset_repo.latest_analysis(conn, asset_id)
    except Exception:
        return ""
    if not analysis:
        return ""
    return (analysis.get("text") or "").strip()


def _extract_video_frames(video: Path, work_dir: Path, prefix: str) -> list[str]:
    """抽帧 -> 交给模型的图片路径列表。失败返回空列表（不抛）。"""
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        frames = media_probe.extract_frames(
            video, work_dir, prefix=prefix,
            interval=VIDEO_FRAME_INTERVAL, max_frames=MAX_VIDEO_FRAMES)
        return [f["path"] for f in frames]
    except Exception:
        return []


def resolve_assets(refs: list, work_dir: Path | None = None) -> dict:
    """
    把素材引用解析成模型能用的上下文。

    返回：
      {
        "images": ["/abs/path.jpg", ...],   # 送给多模态模型的图片（本地路径）
        "notes":  ["<image1> = 参考图「xxx」", ...],  # 图序语义（给模型看）
        "text_context": "……",              # 音频素材的文字上下文（可能为空）
        "items":  [ {...每个素材的处理结果...} ],
        "skipped": [ {"asset_id", "name", "reason"} ],  # 处理不了的素材（如音频）
      }

    行为约定：
      * 素材缺失 / 被删 / 文件丢失 -> 抛 EnhanceAssetError（**如实报错**，
        不静默跳过 —— 静默跳过等于用户以为素材参与了增强）。
      * 音频 -> 不进 images，但把分析文本放进 text_context，并在 skipped 里
        说明「音频以文字形式提供」/「音频未参与」。
      * 视频 -> 抽帧；抽不出帧时如实说明，不假装看过。
    """
    refs = normalize_refs(refs)
    if not refs:
        return {"images": [], "notes": [], "text_context": "",
                "items": [], "skipped": []}

    if work_dir is None:
        work_dir = A.library_root() / A.DIR_DERIVED / "enhance"

    images: list[str] = []
    notes: list[str] = []
    text_parts: list[str] = []
    items: list[dict] = []
    skipped: list[dict] = []

    with A.connect() as conn:
        for index, ref in enumerate(refs):
            asset_id = ref["asset_id"]
            entry = _asset_entry(conn, asset_id)
            if entry[0] is None:
                raise EnhanceAssetError(
                    f"素材「{asset_id}」无法用于提示词增强：{entry[3]}")
            asset, version, path = entry
            name = asset.get("name") or asset_id
            category = _category_of(asset, version)
            slot_label = _slot_label(ref.get("slot") or "ref_image")

            if category == "image":
                if len(images) >= MAX_IMAGES:
                    skipped.append({"asset_id": asset_id, "name": name,
                                    "reason": f"超过单次增强的图片上限（{MAX_IMAGES} 张）"})
                    continue
                images.append(str(path))
                notes.append(f"<image{len(images)}> = {slot_label}「{name}」")
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": ref.get("slot"), "kind": "image"})
                continue

            if category == "video":
                frames = _extract_video_frames(
                    path, work_dir / asset_id, prefix="enhance_frame")
                if not frames:
                    skipped.append({"asset_id": asset_id, "name": name,
                                    "reason": "无法从该视频抽帧（文件可能已损坏或格式不支持）"})
                    items.append({"asset_id": asset_id, "name": name, "category": category,
                                  "slot": ref.get("slot"), "kind": "video", "frames": 0})
                    continue
                added = 0
                for frame in frames:
                    if len(images) >= MAX_IMAGES:
                        break
                    images.append(frame)
                    added += 1
                if added:
                    notes.append(
                        f"<image{len(images) - added + 1}>~<image{len(images)}> = "
                        f"{slot_label}「{name}」抽出的 {added} 帧")
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": ref.get("slot"), "kind": "video", "frames": added})
                continue

            if category == "audio":
                # 【降级】LLM 读不了音频 -> 用素材库的分析文本代替。
                text = _analysis_text(conn, asset_id)
                if text:
                    text = text[:MAX_AUDIO_TEXT_CHARS]
                    text_parts.append(
                        f"【{slot_label}「{name}」的音频内容（来自素材库分析文本，"
                        f"模型无法直接听取音频）】\n{text}")
                    reason = "音频以素材库分析文本形式提供（模型无法直接听取音频）"
                else:
                    reason = "音频未参与增强（模型无法听取音频，且素材库没有该音频的分析文本）"
                skipped.append({"asset_id": asset_id, "name": name, "reason": reason})
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": ref.get("slot"), "kind": "audio",
                              "text_context": bool(text)})
                continue

            skipped.append({"asset_id": asset_id, "name": name,
                            "reason": f"暂不支持把「{category}」类型的素材用于提示词增强"})
            items.append({"asset_id": asset_id, "name": name, "category": category,
                          "slot": ref.get("slot"), "kind": "other"})

    return {
        "images": images,
        "notes": notes,
        "text_context": "\n\n".join(text_parts),
        "items": items,
        "skipped": skipped,
    }


def describe(resolved: dict) -> str:
    """
    把解析结果渲染成**给模型看的文字上下文**。

    这段文字会接在原提示词前面，告诉模型：
      * 一共给了几张图、每张图对应什么角色（图序语义）；
      * 音频素材的内容以什么形式提供（或明确说明没提供）。

    没有素材时返回空串 —— 调用方据此判断「不要注入任何东西」，
    保证无素材面板的提示词与改动前**逐字一致**。
    """
    if not resolved:
        return ""
    parts: list[str] = []
    notes = resolved.get("notes") or []
    if notes:
        parts.append(
            "以下是本次任务所引用的素材（按顺序编号，图片已随本消息一并提供）：\n"
            + "\n".join(f"- {n}" for n in notes)
        )
    text_context = (resolved.get("text_context") or "").strip()
    if text_context:
        parts.append(text_context)
    skipped = resolved.get("skipped") or []
    if skipped:
        detail = "；".join(
            f"{s.get('name') or s.get('asset_id')}（{s.get('reason')}）" for s in skipped)
        parts.append(
            f"【注意】以下素材**未**以视觉形式提供给你：{detail}。"
            "请不要臆测它们的内容。")
    return "\n\n".join(parts)


def summary(resolved: dict) -> dict:
    """给前端回显用的精简摘要。"""
    resolved = resolved or {}
    return {
        "image_count": len(resolved.get("images") or []),
        "asset_count": len(resolved.get("items") or []),
        "notes": list(resolved.get("notes") or []),
        "skipped": list(resolved.get("skipped") or []),
        "has_text_context": bool((resolved.get("text_context") or "").strip()),
    }


def dumps(resolved: dict) -> str:
    """调试用：把解析结果转成可读 JSON。"""
    return json.dumps(summary(resolved), ensure_ascii=False, indent=2)
