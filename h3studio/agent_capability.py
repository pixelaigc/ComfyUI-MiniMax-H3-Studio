# -*- coding: utf-8 -*-
"""
内置 Agent 的**能力真源**（单一真源）—— 从服务端真实配置动态派生。

【为什么需要这个模块 —— 2026-11-20 用户实测报出的缺陷】

Agent 之前把「视频分辨率只有 544p / 768p」「时长 5~15 秒」
「音乐时长 4~360 秒」「加速 LoRA 只有 none/ckpt850/v4step600」
这些**能力事实**硬编码在三个地方：

  1. ``h3studio/agent.py`` 的 ``SYSTEM_PROMPT``（系统提示词）
  2. ``_tool_list_modes`` 的返回值（工具结果）
  3. ``create_job`` 工具的 JSON Schema 描述（工具描述）

三处各写一份 ⇒ 必然漂移 ⇒ 必然与真实能力脱节。
用户实测原话：「视频分辨率只有 544p / 768p，请求 480p 实际按 768p 生成」
—— 这是**假话**：真实能力是 2.0 MP 画布内任意宽高（480p = 0.4MP =
16:9 下 864×480），而且 Agent 侧校验器会把 ``resolution="480p"``
**当成非法值拦下来**（真源是过时的 ``workflows.RES_MP``）。

【本模块的职责】

**只做一件事**：把「服务端真实能力」渲染成 Agent 能读的文本/结构。
它**不新增**任何能力判定 —— 每一个数字与取值集合都来自既有真源：

  * 视频尺寸策略  -> ``h3studio.video_models.VIDEO_MODE_SIZE_POLICY``
  * 视频时长      -> ``h3studio.video_models.VIDEO_DURATION_MIN/MAX``
  * 视频底模      -> ``h3studio.video_models.VIDEO_MODELS``
  * 注意力机制    -> ``h3studio.video_models.VIDEO_ATTENTION_MODES``
  * 加速 LoRA     -> ``h3studio.video_models.all_accel_lora_keys()``
  * 采样器/调度器 -> ``h3studio.video_models.VIDEO_SAMPLERS/SCHEDULERS``
  * 图像族模型/档位 -> ``h3studio.workflows.*_MODELS`` / ``*_RES_TIERS``
  * 音乐时长      -> ``h3studio.music_yue2.YUE2_MIN/MAX/DEFAULT_DURATION``
  * 音乐谱面/格式 -> ``h3studio.music_yue2.YUE2_MODES`` 等
  * 素材槽位      -> ``h3studio.agent_schema.MODE_SLOTS/MODE_MEDIA_SLOTS``
  * 参考图上限    -> ``h3studio.agent_schema.REF_IMAGE_MAX``（按 mode 派生）
  * 负面提示词矩阵 -> ``h3studio.agent_schema.negative_support``

【三条硬规则】

  1. **本模块不允许出现任何能力字面量** —— 凡是「544p」「5~15」
     「4~360」这种数字，都必须从上面列出的真源取。
  2. **改配置即改陈述** —— 真源常量变了，渲染出来的能力自述
     随之改变（这是目标 B 的验收点，靠单测锁住）。
  3. **不与服务端校验器抢活** —— 参数是否合法仍由
     ``agent_schema.validate_params`` 与
     ``server.prepare_job_params`` 判定；本模块只负责**如实描述**。

【测试】tests/test_agent_capability_truth.py
"""
from __future__ import annotations

from . import agent_schema
from . import gen_types
from . import video_models as vmodels
from . import workflows as wf


# ------------------------------------------------------------------ 视频尺寸


def video_size_policy(mode: str) -> dict:
    """该视频模式的**真实尺寸策略**（面积上限 / 对齐倍数 / 是否可自定义）。"""
    return vmodels.size_policy_for_mode(mode)


def video_resolution_forms(mode: str) -> dict:
    """
    该视频模式支持的**分辨率表达方式**（不是「档位白名单」）。

    返回::

        {
          "tiers": ["480p", "544p", ...],   # 可直接用的档位标签（短边像素）
          "max_pixels_mp": 2.0,             # 总像素上限（MP）
          "multiple": 32,                   # 宽高对齐倍数
          "min_edge": 32, "max_edge": 16384,
          "custom_width_height": True,      # 是否支持直接给 width/height
          "example": {"16:9": "480p = 864x480"},
          "source": "video_models.VIDEO_MODE_SIZE_POLICY",
        }

    【为什么不再是「只有 544p / 768p」】
    旧面板的 ``#resSeg`` 只有两档，那是**界面**的遗留；
    生成侧的实际上限是 2.0 MP（``VIDEO_MODE_SIZE_POLICY``），
    且 ``custom_size: True`` —— 面板本来就有「输出尺寸」手填框。
    详见 ``video_models.py`` 里 VIDEO_MODE_SIZE_POLICY 的注释。
    """
    policy = video_size_policy(mode)
    tiers = video_size_tiers()
    return {
        "tiers": tiers,
        "max_pixels_mp": policy.get("max_pixels_mp"),
        "multiple": policy.get("multiple"),
        "min_edge": policy.get("min"),
        "max_edge": policy.get("max"),
        "custom_width_height": bool(policy.get("custom_size")),
        "source": "video_models.VIDEO_MODE_SIZE_POLICY",
    }


def video_size_tiers() -> list[str]:
    """
    视频真实可选的**短边像素档位**（全集，去重升序）。

    来源：``video_models.size_presets_for_aspect`` 对全部比例求并集 ——
    即面板下拉里真实存在的那些档位。
    """
    tiers: set[str] = set()
    for aspect in vmodels.VIDEO_ASPECTS:
        for preset in vmodels.size_presets_for_aspect(aspect):
            tiers.add(str(preset.get("short_edge") or ""))
    out = sorted((int(t) for t in tiers if t.isdigit()))
    return [f"{value}p" for value in out]


def video_tier_pixels(tier: str, aspect: str = "16:9") -> tuple[int, int] | None:
    """某个短边档位在指定比例下的**真实像素宽高**（找不到返回 None）。"""
    text = str(tier or "").strip()
    if text.endswith("p"):
        text = text[:-1]
    if not text.isdigit():
        return None
    short = int(text)
    for preset in vmodels.size_presets_for_aspect(aspect):
        if int(preset.get("short_edge") or 0) == short:
            return int(preset["width"]), int(preset["height"])
    return None


# ------------------------------------------------------------------ 视频其它参数


def video_models_list() -> list[dict]:
    """视频底模清单（key + 标签 + 是否支持 Ref2VA）。"""
    out: list[dict] = []
    for key, entry in vmodels.VIDEO_MODELS.items():
        out.append({
            "key": key,
            "label": entry.get("label") or key,
            "supports_ref2va": bool(entry.get("supports_ref2va")),
            "steps": entry.get("steps"),
            "sampler": entry.get("sampler"),
            "scheduler": entry.get("scheduler"),
        })
    return out


def video_attention_modes() -> list[dict]:
    """注意力机制清单（**排除**已被移除的项）。"""
    out: list[dict] = []
    for key, entry in vmodels.VIDEO_ATTENTION_MODES.items():
        if key in vmodels.VIDEO_REMOVED_ATTENTION_MODES:
            continue
        out.append({
            "key": key,
            "label": entry.get("label") or key,
            "speed_label": entry.get("speed_label") or "",
            "node": entry.get("node") or "",
        })
    return out


def video_accel_loras() -> list[str]:
    """加速 LoRA 全集（旧路径的 lora + 新路径的组合枚）。"""
    return list(vmodels.all_accel_lora_keys())


def video_samplers() -> list[str]:
    return list(vmodels.VIDEO_SAMPLERS)


def video_schedulers() -> list[str]:
    return list(vmodels.VIDEO_SCHEDULERS)


# ------------------------------------------------------------------ 音乐参数


def music_duration_range() -> tuple[int, int]:
    """音乐时长区间（秒）。来源：music_yue2。"""
    return (int(agent_schema.MUSIC_DURATION_MIN),
            int(agent_schema.MUSIC_DURATION_MAX))


def music_default_duration() -> int:
    """音乐默认时长（秒）。来源：music_yue2.YUE2_DEFAULT_DURATION。"""
    try:
        from . import music_yue2
        return int(music_yue2.YUE2_DEFAULT_DURATION)
    except Exception:
        return music_duration_range()[1]


def music_plan_modes() -> list[str]:
    return list(agent_schema.MUSIC_PLAN_MODES)


def music_formats() -> list[str]:
    return list(agent_schema.MUSIC_FORMATS)


def music_checkpoints() -> list[str]:
    return list(agent_schema.MUSIC_CHECKPOINTS)


# ------------------------------------------------------------------ 模式总览


def buildable_modes() -> list[str]:
    """**能真正下发**的模式（= workflows.BUILDERS = create_job 的白名单）。"""
    return list(wf.BUILDERS.keys())


def mode_ref_image_max(mode: str) -> int:
    """该模式允许的参考图数量上限。"""
    try:
        from . import h3image
        if mode == "i2i_h3image":
            return int(h3image.H3_IMAGE_MAX_REF_IMAGES)
    except Exception:
        pass
    try:
        from . import h3_video_modes
        if mode == "r2v":
            return int(h3_video_modes.MAX_REF_IMAGES)
    except Exception:
        pass
    return int(agent_schema.REF_IMAGE_MAX)


def mode_asset_slots(mode: str) -> dict:
    """该模式的素材槽位（single / multi / 媒体类型映射 / 数量上限）。"""
    plan = agent_schema.mode_slot_plan(mode)
    return {
        "single": list(plan.get("single") or []),
        "multi": list(plan.get("multi") or []),
        "media_slots": dict(plan.get("media_slots") or {}),
        "max_per_slot": dict(plan.get("max_per_slot") or {}),
        "max_ref_images": mode_ref_image_max(mode),
    }


# ------------------------------------------------------------------ 文本渲染


def _fmt_range(span) -> str:
    lo, hi = span
    return f"{lo}~{hi}"


def video_capability_text() -> str:
    """视频模式的真实能力（一段可直接进提示词的中文）。"""
    policy = video_size_policy("t2v")
    tiers = video_size_tiers()
    duration = (int(vmodels.VIDEO_DURATION_MIN), int(vmodels.VIDEO_DURATION_MAX))
    attention = video_attention_modes()
    accel = video_accel_loras()
    models = video_models_list()
    ref2va = [m["key"] for m in models if m["supports_ref2va"]]
    mp = policy.get("max_pixels_mp")
    tier_sample = ""
    sample = video_tier_pixels("480p")
    if sample:
        tier_sample = f"（例如 480p = {sample[0]}×{sample[1]}）"
    return (
        f"- **视频分辨率**：统一用 **megapixels（MP 预算）× resolution_percent"
        f"（百分比）** 表达 —— megapixels 是该模式的真实面积上限"
        f"（当前 {mp} MP），resolution_percent 是相对这个上限的百分比"
        f"（100% = {mp} MP）。也可以直接给 width/height"
        f"（宽高必须是 {policy.get('multiple')} 的倍数，"
        f"单边 {policy.get('min')}~{policy.get('max')}）。"
        f"换算后宽高会自动对齐到 {policy.get('multiple')} 的倍数；"
        f"超出真实上限时**自动缩到可用值并说明调整原因**"
        f"（原始请求值 → 生效值 → 原因），不会静默改写。"
        f"短边档位（{', '.join(tiers)}）{tier_sample}"
        f"仅作显示标签，用 get_mode_schema 看当前真实可用集合。"
        f"- **视频时长**：{_fmt_range(duration)} 秒（整数）。"
        f"超上限会被明确拒绝并给出上限值。"
        f"- **视频底模**（model_key）：{', '.join(m['key'] for m in models)}。"
        f"参考生视频（r2v）必须用支持 Ref2VA 的权重：{', '.join(ref2va)}。"
        f"- **注意力机制**（attention）：{', '.join(a['key'] for a in attention)}。"
        f"- **精度**：**没有独立的 precision 参数** —— 精度已经编码在"
        f"底模权重文件里（int8_convrot / bf16 / pruned_w4a8），"
        f"选底模（model_key）就等于选精度。"
        f"- **加速 LoRA**：{', '.join(accel)}（另有 none = 不用加速）。"
        f"只列与该底模**实测兼容**的那些；"
        f"不兼容的会在提交时被明确拒绝并说明原因，不会静默加载。"
        f"- **采样器 / 调度器**：sampler {len(video_samplers())} 种、"
        f"scheduler {len(video_schedulers())} 种，用 get_mode_schema 看清单。"
    )


def music_capability_text() -> str:
    """音乐模式的真实能力（一段可直接进提示词的中文）。"""
    span = music_duration_range()
    default = music_default_duration()
    return (
        f"- **音乐时长**：字段名是 **max_duration**（不是 duration，用 duration 会被忽略），"
        f"范围 {_fmt_range(span)} 秒，默认 {default} 秒。"
        f"- **歌词**：lyrics，留空 = 纯器乐。"
        f"- **谱面模式**：plan_mode = {', '.join(music_plan_modes())}。"
        f"- **输出格式**：format = {', '.join(music_formats())}。"
        f"- **模型**：checkpoint = {', '.join(music_checkpoints()) or '（仅默认）'}。"
    )


def image_capability_text() -> str:
    """图像模式的真实能力（按族列）。"""
    lines = ["- **图像分辨率**：按族不同，各自有独立档位集合。"]
    families: dict[str, list[str]] = {}
    for mode in wf.BUILDERS:
        if not agent_schema.is_image_mode(mode):
            continue
        family = image_family(mode)
        if family in families:
            continue
        tiers = agent_schema.mode_resolutions(mode)
        if tiers:
            families[family] = tiers
    for family, tiers in families.items():
        lines.append(f"  * {family}：{', '.join(tiers)}")
    lines.append(
        "  * 超出该族直出上限时：Z-Image 族会自动走官方 2K 放大路径，"
        "其余族没有放大路径（会直出并可能崩图）—— 用 get_mode_schema "
        "查该族的上限。")
    lines.append(
        "  * 负面提示词只在部分族/模型上生效；用 get_mode_schema 看 "
        "negative_prompt 的逐模型矩阵，不生效时如实告诉用户。")
    return "\n".join(lines)


def aspect_text() -> str:
    """画面比例的真实集合。"""
    aspects = list(wf.ASPECT_MAP.keys())
    return (f"- **画面比例**：{', '.join(aspects)}"
            f"（音乐模式没有画面比例）。")


def asset_rule_text() -> str:
    """素材引用规则（真实闸门）。"""
    return (
        "- **素材必须来自素材库**：所有引用都用 asset_id，"
        "且必须是**已入库**的素材（服务端会查库，编造的 id 会被拒）。"
        "槽位：参考图 -> ref_image / asset_ids；首尾帧 -> first_frame / last_frame；"
        "参考生音乐 -> ref_audio（只能 1 段）；"
        "参考生视频（r2v）额外可吃参考视频（ref_video，最多 "
        f"{agent_schema.REF_VIDEO_MAX} 段）与参考音频（ref_audio）。"
        "参考图数量上限按模式而定，用 get_mode_schema 查。"
    )


def mode_list_text() -> str:
    """真实存在的模式清单。"""
    items = []
    for mode in buildable_modes():
        info = gen_types.type_for_mode(mode)
        items.append(f"{mode}（{info.get('label') or mode}）")
    return f"- **可下发的生成模式**：{', '.join(items)}。"


def capability_text() -> str:
    """
    完整的**能力自述**（渲染进系统提示词）。

    每一句都能追溯到服务端真源 —— 改真源，本段随之改变。
    """
    return "\n".join([
        "## 本工作台的真实能力（由服务端配置动态生成，**这是唯一事实来源**）",
        mode_list_text(),
        aspect_text(),
        video_capability_text(),
        image_capability_text(),
        music_capability_text(),
        asset_rule_text(),
        "- **拿不准就查**：get_mode_schema(mode) 返回该模式的真实合法参数"
        "（取值 + 默认值 + 来源）。不要凭记忆猜档位名或上限。",
    ])


# ------------------------------------------------------------------ 供列表工具用


def modes_summary() -> list[dict]:
    """
    每个模式的**紧凑能力摘要**（给 list_modes 工具返回用）。

    结构紧凑是有意的：工具结果会被裁到 TOOL_RESULT_LIMIT，
    完整明细在 get_mode_schema 里按需查。
    """
    out: list[dict] = []
    for mode in buildable_modes():
        info = gen_types.type_for_mode(mode)
        entry: dict = {
            "mode": mode,
            "label": info.get("label") or mode,
            "media": info.get("media") or "other",
        }
        aspects = agent_schema.mode_aspects(mode)
        if aspects:
            entry["aspects"] = aspects
        duration_range = agent_schema.mode_duration_range(mode)
        if duration_range:
            entry["duration_field"] = agent_schema.mode_duration_field(mode)
            entry["duration_range"] = list(duration_range)
        if agent_schema.is_video_mode(mode):
            forms = video_resolution_forms(mode)
            entry["resolution"] = {
                "tiers": forms["tiers"],
                "max_pixels_mp": forms["max_pixels_mp"],
                "multiple": forms["multiple"],
                "custom_width_height": forms["custom_width_height"],
            }
            entry["models"] = [m["key"] for m in video_models_list()]
            entry["attention"] = [a["key"] for a in video_attention_modes()]
            entry["accel_loras"] = video_accel_loras()
            # 【2026-11-21 —— 不再有 precisions】
            # 精度不是独立参数：它编码在底模权重文件里
            # （int8_convrot / bf16 / pruned_w4a8），选 model_key 就是选精度。
            # 两条视频 builder 都不读 params["precision"] ——
            # 它是「有参数、无消费」的假参数。
            entry["precision_note"] = (
                "精度编码在底模权重里，选 model_key 即选精度；"
                "没有独立的 precision 参数。")
        if agent_schema.is_image_mode(mode):
            entry["models"] = agent_schema.mode_models(mode)
            entry["resolutions"] = agent_schema.mode_resolutions(mode)
        if agent_schema.is_music_mode(mode):
            entry["duration_range"] = list(music_duration_range())
            entry["duration_field"] = agent_schema.mode_duration_field(mode)
            entry["duration_default"] = music_default_duration()
            entry["music_params"] = {
                "max_duration": (f"时长上限（秒），"
                                 f"{music_duration_range()[0]}~"
                                 f"{music_duration_range()[1]}，"
                                 f"默认 {music_default_duration()}"),
                "lyrics": "歌词，留空 = 纯器乐",
                "plan_mode": f"谱面模式：{' / '.join(music_plan_modes())}",
                "format": f"输出格式：{' / '.join(music_formats())}",
            }
        slots = mode_asset_slots(mode)
        if slots["single"] or slots["multi"] or slots["media_slots"]:
            entry["slots"] = ",".join(slots["single"] + slots["multi"])
            if slots["media_slots"]:
                entry["media_slots"] = ",".join(
                    f"{kind}:{slot}"
                    for kind, slot in sorted(slots["media_slots"].items()))
            if slots["max_ref_images"]:
                entry["max_ref_images"] = slots["max_ref_images"]
        out.append(entry)
    return out


def image_family(mode: str) -> str:
    """图像模式 -> 族名（公开入口，供 Agent 工具渲染用）。"""
    return {
        "t2i": "z_image",
        "t2i_qwen": "qwen_image",
        "i2i_qwen": "qwen_image",
        "t2i_moss": "moss_image",
        "t2i_h3image": "h3_image",
        "i2i_h3image": "h3_image",
    }.get(mode, mode)


def image_families() -> dict:
    """
    图像族 -> "模型1,模型2|档位1,档位2"（紧凑表示）。

    【为什么紧凑】工具结果会被裁到 TOOL_RESULT_LIMIT；
    族名 + 清单各占字符，逐模式重复必然超限。
    """
    out: dict = {}
    for mode in buildable_modes():
        if not agent_schema.is_image_mode(mode):
            continue
        family = image_family(mode)
        if family in out:
            continue
        models = agent_schema.mode_models(mode)
        resolutions = agent_schema.mode_resolutions(mode)
        if models or resolutions:
            out[family] = ",".join(models) + "|" + ",".join(resolutions)
    return out
