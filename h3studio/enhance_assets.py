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

【参考标签 —— 官方 MiniMax H3 写法（2026-11-21 现场复验后补齐）】
上一轮只解决了「素材要传进增强请求」，但**增强后的提示词里没有参考
标签** —— 模型描述了图片内容，却没标出「这是第几张参考图」。
于是下游 H3 工作流无法把提示词里的引用和实际送进去的图对上。

官方规范（MiniMax H3 官方 skill `h3-prompt-writing/references/ref-en.txt`，
本机路径 `/home/ubuntu/comfyui/custom_nodes/comfyui-minimax-h3-prompt-enhancer-T8/
official_skills/h3-prompt-writing/references/ref-en.txt`）规定四类标签：

    | 标签         | 含义                                              |
    |--------------|---------------------------------------------------|
    | <Subject N>  | 从参考素材里抽象出来、可被复用的可见内容           |
    | <Picture N>  | 作为具体帧锚点（首帧 / 尾帧 / 关键帧 / 构图锚）的参考图 |
    | <Video N>    | 提供剪辑源 / 续写起点 / 整体时序结构的参考视频      |
    | <Audio N>    | 被复制或被参考的音频信号                          |

**关键规则：每个类别各自独立编号**（官方原文：
"`<Video N>` and `<Audio N>` are numbered independently. Each index
indicates only the label's order within its own category and does not
encode a pairing between the two categories."）。
所以「一张图 + 一个视频 + 一段音频」= `<Picture 1>` / `<Video 1>` / `<Audio 1>`，
**不是** `<Picture 1>` / `<Video 2>` / `<Audio 3>`。

**编号必须与面板素材顺序一一对应**：图片按面板槽位顺序（首帧 -> 尾帧 ->
参考图 1..N），视频同理，音频同理。本模块按传入 refs 的顺序分配，
而 refs 的顺序由前端按面板槽位顺序生成 —— 两边口径一致。

【为什么不用「<image1>」这种自创写法】
`<image1>` 是 Qwen-Image 官方 PE-I2I 的语法，与 H3 的 `<Picture N>` 是两套
东西。本项目的 H3 视频链路（flf / r2v / t2v）必须用 H3 官方标签 ——
模型按 `<Picture N>` 把文字引用和它看到的第 N 张图对上。

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


#: 单次增强最多送多少张**图片**给模型。
#:
#: 【为什么是 9】H3 节点的真实上限是 `ref_images max=9`
#: （h3studio/h3_video_modes.py: MAX_REF_IMAGES = 9，节点 schema 实测），
#: 面板的参考图槽位也是 9 个（web/app.js: `for (let i = 0; i < 9; i++) mk(...)`）。
#: 取更小的值（比如原来的 8）会让**第 9 张图被静默丢弃** ——
#: 用户填满了 9 个参考图，增强时第 9 张根本没进模型，而提示词里
#: 也不会有它的标签 -> 素材等于白引用。2026-11-21 现场复验明确要求
#: 「全部素材都要真传，不能静默丢弃」，所以这里对齐节点真实上限。
#:
#: 【为什么不是更大】多模态接口会把每张图编码成 base64 塞进请求体，
#: 视觉 token 数也会爆 —— 但 9 张已经是**面板本身允许的上限**，
#: 再大就超出了出图链路的能力边界。
MAX_IMAGES = 9

#: 视频素材最多抽多少帧送进模型（每段视频）。
MAX_VIDEO_FRAMES = 4

#: 单次增强的**视觉输入总预算**（图片 + 视频抽帧）。
#:
#: 【为什么要一个总预算】面板允许 9 张图 + 3 段视频；如果每段视频抽 4 帧，
#: 光视频就是 12 帧，加上 9 张图共 21 个视觉输入 —— 本地多模态模型的
#: 视觉 token 会爆（实测本机 Qwen 多模态 8 张图已接近可用上限）。
#:
#: 【分配规则】
#:   * 图片优先 —— 面板里的每一张参考图都必须进去（`<Picture N>` 要对得上）；
#:   * 剩下的额度分给视频抽帧；
#:   * 分不到额度的视频**如实记进 skipped**（「未参与」+ 原因），
#:     绝不静默丢弃 —— 静默丢弃就是「用户以为素材参与了增强」。
MAX_TOTAL_VISUAL = 16

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


# ------------------------------------------------------------------ 官方参考标签

#: 官方 H3 参考标签的四个类别（见模块头部的规范出处）。
#:
#: 【编号规则 —— 官方原文】"`<Video N>` and `<Audio N>` are numbered
#: independently. Each index indicates only the label's order within its
#: own category and does not encode a pairing between the two categories."
#:
#: 所以**每个类别各自从 1 开始编号**：
#:   1 张图 + 1 个视频 + 1 段音频 -> <Picture 1> / <Video 1> / <Audio 1>
#: 而不是 <Picture 1> / <Video 2> / <Audio 3>。
#:
#: `<Subject N>` 是「从素材里抽象出来的可复用内容」—— 它描述的是**内容**
#: 而不是素材文件本身，所以由模型在写作时按需给出，本模块**不预分配**
#: （预分配会凭空造出模型不承认的主体）。
LABEL_CATEGORIES = ("picture", "video", "audio")

#: 素材类别 -> 官方标签名。
CATEGORY_LABEL_NAME = {
    "image": "Picture",
    "video": "Video",
    "audio": "Audio",
}


def label_for(category: str, index: int) -> str:
    """
    某个素材类别的第 index 个（1 基）素材对应的**官方标签**。

    例：label_for("image", 1) -> "<Picture 1>"
        label_for("video", 1) -> "<Video 1>"
        label_for("audio", 1) -> "<Audio 1>"
    """
    name = CATEGORY_LABEL_NAME.get(category)
    if not name:
        return ""
    return f"<{name} {index}>"


def label_pattern() -> str:
    """匹配官方参考标签的正则（供服务端校验输出是否真的带了标签）。"""
    import re
    return r"<(?:Subject|Picture|Video|Audio)\s*\d+>"


def has_reference_label(text: str) -> bool:
    """文本里有没有官方参考标签。"""
    import re
    return bool(re.search(label_pattern(), text or ""))


def extract_reference_labels(text: str) -> list[str]:
    """抽出文本里出现的官方参考标签（按出现顺序，去重）。"""
    import re
    out: list[str] = []
    for match in re.finditer(label_pattern(), text or ""):
        tag = re.sub(r"\s+", " ", match.group(0)).strip()
        if tag not in out:
            out.append(tag)
    return out


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
        "notes":  ["<Picture 1> = 首帧「xxx」", ...],  # 官方标签语义（给模型看）
        "text_context": "……",              # 音频素材的文字上下文（可能为空）
        "items":  [ {...每个素材的处理结果...} ],
        "skipped": [ {"asset_id", "name", "reason"} ],  # 处理不了的素材（如音频）
        "labels": ["<Picture 1>", ...],     # 本次分配出去的全部官方标签
      }

    行为约定：
      * 素材缺失 / 被删 / 文件丢失 -> 抛 EnhanceAssetError（**如实报错**，
        不静默跳过 —— 静默跳过等于用户以为素材参与了增强）。
      * 音频 -> 不进 images，但把分析文本放进 text_context，并在 skipped 里
        说明「音频以文字形式提供」/「音频未参与」。
      * 视频 -> 抽帧；抽不出帧时如实说明，不假装看过。
      * **每个素材类别各自独立编号**（官方 H3 规则），编号按传入顺序，
        与面板槽位顺序一一对应。
    """
    refs = normalize_refs(refs)
    if not refs:
        return {"images": [], "notes": [], "text_context": "",
                "items": [], "skipped": [], "labels": []}

    if work_dir is None:
        work_dir = A.library_root() / A.DIR_DERIVED / "enhance"

    images: list[str] = []
    notes: list[str] = []
    text_parts: list[str] = []
    items: list[dict] = []
    skipped: list[dict] = []
    labels: list[str] = []
    # 官方编号：每个类别各自从 1 开始
    counters = {cat: 0 for cat in LABEL_CATEGORIES}

    # ---- 第 1 遍：解析素材条目（不做任何视觉处理）----
    #
    # 【为什么要分两遍】图片必须**优先**拿到视觉预算 —— `<Picture N>`
    # 的编号要跟面板里的图片顺序一一对应，图片少了编号就全错位。
    # 所以先把所有素材解析成条目，再按「图片优先」分配视觉额度。
    entries: list[dict] = []
    with A.connect() as conn:
        for ref in refs:
            asset_id = ref["asset_id"]
            entry = _asset_entry(conn, asset_id)
            if entry[0] is None:
                raise EnhanceAssetError(
                    f"素材「{asset_id}」无法用于提示词增强：{entry[3]}")
            asset, version, path = entry
            name = asset.get("name") or asset_id
            category = _category_of(asset, version)
            entries.append({
                "asset_id": asset_id,
                "name": name,
                "category": category,
                "path": path,
                "slot": ref.get("slot"),
                "slot_label": _slot_label(ref.get("slot") or "ref_image"),
                "conn": conn,
            })

        # ---- 第 2 遍：分配官方标签 + 处理 ----
        for item in entries:
            asset_id = item["asset_id"]
            name = item["name"]
            category = item["category"]
            slot_label = item["slot_label"]

            if category == "image":
                if len(images) >= MAX_IMAGES:
                    # 超出图片上限 -> 如实记进 skipped（不静默丢弃）
                    skipped.append({"asset_id": asset_id, "name": name,
                                    "reason": f"超过单次增强的图片上限（{MAX_IMAGES} 张）"})
                    continue
                counters["picture"] += 1
                label = label_for("image", counters["picture"])
                images.append(str(item["path"]))
                notes.append(f"{label} = {slot_label}「{name}」")
                labels.append(label)
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": item["slot"], "kind": "image",
                              "label": label, "ordinal": counters["picture"] - 1})
                continue

            if category == "video":
                frames = _extract_video_frames(
                    item["path"], work_dir / asset_id, prefix="enhance_frame")
                if not frames:
                    skipped.append({"asset_id": asset_id, "name": name,
                                    "reason": "无法从该视频抽帧（文件可能已损坏或格式不支持）"})
                    items.append({"asset_id": asset_id, "name": name, "category": category,
                                  "slot": item["slot"], "kind": "video", "frames": 0})
                    continue
                # 视觉预算：图片已用掉多少，剩下的额度分给视频抽帧
                added = 0
                first_frame_index = len(images) + 1
                for frame in frames:
                    if added >= MAX_VIDEO_FRAMES or len(images) >= MAX_TOTAL_VISUAL:
                        break
                    images.append(frame)
                    added += 1
                if not added:
                    # 额度用尽 -> **不分配标签**（标签是给「真正参与了」的
                    # 素材用的；给一个没参与的素材分配标签会让编号出现空洞，
                    # 模型也会去引用一个它根本没看到的东西）。
                    skipped.append({"asset_id": asset_id, "name": name,
                                    "reason": "视觉输入额度已用完，该视频未参与增强"})
                    items.append({"asset_id": asset_id, "name": name, "category": category,
                                  "slot": item["slot"], "kind": "video", "frames": 0})
                    continue
                counters["video"] += 1
                label = label_for("video", counters["video"])
                # 【视频与 <Picture N> 的关系】官方规范里 <Video N> 是
                # 「整段视频」这个资产；抽出来的帧只是给模型「看」用的
                # 视觉输入，不是独立的 <Picture N>。
                # 所以这里只登记 <Video N>，不占用 Picture 编号 ——
                # 否则 Picture 编号就跟面板图片顺序对不上了。
                notes.append(
                    f"{label} = {slot_label}「{name}」"
                    f"（已抽取 {added} 帧供模型查看）")
                labels.append(label)
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": item["slot"], "kind": "video", "frames": added,
                              "label": label, "ordinal": counters["video"] - 1,
                              "frame_image_range": [first_frame_index, len(images)]})
                continue

            if category == "audio":
                # 【降级】LLM 读不了音频 -> 用素材库的分析文本代替。
                text = _analysis_text(item["conn"], asset_id)
                counters["audio"] += 1
                label = label_for("audio", counters["audio"])
                labels.append(label)
                if text:
                    text = text[:MAX_AUDIO_TEXT_CHARS]
                    text_parts.append(
                        f"【{label} = {slot_label}「{name}」的音频内容"
                        f"（来自素材库分析文本，模型无法直接听取音频）】\n{text}")
                    reason = "音频以素材库分析文本形式提供（模型无法直接听取音频）"
                else:
                    reason = "音频未参与增强（模型无法听取音频，且素材库没有该音频的分析文本）"
                skipped.append({"asset_id": asset_id, "name": name, "reason": reason,
                                "label": label})
                items.append({"asset_id": asset_id, "name": name, "category": category,
                              "slot": item["slot"], "kind": "audio",
                              "label": label, "ordinal": counters["audio"] - 1,
                              "text_context": bool(text)})
                continue

            skipped.append({"asset_id": asset_id, "name": name,
                            "reason": f"暂不支持把「{category}」类型的素材用于提示词增强"})
            items.append({"asset_id": asset_id, "name": name, "category": category,
                          "slot": item["slot"], "kind": "other"})

    return {
        "images": images,
        "notes": notes,
        "text_context": "\n\n".join(text_parts),
        "items": items,
        "skipped": skipped,
        "labels": labels,
    }


def describe(resolved: dict) -> str:
    """
    把解析结果渲染成**给模型看的文字上下文**。

    这段文字会接在原提示词前面，告诉模型：
      * 一共给了几张图、每张图对应什么角色（**官方标签** `<Picture N>` /
        `<Video N>` / `<Audio N>`，编号与面板素材顺序一一对应）；
      * 音频素材的内容以什么形式提供（或明确说明没提供）。

    【为什么用官方标签而不是自创的 <image1>】
    H3 官方规范（ref-en.txt）规定参考引用一律用 `<Picture N>` / `<Video N>` /
    `<Audio N>` / `<Subject N>`。模型按这些标签把文字里的引用和它看到的
    第 N 个视觉输入对上；用别的写法，标签就对不上，等于没标。

    没有素材时返回空串 —— 调用方据此判断「不要注入任何东西」，
    保证无素材面板的提示词与改动前**逐字一致**。
    """
    if not resolved:
        return ""
    parts: list[str] = []
    notes = resolved.get("notes") or []
    if notes:
        parts.append(
            "以下是本次任务所引用的素材。**必须用下面的官方参考标签引用它们**"
            "（标签编号已按素材顺序固定，不要另编编号）：\n"
            + "\n".join(f"- {n}" for n in notes)
        )
    text_context = (resolved.get("text_context") or "").strip()
    if text_context:
        parts.append(text_context)
    skipped = resolved.get("skipped") or []
    if skipped:
        detail = "；".join(
            f"{s.get('label') or s.get('name') or s.get('asset_id')}"
            f"（{s.get('reason')}）" for s in skipped)
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
        # 本次分配的官方标签（前端回显 + 服务端校验输出用）
        "labels": list(resolved.get("labels") or []),
    }


def dumps(resolved: dict) -> str:
    """调试用：把解析结果转成可读 JSON。"""
    return json.dumps(summary(resolved), ensure_ascii=False, indent=2)
