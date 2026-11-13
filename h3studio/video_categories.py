# -*- coding: utf-8 -*-
"""视频类别 → 模型 的两级注册表 —— **唯一真源**。

【为什么要单独一个模块】
左侧导航要改成两级：先选「视频类别」，再选该类别下的具体模型。
需求明确要求：

  * 第一级「视频类别」固定四项，顺序与文案不可变：
        1) 文生视频
        2) 首尾帧生视频
        3) 参考生视频
        4) 长片拼接
  * 第二级是每个类别下的具体模型项。
  * 「模型与类别的归属关系要数据驱动，不能把模型名硬编码进类别标签文案」。

所以这里定义「**类别 key → 该类别下的模型项列表**」的映射，
每个模型项声明自己属于哪个类别、叫什么、背后是哪个 mode。

【单一真源】
前端与服务端对 mode/model 的映射**只允许存在这一处**。
前端从 `GET /api/video-categories` 拿清单，不在 JS 里再写一份表
（两处各维护一份映射表是本项目反复踩过的坑）。

【向后兼容】
老 URL / 老参数 / 老任务记录里的 mode（t2v / flf / r2v / longfilm）
**一个字都没改** —— 类别只是给这些 mode 套了一层「分组视图」。
`resolve_mode()` 接受老 mode，也接受「类别 key」，
保证旧的链接与任务卡片还能定位到对应面板。
"""

from __future__ import annotations

# ------------------------------------------------------------------
# 类别定义（顺序 = 需求规定的固定顺序，不可改）
# ------------------------------------------------------------------
#: 视频类别的 key -> 显示名。**顺序即导航里的显示顺序**。
VIDEO_CATEGORY_LABELS: dict[str, str] = {
    "t2v": "文生视频",
    "flf": "首尾帧生视频",
    "r2v": "参考生视频",
    "longfilm": "长片拼接",
}

#: 需求规定的固定顺序（显式列出来，便于测试直接断言这个序列）。
VIDEO_CATEGORY_ORDER: list[str] = ["t2v", "flf", "r2v", "longfilm"]

# ------------------------------------------------------------------
# 类别 → 模型项
# ------------------------------------------------------------------
#: 类别 key -> 该类别下的模型项列表（有序）。
#:
#: 每个模型项：
#:   key        模型项的稳定标识（存配置用）
#:   label      显示名（模型自己的名字，不是类别名）
#:   mode       背后真实的生成模式（提交任务时用它）
#:   media      媒体类型（video / audio …）
#:   available  是否可用（本机权重在不在 / 该模式是否已实现）
#:   note       补充说明（可选）
#:
#: 【关键】类别标签文案里**不出现任何模型名** ——
#: 类别名来自 VIDEO_CATEGORY_LABELS，模型名来自这里，两者是正交的。
#: 以后往某个类别下加新模型，只改这张表，前端自动多出一项。
VIDEO_CATEGORY_MODELS: dict[str, list[dict]] = {
    "t2v": [
        {
            "key": "h3_t2v",
            "label": "minimax-h3 文生视频",
            "mode": "t2v",
            "media": "video",
            "note": "MiniMax-H3 文生视频（新面板：宽高直填 / 模型可选 / "
                    "加速 LoRA / 注意力机制 / 二采）。",
        },
    ],
    "flf": [
        {
            "key": "h3_flf",
            "label": "minimax-h3 首尾帧生视频",
            "mode": "flf",
            "media": "video",
            "note": "MiniMax-H3 首尾帧生视频：给首帧 + 尾帧，模型补中间。",
        },
    ],
    "r2v": [
        {
            "key": "h3_r2v",
            "label": "minimax-h3 参考生视频",
            "mode": "r2v",
            "media": "video",
            "note": "MiniMax-H3 参考生视频：给参考图 / 参考视频 / 参考音频。",
        },
    ],
    "longfilm": [
        {
            "key": "h3_longfilm",
            "label": "minimax-h3 长片拼接",
            "mode": "longfilm",
            "media": "video",
            "note": "把 ≥2 个已完成的视频任务拼成一支长片。",
        },
    ],
}

#: 老的扁平 mode -> 类别 key（向后兼容的桥）。
#:
#: 老客户端 / 老 URL / 老任务记录里的 mode 都能通过它落到某个类别下。
MODE_TO_CATEGORY: dict[str, str] = {
    item["mode"]: category
    for category, items in VIDEO_CATEGORY_MODELS.items()
    for item in items
}

#: 老 mode -> 显示名（保留可追溯的旧称谓，用于历史任务卡片回显）。
LEGACY_MODE_LABELS = {
    "t2v": "文生视频",
    "flf": "首尾帧生成",
    "r2v": "参考图生成",
    "longfilm": "长片拼接",
}


def category_for_mode(mode: str) -> str:
    """mode -> 类别 key。未知 mode 返回空串（调用方自己决定兜底）。"""
    return MODE_TO_CATEGORY.get((mode or "").strip(), "")


def models_for_category(category: str) -> list[dict]:
    """某个类别下的模型项列表（有序）。未知类别返回空列表。"""
    return [dict(item) for item in VIDEO_CATEGORY_MODELS.get(category, [])]


def resolve_mode(category: str, model_key: str | None = None) -> str:
    """(类别, 模型项 key) -> 真实 mode。

    **model_key 为空时**取该类别下的**第一个**可用模型项的 mode
    —— 这是「切换类别时的模型回退规则」的最后一环。

    找不到就回落到该类别的首个模型项；类别本身不存在则返回空串。
    """
    items = VIDEO_CATEGORY_MODELS.get(category or "")
    if not items:
        return ""
    if model_key:
        for item in items:
            if item.get("key") == model_key:
                return item.get("mode", "")
    return items[0].get("mode", "")


def list_categories() -> list[dict]:
    """前端导航要的两级清单（顺序 = 需求的固定四项）。

    每项：
        key          类别 key
        label        类别显示名（固定四项文案）
        media        媒体类型
        models       该类别下的模型项列表
        default_model 该类别默认选中的模型项 key
        modes        该类别覆盖的 mode 列表（供前端反查）
    """
    out: list[dict] = []
    for category in VIDEO_CATEGORY_ORDER:
        items = VIDEO_CATEGORY_MODELS.get(category, [])
        out.append({
            "key": category,
            "label": VIDEO_CATEGORY_LABELS.get(category, category),
            "media": "video",
            "models": [dict(item) for item in items],
            "default_model": items[0].get("key", "") if items else "",
            "modes": [item.get("mode", "") for item in items],
        })
    return out


def list_models() -> list[dict]:
    """扁平的全部模型项（带所属类别），便于前端建索引。"""
    out: list[dict] = []
    for category in VIDEO_CATEGORY_ORDER:
        for item in VIDEO_CATEGORY_MODELS.get(category, []):
            entry = dict(item)
            entry["category"] = category
            entry["category_label"] = VIDEO_CATEGORY_LABELS.get(category, category)
            out.append(entry)
    return out
