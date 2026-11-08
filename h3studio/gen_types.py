# -*- coding: utf-8 -*-
"""生成类型注册表 —— 右侧任务标签「按生成类型」的唯一真源。

设计原则：**类型来自后端真实注册的模式，不在前端硬编码。**
新增一个生成模式时，只需在 `h3studio/workflows.py` 的 `BUILDERS` 里注册，
这里自动多出一个类型标签；前端从 `GET /api/generation-types` 拿清单。

老任务（建库之前创建的）没有显式的 type 字段 —— 统一按 mode 推导，
保证「不会出现空白或全部归到未知」。
"""

from __future__ import annotations

# mode -> 类型元数据
# media：媒体类型（image / video / audio）—— 与 enhancer.MODE_MEDIA 语义一致
# label：类型标签显示名
# group：同组标签互斥（一组里只能选一个）
GENERATION_TYPES: dict[str, dict] = {
    "t2i": {"label": "Z-IMAGE 文生图", "media": "image", "group": "generation_type"},
    "t2v": {"label": "文生视频", "media": "video", "group": "generation_type"},
    "flf": {"label": "首尾帧生成", "media": "video", "group": "generation_type"},
    "r2v": {"label": "参考图生成", "media": "video", "group": "generation_type"},
    "longfilm": {"label": "长片拼接", "media": "video", "group": "generation_type"},
}

# 兜底：mode 未登记时的归类（保证不出现空白 / 全部归到未知）
FALLBACK_TYPE = {
    "label": "其它生成",
    "media": "other",
    "group": "generation_type",
}

# 历史上出现过的、已不在当前注册表里的 mode -> 旧名（保留可追溯的显示名）
LEGACY_MODE_LABELS = {
    "audio": "音频生成",
    "bgm": "音频生成",
    "t2a": "音频生成",
    "i2v": "图生视频",
    "unknown": "未知类型",
}


def registered_modes() -> list[str]:
    """当前后端真实注册的模式（来自 workflows.BUILDERS + longfilm）。

    BUILDERS 是 ComfyUI 工作流构建器的唯一注册点 —— 它注册了什么，
    这里就认什么，不会出现「前端有标签但后端没实现」的情况。
    """
    modes: list[str] = []
    try:
        from . import workflows
        modes.extend(workflows.BUILDERS.keys())
    except Exception:
        pass
    # longfilm 不走 BUILDERS（它自己起子进程拼片），但确实是后端支持的生成类型
    if "longfilm" not in modes:
        modes.append("longfilm")
    return modes


def type_for_mode(mode: str) -> dict:
    """mode -> 类型元数据。未登记的 mode 走一致兜底，不返回空。"""
    mode = (mode or "").strip()
    if mode in GENERATION_TYPES:
        return dict(GENERATION_TYPES[mode], key=mode)
    if mode in LEGACY_MODE_LABELS:
        return {"key": mode, "label": LEGACY_MODE_LABELS[mode],
                "media": "other", "group": "generation_type", "legacy": True}
    # 完全未知的 mode：归到「其它生成」，label 里带上原 mode 便于排查
    return {"key": mode or "unknown", "label": FALLBACK_TYPE["label"],
            "media": FALLBACK_TYPE["media"], "group": FALLBACK_TYPE["group"],
            "unknown": True}


def type_key_for_mode(mode: str) -> str:
    """mode -> 类型 key（用于过滤）。"""
    return type_for_mode(mode)["key"]


def list_types() -> list[dict]:
    """前端类型标签清单（按注册顺序 + 兜底项补在最后）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for mode in registered_modes():
        if mode in seen:
            continue
        seen.add(mode)
        info = type_for_mode(mode)
        out.append({
            "key": info["key"],
            "label": info["label"],
            "media": info["media"],
            "group": info["group"],
        })
    # 兜底项：始终提供，保证老任务/未知 mode 有归类可显示
    out.append({
        "key": "__other__",
        "label": FALLBACK_TYPE["label"],
        "media": FALLBACK_TYPE["media"],
        "group": FALLBACK_TYPE["group"],
        "fallback": True,
    })
    return out


def type_filter_keys() -> dict[str, list[str]]:
    """类型 key -> 对应的 mode 列表（供 SQL IN 过滤）。"""
    mapping: dict[str, list[str]] = {}
    for mode in registered_modes():
        mapping.setdefault(type_key_for_mode(mode), []).append(mode)
    # 未登记的历史 mode 归到兜底
    for mode in LEGACY_MODE_LABELS:
        mapping.setdefault(mode, []).append(mode)
    return mapping
