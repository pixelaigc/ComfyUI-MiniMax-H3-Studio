# -*- coding: utf-8 -*-
"""
提示词增强迷你 Agent（独立模块 / 独立服务边界）。

设计目标（用户需求原文）：
  "增强能力要独立设计成一个迷你 Agent（独立模块/独立服务边界，具备自己的
   提示词模板、参数与调用封装），以便后续复用到视频/音频提示词。"

所以本模块的边界是：
  - 自己管模板（可编辑、可持久化）
  - 自己管参数（temperature / max_tokens / 语言 / 长度档位）
  - 自己封装调用（不关心 Provider 细节，只认 provider 字典）
  - 自己管输出（原文保留 / 替换 / 追加 三种落地策略）

复用方式：新增 "video" 或 "audio" 模板即可，无需改动本模块。
每个模板是一个独立的 PromptSpec，自带 system prompt + 维度清单 + 输出格式要求。
"""
import json

# ------------------------------------------------------------------ 模板

# 图像提示词增强模板 —— 参考社区成熟的图像提示词扩写实践：
#   主体(subject) / 场景(environment) / 构图(composition) / 光线(lighting)
#   / 风格(style) / 镜头(camera+lens) 六个维度
# 关键约束（社区实践共识）：
#   1. 主体放最前（模型对开头最敏感）
#   2. 一个主风格 + 一个质感参考，不要堆砌互相冲突的风格
#   3. 用具体可视元素代替"高级感/氛围感"这类评价词
#   4. 词数控制在 40-120 词（Z-IMAGE 吃自然语言整段，不吃标签堆）
IMAGE_SPEC = {
    "id": "image",
    "label": "图像提示词（Z-IMAGE）",
    "media": "image",
    "system_prompt": (
        "你是一位专业的 AI 图像提示词工程师，"
        "专精为 Z-IMAGE / Flux / SDXL 一类的扩散模型撰写结构化图像提示词。\n"
        "你的任务是把用户的一句粗略描述扩写成一段可直接投喂给图像模型的完整提示词。\n"
        "\n"
        "扩写必须覆盖以下六个维度，每一类都要落到具体可视的元素上：\n"
        "1. 主体（subject）：谁/什么、外观特征、服装、表情、动作、姿态。主体放最前面。\n"
        "2. 场景（environment）：地点、时间、天气、周边物件、空间纵深。\n"
        "3. 构图（composition）：景别（特写/半身/全身/远景）、机位角度、主体在画面中的位置、留白。\n"
        "4. 光线（lighting）：光源方向、软硬、色温、明暗关系、氛围。\n"
        "5. 风格（style）：一个主风格 + 一个质感参考（如胶片摄影 + 柯达 Portra 色调）。\n"
        "6. 镜头（camera）：焦段（35mm/85mm/微距/广角）、光圈与景深。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出扩写后的提示词本身，不要任何解释、前言、标题、编号或 markdown 标记。\n"
        "- 不要堆砌互相冲突的风格（如同时写写实+二次元+油画），一个主风格 + 一个质感参考。\n"
        "- 用具体可视元素替代主观评价词；不要写「高级感」「氛围感」这类无信息的词。\n"
        "- 保留用户原文里已经写明的意图和限定条件，不要替换掉它们。\n"
        "- 输出语言与用户原文一致（用户写中文就输出中文）。\n"
        "- 长度控制在 40-120 词的中文描述（或等量英文），单段连贯自然语言，"
        "不要用逗号标签堆砌。\n"
        "- 不要输出负面提示词，负面词由用户单独维护。"
    ),
    "user_template": "请把下面这句描述扩写成一段完整的图像生成提示词：\n\n{original}",
    "dimensions": ["主体", "场景", "构图", "光线", "风格", "镜头"],
}

# 视频提示词增强模板 —— 预留复用位（复用同一套 Agent 边界）
VIDEO_SPEC = {
    "id": "video",
    "label": "视频提示词（MiniMax-H3）",
    "media": "video",
    "system_prompt": (
        "你是一位专业的 AI 视频提示词工程师，专精为 MiniMax-H3 等视频生成模型撰写提示词。\n"
        "请把用户的粗略描述扩写成包含镜头、动作、环境与声音的完整视频提示词。\n"
        "\n"
        "扩写必须覆盖：\n"
        "1. 主体：外观、服装、动作、情绪。\n"
        "2. 环境：地点、时间、天气、周边元素。\n"
        "3. 镜头与运镜：景别、机位、运动方式（推/拉/摇/移/跟）、运动幅度。\n"
        "4. 光线与氛围：光源方向、色温、明暗。\n"
        "5. 风格：一个主风格 + 一个质感参考。\n"
        "6. 声音（可选）：环境音与配乐描述。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出扩写后的提示词本身，不要解释、前言、编号或 markdown 标记。\n"
        "- 不要堆砌互相冲突的风格，一个主风格 + 一个质感参考。\n"
        "- 保留用户原文里已写明的意图。\n"
        "- 输出语言与用户原文一致。\n"
        "- 控制在 60-160 词的中文描述。"
    ),
    "user_template": "请把下面这句描述扩写成一段完整的视频生成提示词：\n\n{original}",
    "dimensions": ["主体", "环境", "运镜", "光线", "风格", "声音"],
}

# 音频提示词增强模板 —— 预留复用位
AUDIO_SPEC = {
    "id": "audio",
    "label": "音频提示词（BGM / 音效）",
    "media": "audio",
    "system_prompt": (
        "你是一位专业的 AI 音乐与音效提示词工程师。\n"
        "请把用户的粗略描述扩写成一段可用于音乐/音效生成的完整提示词。\n"
        "\n"
        "扩写必须覆盖：体裁、节奏、乐器、音色情绪、动态变化、混音质感、参考风格。\n"
        "\n"
        "硬性规则：\n"
        "- 只输出扩写后的提示词本身，不要解释、前言、编号或 markdown 标记。\n"
        "- 输出语言与用户原文一致。\n"
        "- 控制在 30-80 词。"
    ),
    "user_template": "请把下面这句描述扩写成一段完整的音频生成提示词：\n\n{original}",
    "dimensions": ["体裁", "节奏", "乐器", "音色情绪", "动态", "混音质感"],
}

# 内置模板表（可被用户覆盖 / 编辑）
BUILTIN_SPECS = {
    "image": IMAGE_SPEC,
    "video": VIDEO_SPEC,
    "audio": AUDIO_SPEC,
}

# ------------------------------------------------------------------ 参数


# 长度档位 -> max_tokens
LENGTH_PRESETS = {
    "short": {"label": "精简", "max_tokens": 320, "hint": "约 30-60 词"},
    "medium": {"label": "标准", "max_tokens": 768, "hint": "约 60-120 词"},
    "long": {"label": "详尽", "max_tokens": 1536, "hint": "约 120-200 词"},
}

# 默认参数
DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 0.95,
    "max_tokens": 768,
    "length": "medium",
    "language": "auto",
}


def list_specs() -> list:
    """列出全部可用模板（内置 + 用户自定义），供前端展示。"""
    specs = []
    for key, spec in BUILTIN_SPECS.items():
        specs.append({
            "id": spec["id"],
            "label": spec["label"],
            "media": spec["media"],
            "builtin": True,
            "dimensions": spec["dimensions"],
        })
    return specs


def get_spec(spec_id: str, overrides: dict | None = None) -> dict:
    """
    取模板。overrides 来自用户持久化的自定义模板（可编辑 system_prompt 等）。
    找不到就回落到 image 模板 —— 保证任何调用都有模板可用。
    """
    overrides = overrides or {}
    custom = overrides.get(spec_id)
    base = BUILTIN_SPECS.get(spec_id) or BUILTIN_SPECS["image"]
    if not isinstance(custom, dict):
        return dict(base)
    merged = dict(base)
    for key in ("system_prompt", "user_template", "label", "dimensions"):
        if isinstance(custom.get(key), str) and custom[key].strip():
            merged[key] = custom[key]
    merged["builtin"] = False
    return merged


def resolve_params(params: dict | None, overrides: dict | None = None) -> dict:
    """
    合并参数：内置默认 <- 用户保存的覆盖 <- 本次请求传入。
    模板 id 存在 overrides["params"] 里。
    """
    params = params or {}
    overrides = overrides or {}
    saved = overrides.get("params") or {}
    merged = dict(DEFAULT_PARAMS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_PARAMS})
    merged.update({k: v for k, v in params.items() if k in DEFAULT_PARAMS})
    # 长度档位优先于裸 max_tokens
    length = merged.get("length", "medium")
    if length in LENGTH_PRESETS:
        merged["max_tokens"] = LENGTH_PRESETS[length]["max_tokens"]
    else:
        merged["max_tokens"] = int(merged.get("max_tokens") or 768)
    merged["temperature"] = float(merged.get("temperature", 0.7))
    merged["top_p"] = float(merged.get("top_p", 0.95))
    return merged


def build_messages(spec: dict, original: str, language: str = "auto") -> list:
    """构造统一消息格式（system + user）。"""
    original = (original or "").strip()
    user_text = spec["user_template"].replace("{original}", original)
    if language and language != "auto":
        user_text += f"\n\n请用{language}输出。"
    return [
        {"role": "system", "content": spec["system_prompt"]},
        {"role": "user", "content": user_text},
    ]


def looks_like_echo(output: str, system_prompt: str) -> bool:
    """
    检测模型是不是把 system prompt 原样吐了回来。

    小参数量模型（135M 之类）经常不干活，直接复读系统提示词。
    这种输出必须当成失败处理 —— 否则用户会拿到一段系统提示词冒充提示词。
    判据：输出与系统提示词存在长度 >= 40 字符的公共子块。
    """
    if not output or not system_prompt:
        return False
    import difflib
    matcher = difflib.SequenceMatcher(None, system_prompt, output, autojunk=False)
    block = matcher.find_longest_match(0, len(system_prompt), 0, len(output))
    return block.size >= 40


def strip_wrapping(text: str) -> str:
    """
    清理模型可能的包装：代码块围栏、引号、以及「提示词：」这类前缀。
    只做保守剥离，不动正文内容。
    """
    if not text:
        return ""
    result = text.strip()
    # 去掉 ```lang ... ``` 围栏
    if result.startswith("```"):
        lines = result.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        result = "\n".join(lines).strip()
    # 去掉常见前缀
    for prefix in ("扩写后的提示词：", "扩写结果：", "提示词：", "Prompt:", "prompt:"):
        if result.startswith(prefix):
            result = result[len(prefix):].strip()
    # 去掉成对包裹引号
    if len(result) >= 2 and result[0] in "\"'“" and result[-1] in "\"'”":
        result = result[1:-1].strip()
    return result


def merge_result(original: str, enhanced: str, mode: str) -> str:
    """
    落地策略：
      replace —— 用扩写结果替换原提示词
      append  —— 保留原文，扩写结果追加在后面（原文不被覆盖）
    """
    original = (original or "").strip()
    enhanced = (enhanced or "").strip()
    if mode == "append":
        if not original:
            return enhanced
        if not enhanced:
            return original
        return f"{original}\n\n{enhanced}"
    return enhanced or original
