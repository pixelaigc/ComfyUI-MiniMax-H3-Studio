# -*- coding: utf-8 -*-
"""
分辨率的**统一表达**：「百万像素(MP) 预算 × 百分比」。

【用户 2026-11-20 原话】
    「无论生成什么媒体，都是用百万分辨率 + 百分比模式。
      并自动调整到适合的值上。」

【为什么需要这个模块】
旧代码里「分辨率」有**三套互不相干的表达**：
  1. 视频旧面板（flf / r2v）：档位字符串 ``544p`` / ``768p``
     —— 取值来自 ``workflows.RES_MP = {"544p": 0.4, "768p": 0.8}``，
        这是**旧界面**的两档遗留，被误当成生成侧的能力上限。
  2. 视频新面板（t2v）：``megapixels`` 数值（MP）。
  3. 图片族：档位字符串 ``1mp`` / ``480p`` / ``2k`` / ``4k`` …
     —— 各自一张表（Z_IMAGE_RES_TIERS / QWEN_* / MOSS_* / H3_IMAGE_*），
        MP 值写在表里。

三套表达并存 = 「面板显示什么」与「产物是什么」可能不是一回事
（本项目反复修的「界面说谎」类缺陷）。

【本模块的职责 —— 唯一的换算入口】
    MP 预算 + 百分比 + 比例  ->  (width, height, reasons)

换算链（全部复用**已有的**真实公式，不另造一套）：
  1. MP 预算 = 该模式的**真实面积上限**（``video_models.max_pixels_for_mode``
     / 图片族自己的档位表），来自各 mode 的真实校验器；
  2. 实际 MP = 预算 × 百分比；
  3. 宽高 = ``video_models.video_dimensions(aspect, mp)``
     —— 与 ComfyUI 自带 ResolutionSelector 完全同源的公式，
        宽高天然对齐到 32 的倍数；
  4. 超上限（单边 / 总面积）-> ``video_models.normalize_video_size``
     按比例缩放并吸附 32 倍数，**给出调整原因**。

【调整原因的回传】
所有调整都写进 ``params["size_adjust_reasons"]``（既有机制），
格式统一为「原始请求值 -> 生效值 -> 原因」，与
``h3_video_size.apply_size`` / ``server.normalize_t2v_params`` 一致。

【为什么不重新实现公式】
``video_dimensions`` 已经**逐格复现**官方模板 Note 的十四档
（见 tests/test_video_size_presets.py）。另写一套 = 又一处会漂移的真源。
"""

from __future__ import annotations

import math

from . import video_models as vmodels


#: 百分比字段的合法区间（100% = 该模式的真实面积上限）
PERCENT_MIN = 1.0
PERCENT_MAX = 200.0

#: MP 预算字段的合法区间（单位：MP；上限由各 mode 的真实策略决定）
MP_MIN = 0.01


# ------------------------------------------------------------------
# 模式 -> 真实面积上限（MP）
# ------------------------------------------------------------------

def mode_max_megapixels(mode: str) -> float | None:
    """
    该模式的**真实面积上限**（MP）。None = 不限。

    【视频族】``video_models.max_pixels_for_mode`` —— 由
    ``VIDEO_MODE_SIZE_POLICY`` 派生，t2v / flf / r2v 均为 2.0 MP
    （2026-11-17 放宽 flf/r2v 后三者一致）。
    【图片族】走各自的档位表（最大档的 MP 值）。
    【其它】没有尺寸概念的 mode（音乐等）返回 None。
    """
    key = str(mode or "").strip()
    if key in ("t2v", "flf", "r2v"):
        mp = vmodels.max_pixels_for_mode(key)
        return float(mp) if mp else None

    from . import workflows as wf
    from . import h3image

    tiers = {
        "t2i": getattr(wf, "Z_IMAGE_RES_TIERS", None),
        "t2i_qwen": getattr(wf, "QWEN_IMAGE_RES_TIERS", None),
        "i2i_qwen": getattr(wf, "QWEN_IMAGE_RES_TIERS", None),
        "t2i_moss": getattr(wf, "MOSS_IMAGE_RES_TIERS", None),
        "t2i_h3image": getattr(h3image, "H3_IMAGE_RES_TIERS", None),
        "i2i_h3image": getattr(h3image, "H3_IMAGE_RES_TIERS", None),
    }.get(key)
    if not tiers:
        return None
    values = [float(v) for v in tiers.values() if isinstance(v, (int, float))]
    return max(values) if values else None


def mode_supports_size(mode: str) -> bool:
    """该模式**有没有**尺寸概念（决定 MP+百分比是否适用）。"""
    return mode_max_megapixels(mode) is not None


# ------------------------------------------------------------------
# 档位 <-> MP（图片族用；视频族用短边档位）
# ------------------------------------------------------------------

def tier_to_megapixels(mode: str, tier: str) -> float | None:
    """图片族的档位字符串 -> MP 值（找不到返回 None）。"""
    key = str(mode or "").strip()
    from . import workflows as wf
    from . import h3image

    tiers = {
        "t2i": getattr(wf, "Z_IMAGE_RES_TIERS", None),
        "t2i_qwen": getattr(wf, "QWEN_IMAGE_RES_TIERS", None),
        "i2i_qwen": getattr(wf, "QWEN_IMAGE_RES_TIERS", None),
        "t2i_moss": getattr(wf, "MOSS_IMAGE_RES_TIERS", None),
        "t2i_h3image": getattr(h3image, "H3_IMAGE_RES_TIERS", None),
        "i2i_h3image": getattr(h3image, "H3_IMAGE_RES_TIERS", None),
    }.get(key)
    if not tiers:
        return None
    text = str(tier or "").strip()
    value = tiers.get(text)
    return float(value) if isinstance(value, (int, float)) else None


def video_tier_megapixels(tier: str, aspect: str = "16:9") -> float | None:
    """
    视频族的短边档位（``480p`` …）-> 该比例下的 MP 值。

    【为什么不按比例分别给】短边像素在不同比例下对应不同面积
    （480p：16:9 -> 0.40MP、4:3 -> 0.30MP、21:9 -> 0.50MP）。
    所以必须**按比例反查**，不能用一个全局表。
    """
    text = str(tier or "").strip()
    if text.endswith("p"):
        text = text[:-1]
    if not text.isdigit():
        return None
    short = int(text)
    aspect_key = str(aspect or "16:9").strip()
    if aspect_key not in vmodels.VIDEO_ASPECTS:
        aspect_key = "16:9"
    for preset in vmodels.size_presets_for_aspect(aspect_key):
        if int(preset.get("short_edge") or 0) == short:
            width = int(preset["width"])
            height = int(preset["height"])
            return width * height / (1024 * 1024)
    return None


def legacy_tier_megapixels(tier: str) -> float | None:
    """
    **旧口径**档位 -> MP 值（``workflows.RES_MP``）。

    【为什么需要「旧口径」这一套】
    历史上「768p」有两个**互不相同**的含义：
      * 旧口径（``workflows.RES_MP`` / ``h3_video_size.LEGACY_RESOLUTION_MP``）：
        768p = 0.8 MP -> 16:9 下 **1216×672**；
      * 新口径（短边档位，``size_presets_for_aspect``）：
        768p 短边 = **1344×768**（0.984 MP）。

    存量库里存的是**旧口径**的档位字符串。迁移时必须按旧口径反查 ——
    否则用户已有的默认参数会从 1216×672 悄悄变成 1344×768，
    那是「改了用户配过的东西」。

    【新口径从哪来】旧口径的档位表只有 544p / 768p 两档，
    而短边档位是由 MP 预算反算出来的 —— 两者不是同一族东西。
    """
    from . import workflows as wf
    value = (wf.RES_MP or {}).get(str(tier or "").strip())
    return float(value) if isinstance(value, (int, float)) else None


# ------------------------------------------------------------------
# MP + 百分比 -> 宽高
# ------------------------------------------------------------------

def resolve_megapixels(mode: str,
                       megapixels=None,
                       percent=None,
                       aspect: str = "16:9",
                       resolution: str | None = None,
                       ) -> tuple[float | None, list[str]]:
    """
    把「MP 预算 + 百分比 + 档位」解析成**实际 MP 值**。

    返回 ``(mp, reasons)``；mp 为 None 表示「给不出合法的 MP」。

    优先级（与既有归一层一致，**显式值优先**）：
      1. ``megapixels``（明确的 MP 预算，直接生效）
      2. ``percent``（该模式真实上限的百分比）
      3. ``resolution`` 档位（反查该比例下的 MP）
      4. 都��给 -> None（由调用方决定回落策略）

    【percent 的基准】该模式的**真实面积上限**（``mode_max_megapixels``）。
    100% = 上限；50% = 一半。这让「百分比」在所有 mode 上语义一致。
    """
    reasons: list[str] = []

    if megapixels is not None:
        try:
            mp = float(megapixels)
        except (TypeError, ValueError):
            mp = None
        if mp is not None:
            if mp <= 0:
                reasons.append(f"MP 预算 {mp} 不合法（必须 > 0）-> 已忽略")
            else:
                return mp, reasons

    if percent is not None:
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            pct = None
        if pct is not None:
            cap = mode_max_megapixels(mode)
            if cap is None:
                reasons.append(
                    f"{mode} 没有尺寸概念，百分比 {pct}% 不适用 -> 已忽略")
            else:
                pct = max(PERCENT_MIN, min(PERCENT_MAX, pct))
                mp = cap * pct / 100.0
                reasons.append(
                    f"按 {pct}% 的 MP 预算换算：{cap} MP × {pct}% = {mp:.3f} MP")
                return mp, reasons

    if resolution:
        mp = tier_to_megapixels(mode, resolution)
        if mp is None:
            mp = video_tier_megapixels(resolution, aspect)
        if mp is not None:
            return mp, reasons

    return None, reasons


def plan_size(mode: str,
              aspect: str = "16:9",
              megapixels=None,
              percent=None,
              resolution: str | None = None,
              width: int | None = None,
              height: int | None = None,
              ) -> tuple[int, int, list[str]]:
    """
    「MP 预算 × 百分比」-> (生效宽, 生效高, 调整原因)。

    【调用方约定】返回的 reasons 应当写进
    ``params["size_adjust_reasons"]``（既有机制），让「原始请求值 ->
    生效值 -> 原因」一路带到用户面前。

    【显式宽高优先】已经给了 width/height 就**直接采用**（只做上限检查
    与 32 对齐），不拿 MP 预算去覆盖它 —— 那是用户手填的实参。
    """
    reasons: list[str] = []
    aspect_key = str(aspect or "16:9").strip()
    if aspect_key not in vmodels.VIDEO_ASPECTS:
        aspect_key = "16:9"

    def _as_positive(value):
        try:
            out = int(float(value))
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    explicit_w = _as_positive(width)
    explicit_h = _as_positive(height)

    if explicit_w and explicit_h:
        # 已有真实宽高 -> 过一遍该模式的上限 + **32 对齐**
        #
        # 【为什么这里也要 32 对齐】H3 的 CANVAS_MULTIPLE = 32、
        # 节点 schema step = 32、latent 网格 = width//16 ——
        # 非 32 倍数的宽高会得到不对齐的 latent 网格。
        # 而 ``h3_video_size.resolve_size`` 也做同样的事 ——
        # 两处必须一致，否则「Agent 路径」与「面板路径」会算出不同的宽高。
        cap = mode_max_megapixels(mode)
        if cap:
            limit = cap * 1024 * 1024
            if explicit_w * explicit_h > limit:
                snapped_w, snapped_h, snap_reasons = vmodels.normalize_video_size(
                    explicit_w, explicit_h, max_pixels=cap,
                    strict_over_limit=False, mode=mode)
                if snapped_w > 0 and snapped_h > 0:
                    reasons.append(
                        f"请求尺寸 {explicit_w}×{explicit_h} 超过 {mode} 模式上限 "
                        f"{cap}MP -> 已缩到 {snapped_w}×{snapped_h}")
                    reasons.extend(snap_reasons)
                    explicit_w, explicit_h = snapped_w, snapped_h
                else:
                    reasons.extend(snap_reasons)
        multiple = vmodels.VIDEO_SIZE_MULTIPLE
        aligned_w = max(multiple,
                       int(round(explicit_w / multiple)) * multiple)
        aligned_h = max(multiple,
                       int(round(explicit_h / multiple)) * multiple)
        if aligned_w != explicit_w:
            reasons.append(
                f"宽度 {explicit_w} 不是 {multiple} 的倍数 -> 已吸附到 {aligned_w}")
            explicit_w = aligned_w
        if aligned_h != explicit_h:
            reasons.append(
                f"高度 {explicit_h} 不是 {multiple} 的倍数 -> 已吸附到 {aligned_h}")
            explicit_h = aligned_h
        if explicit_w > vmodels.VIDEO_SIZE_MAX:
            reasons.append(
                f"宽度 {explicit_w} 超过上限 {vmodels.VIDEO_SIZE_MAX} "
                f"-> 已钳到上限")
            explicit_w = vmodels.VIDEO_SIZE_MAX
        if explicit_h > vmodels.VIDEO_SIZE_MAX:
            reasons.append(
                f"高度 {explicit_h} 超过上限 {vmodels.VIDEO_SIZE_MAX} "
                f"-> 已钳到上限")
            explicit_h = vmodels.VIDEO_SIZE_MAX
        return explicit_w, explicit_h, reasons

    mp, mp_reasons = resolve_megapixels(
        mode, megapixels=megapixels, percent=percent,
        aspect=aspect_key, resolution=resolution)
    reasons.extend(mp_reasons)

    if mp is None:
        return 0, 0, reasons

    # 按比例 + MP 算宽高（video_dimensions 天然对齐 32 的倍数）
    calc_w, calc_h = vmodels.video_dimensions(aspect_key, mp)
    reasons.append(
        f"{mode} 尺寸：MP 预算 {mp:.3f} MP + 比例 {aspect_key} "
        f"-> {calc_w}×{calc_h}")

    # 再过一次该模式的上限（面积 / 单边）
    cap = mode_max_megapixels(mode)
    if cap:
        limit = cap * 1024 * 1024
        if calc_w * calc_h > limit:
            snapped_w, snapped_h, snap_reasons = vmodels.normalize_video_size(
                calc_w, calc_h, max_pixels=cap,
                strict_over_limit=False, mode=mode)
            if snapped_w > 0 and snapped_h > 0:
                reasons.append(
                    f"总像素 {calc_w * calc_h / (1024 * 1024):.3f} MP 超过 "
                    f"{mode} 上限 {cap} MP -> 已缩到 {snapped_w}×{snapped_h}")
                reasons.extend(snap_reasons)
                return snapped_w, snapped_h, reasons
            reasons.extend(snap_reasons)

    return calc_w, calc_h, reasons


def apply_size_spec(params: dict, mode: str) -> dict:
    """
    就地把「MP 预算 × 百分比」解析结果写进 params。

    写回：``width`` / ``height`` / ``size_adjust_reasons``。
    **不删** ``resolution`` —— 那是用户选的原意（面板档位标签），
    卡片显示按实际像素反算，不会撒谎。

    【为什么写回而不是只返回】两条建任务路径（面板提交 / Agent 自建）
    都要把生效宽高带进工作流；写进 params 才能保证**两边一致**。
    """
    if not isinstance(params, dict):
        return params
    reasons: list[str] = []

    width, height, notes = plan_size(
        mode,
        aspect=params.get("aspect") or "16:9",
        megapixels=params.get("megapixels"),
        percent=params.get("resolution_percent"),
        resolution=params.get("resolution"),
        width=params.get("width"),
        height=params.get("height"),
    )
    reasons.extend(notes)

    if width > 0 and height > 0:
        params["width"] = width
        params["height"] = height

    if reasons:
        existing = params.get("size_adjust_reasons")
        merged = list(existing) if isinstance(existing, list) else []
        for note in reasons:
            if note not in merged:
                merged.append(note)
        params["size_adjust_reasons"] = merged
    return params


# ------------------------------------------------------------------
# 默认值与规格（给管理页 + Agent 能力自述用）
# ------------------------------------------------------------------

def default_megapixels(mode: str) -> float | None:
    """该模式的**默认 MP 预算**（= 真实上限的 50%，即「安全默认」）。"""
    cap = mode_max_megapixels(mode)
    if cap is None:
        return None
    return round(cap * 0.5, 3)


def default_percent() -> float:
    """默认百分比（100% = 该模式真实上限）。"""
    return 100.0


def size_spec_for_mode(mode: str) -> dict:
    """
    该模式的尺寸规格（供管理页字段表与 Agent 能力自述共用）。

    字段：
      * ``megapixels`` —— MP 预算（数值，单位 MP）
      * ``resolution_percent`` —— 百分比（数值，1~200%）
      * ``max_megapixels`` —— 该模式的真实上限
      * ``multiple`` / ``min_edge`` / ``max_edge``
      * ``presets`` —— 该模式下**真实可用的短边档位**（按比例）
    """
    cap = mode_max_megapixels(mode)
    policy = (vmodels.size_policy_for_mode(mode)
              if str(mode or "") in ("t2v", "flf", "r2v") else {})
    presets = []
    if str(mode or "") in ("t2v", "flf", "r2v"):
        for aspect in vmodels.VIDEO_ASPECTS:
            for preset in vmodels.size_presets_for_aspect(aspect):
                presets.append({
                    "aspect": aspect,
                    "short_edge": preset.get("short_edge"),
                    "label": preset.get("label"),
                    "width": preset.get("width"),
                    "height": preset.get("height"),
                    "megapixels": preset.get("megapixels"),
                })
    return {
        "mode": mode,
        "supports_size": cap is not None,
        "max_megapixels": cap,
        "default_megapixels": default_megapixels(mode),
        "default_percent": default_percent(),
        "percent_min": PERCENT_MIN,
        "percent_max": PERCENT_MAX,
        "multiple": policy.get("multiple", vmodels.VIDEO_SIZE_MULTIPLE),
        "min_edge": policy.get("min", vmodels.VIDEO_SIZE_MIN),
        "max_edge": policy.get("max", vmodels.VIDEO_SIZE_MAX),
        "custom_size": bool(policy.get("custom_size")),
        "presets": presets,
        "source": "video_models.VIDEO_MODE_SIZE_POLICY / 各 mode 的真实档位表",
    }
