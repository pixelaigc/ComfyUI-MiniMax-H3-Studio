# -*- coding: utf-8 -*-
"""
提示词增强迷你 Agent（独立模块 / 独立服务边界）。

设计目标（用户需求原文）：
  "增强能力要独立设计成一个迷你 Agent（独立模块/独立服务边界，具备自己的
   提示词模板、参数与调用封装），以便后续复用到视频/音频提示词。"

模块边界：
  - 自己管模板（Skill 目录 assets/ 下的模板文件，用户可覆盖并持久化）
  - 自己管参数（采样档 / 长度档 / 语言）
  - 自己封装调用（只认 provider 字典，不关心具体协议）
  - 自己管校验（scripts/validate.py 的规则）
  - 自己管输出（replace / append 两种落地策略）

与 Provider 配置解耦：
  增强模块不持有任何 Provider 配置，一律通过调用方传入的 provider 字典工作。
  provider 字典来自「本地模型配置」页里设定的默认多模态 Provider，
  该配置持久化在服务端 SQLite，供局域网全部客户端共用。

Skill 目录（遵循 Agent Skills 规范的渐进式披露）：
  h3studio/skills/prompt-enhancer/
    SKILL.md              导航层（<500 行）
    references/           约束与字段定义（按需加载）
    assets/               模板与示例（按需加载）
    scripts/validate.py   校验器（只跑不读）
"""
import json
import re
from pathlib import Path

# Skill 根目录
SKILL_DIR = Path(__file__).resolve().parent / "skills" / "prompt-enhancer"
ASSETS_DIR = SKILL_DIR / "assets"
SCRIPTS_DIR = SKILL_DIR / "scripts"

# ------------------------------------------------------------------ 模板档位

# 每档 = 一个模板文件 + 校验模式 + 长度档 + 适用模型族
#
# 过滤机制（通用，不硬编码删选项）：
#   两个维度匹配 —— media（媒体类型）+ families（模型族）
#   - media：image / video / audio / 3d ... 决定模板属于哪类媒体
#   - families：该模板适配哪些模型族；含 "*" 表示该 media 下通用
#   - 上下文（当前模式 + 当前模型）解出 media 与 family，模板必须同时匹配
#   新增模型或新增模板只需改这两张表，不用动过滤代码。
#
# 为什么按「模型族」而不是按「模式」过滤：
#   同一模式下可能有多个模型（如 Z-Image Turbo / Base / DarkBeast），
#   将来加入 FLUX 等其它图像模型时，只有 Z-Image 专属模板才需要被排除。
TEMPLATE_SPECS = {
    "image": {
        "id": "image",
        "label": "图像提示词（Z-Image 官方 PE）",
        "media": "image",
        "families": ["z_image"],
        "template": "pe-zimage-official.txt",
        "validate_mode": "pe",
        "default_length": "medium",
        "builtin": True,
        "dimensions": ["主体", "数量", "动作", "状态", "专有名词/颜色/文字",
                        "生成式推理", "构图", "光影氛围", "材质质感", "色彩方案",
                        "层次空间", "文字排版"],
        "source": "Z-Image 官方 HF Space pe.py（实测 2371 bytes）",
    },
    # 二次增强（细化）：把提示词里的物体逐个枚举 + 逐个精细化。
    # 可作用于「原始提示词」或「一次增强后的提示词」。
    "image-refine": {
        "id": "image-refine",
        "label": "图像提示词·物体细化（二次增强）",
        "media": "image",
        "families": ["*"],
        "template": "refine-objects.txt",
        "validate_mode": "refine",
        # 逐物体枚举需要更长预算
        "default_length": "long",
        "builtin": True,
        "dimensions": ["L1 特写", "L2 中景", "L3 远景", "环境现象", "主体细化"],
        "source": "本机 skill: scene-object-inventory（物体逐个枚举 + L1/L2/L3 分级）",
        # 标记为「二次增强」档：前端据此区分一次/二次增强
        "refine": True,
    },
    "image-scalist": {
        "id": "image-scalist",
        "label": "图像提示词（SCALIST 结构化）",
        "media": "image",
        "families": ["*"],
        "template": "scalist-structured.txt",
        "validate_mode": "scalist",
        "default_length": "medium",
        "builtin": True,
        "dimensions": ["subject", "action", "location", "composition",
                        "lighting", "style"],
        "source": "HiDream-O1 官方 SCALIST 字段划分",
    },
    "image-bbox": {
        "id": "image-bbox",
        "label": "图像提示词 + bbox 元素表（FLUX 3 官方格式）",
        "media": "image",
        "families": ["*"],
        "template": "bbox-elements.txt",
        "validate_mode": "bbox",
        # caption + JSON 元素表比纯 caption 长，默认给 long 档预算
        "default_length": "long",
        "builtin": True,
        "dimensions": ["caption", "id", "bbox", "desc"],
        "source": "BFL FLUX 3 官方 t2i_bbox system prompt",
    },
    # Qwen-Image 2.1 官方 PE 两档（文生图 / 图片编辑）。
    # 正文取自 ComfyUI 官方工作流模板里的官方 PE system prompt：
    #   image_qwen_image_2_1_t2i.json       -> assets/qwen21-pe-t2i.txt
    #   image_qwen_image_2_1_image_edit.json -> assets/qwen21-pe-i2i.txt
    # 两者是官方给两种场景各配的 system prompt，**不可互换**：
    # PE-T2I 走「观察者式结构化描述」（含比例决策），
    # PE-I2I 走「属性解耦 + 保真锁定」的编辑指令写法（<imageN> 引用）。
    "qwen21-pe-t2i": {
        "id": "qwen21-pe-t2i",
        "label": "Qwen-Image 2.1 官方 PE·文生图（结构化描述）",
        "media": "image",
        "families": ["qwen_image21"],
        "template": "qwen21-pe-t2i.txt",
        # 官方 PE 的 system prompt 自己定义了「用户请求」的角色，
        # user 消息直接放原始提示词，不加「用户输入 prompt:」前缀
        "user_template": "{prompt}",
        "validate_mode": "pe",
        "default_length": "long",
        "builtin": True,
        "dimensions": ["开头句", "背景面", "顶带", "主体区", "底带",
                        "方位短语", "文字块", "光照", "收尾"],
        "source": "官方 Qwen-Image-2.1-PE-T2I system prompt（ComfyUI 官方模板）",
    },
    "qwen21-pe-i2i": {
        "id": "qwen21-pe-i2i",
        "label": "Qwen-Image 2.1 官方 PE·图片编辑（修改X保持Y）",
        "media": "image",
        "families": ["qwen_image21"],
        "template": "qwen21-pe-i2i.txt",
        "user_template": "{prompt}",
        "validate_mode": "pe",
        "default_length": "long",
        "builtin": True,
        "dimensions": ["操作动词", "保持不变", "不要顺手改", "外观细节",
                        "图内文字", "多图 <imageN> 引用"],
        "source": "官方 Qwen-Image-2.1-PE-I2I system prompt（ComfyUI 官方模板）",
    },
    # MOSS-Image：官方 ComfyUI-Moss 插件内置的 MOSS Skill（defaults/SKILL.md）
    # 规则改写成「只出正向提示词」的形态 —— 本项目的增强 Agent 不跑
    # ComfyUI-Moss 的 layout 编译器，只做描述式扩写。
    "moss-image": {
        "id": "moss-image",
        "label": "MOSS-Image 提示词（官方 MOSS Skill 描述式扩写）",
        "media": "image",
        "families": ["moss_image"],
        "template": "moss-image.txt",
        "validate_mode": "pe",
        "default_length": "medium",
        "builtin": True,
        "dimensions": ["主体与动作", "外观", "场景", "镜头与取景", "媒介",
                        "光线", "材质", "配色", "氛围", "图内文字（逐字）"],
        "source": "ComfyUI-Moss 插件内置官方 MOSS Skill（defaults/SKILL.md）",
    },
    "video": {
        "id": "video",
        "label": "视频提示词（MiniMax-H3）",
        "media": "video",
        "families": ["minimax_h3"],
        "template": "video-official.txt",
        "validate_mode": "pe",
        "default_length": "medium",
        "builtin": True,
        "dimensions": ["主体", "环境", "运镜", "光线", "风格", "声音"],
        "source": "预留复用位（沿用同一套 Agent 边界）",
    },
    # H3 官方「导演提示词」档：严格按 H3 的三段式结构
    # (integrated_multimodal_description / overall_soundscape / non_diegetic_music)
    # 输出。模板正文依据本机 ComfyUI 的官方 skill：
    #   custom_nodes/comfyui-minimax-h3-prompt-enhancer-T8/official_skills/
    #     h3-prompt-writing/references/base-en.txt
    # 与上面那个 "video" 档的区别：那个是泛用扩写，这个是**按 H3 官方
    # 视频提示词规范**写（镜头编号 / 切换时间 / 运镜三维度 / 说话人 ID /
    # 画面文字双引号 / 三段式字段）。
    "video-director": {
        "id": "video-director",
        "label": "视频导演提示词（MiniMax-H3 官方三段式）",
        "media": "video",
        "families": ["minimax_h3"],
        "template": "video-director.txt",
        "validate_mode": "pe",
        "default_length": "long",
        "builtin": True,
        "dimensions": ["integrated_multimodal_description", "overall_soundscape",
                        "non_diegetic_music", "镜头编号与切换", "运镜三维度",
                        "说话人 ID 与台词", "画面文字"],
        "source": "本机官方 skill h3-prompt-writing/references/base-en.txt（实测 2026-11-10）",
    },
    "audio": {
        "id": "audio",
        "label": "音频提示词（BGM / 音效）",
        "media": "audio",
        "families": ["*"],
        "template": "audio-official.txt",
        "validate_mode": "pe",
        "default_length": "short",
        "builtin": True,
        "dimensions": ["体裁", "节奏", "乐器", "音色情绪", "动态", "混音质感"],
        "source": "预留复用位（沿用同一套 Agent 边界）",
    },
}

# 模式 -> 媒体类型（决定候选模板的 media 维度）
MODE_MEDIA = {
    "t2i": "image",
    # Qwen-Image 2.1 的两个模式都是图像媒体
    "t2i_qwen": "image",
    "i2i_qwen": "image",
    # MOSS-Image 文生图也是图像媒体
    "t2i_moss": "image",
    "t2v": "video",
    "flf": "video",
    "r2v": "video",
    "longfilm": "video",
    "audio": "audio",
}

# 模型 key -> 模型族（决定候选模板的 families 维度）。
# 新装一个模型就往这里加一行；模板侧不用动。
MODEL_FAMILY = {
    "z_image_turbo_bf16": "z_image",
    "z_image_bf16": "z_image",
    "darkbeast_z6_blitz_fp8": "z_image",
    # Qwen-Image 2.1 的两个模型版本
    "qwen_image_2.1_bf16": "qwen_image21",
    "qwen_image_2.1_turbo_bf16": "qwen_image21",
    # MOSS-Image 的两个底模版本（Base / SFT）
    "moss_image_base": "moss_image",
    "moss_image_sft": "moss_image",
    # 视频侧的模型 key（MiniMax-H3 的 LoRA / 标准两档）
    "lora": "minimax_h3",
    "standard": "minimax_h3",
    # 文生视频新面板的模型 key（video_models.VIDEO_MODELS）——
    # 全部属于 H3 家族，所以增强模板的过滤结果不变。
    "fl2va_pruned_int8": "minimax_h3",
    "fl2va_int8": "minimax_h3",
    "fl2va_pruned_w4a8": "minimax_h3",
    "fl2va_bf16": "minimax_h3",
    "hybrid_fl2va_ref2va": "minimax_h3",
}

# 模式 -> 默认模型 key（用于解上下文；前端会传实际选中的模型）
MODE_DEFAULT_MODEL = {
    "t2i": "z_image_turbo_bf16",
    "t2i_moss": "moss_image_base",
    "t2v": "lora",
    "flf": "lora",
    "r2v": "lora",
    "longfilm": "lora",
}

# 各模式的默认模板（当前模板不适用时的回落目标）。
# 找不到就用该模式过滤结果里的第一个。
MODE_DEFAULT_SPEC = {
    "t2i": "image",
    # Qwen 两个模式各用自己的官方 PE 档
    "t2i_qwen": "qwen21-pe-t2i",
    "i2i_qwen": "qwen21-pe-i2i",
    # MOSS 用官方 Skill 的描述式扩写模板
    "t2i_moss": "moss-image",
    # 文生视频默认走 H3 官方「导演提示词」档（video-director）
    "t2v": "video-director",
    "flf": "video-director",
    "r2v": "video-director",
    "longfilm": "video-director",
    "audio": "audio",
}

# 视频 / 音频模板（占位实现，便于后续直接换官方原文）
VIDEO_TEMPLATE = """你是一位专业的 AI 视频提示词工程师，专精为 MiniMax-H3 等视频生成模型撰写提示词。

请把用户的粗略描述扩写成一段包含镜头、动作、环境与声音的完整视频提示词。

工作流程：首先锁定用户提示词中不可变更的核心要素（主体、数量、动作、状态、专有名词、颜色）；
接着判断是否需要"生成式推理"；然后注入专业级的画面细节（环境、时间、天气、空间层次）；
最后精确处理画面文字元素（一字不差转录并用英文双引号括起来）。

你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，也绝不包含"8K"、"masterpiece"等质量元标签或绘制指令。

仅严格输出最终的提示词，不要输出任何其他内容。

用户输入 prompt: {prompt}
"""

AUDIO_TEMPLATE = """你是一位专业的 AI 音乐与音效提示词工程师。

请把用户的粗略描述扩写成一段可用于音乐/音效生成的完整提示词。

需要覆盖：体裁、节奏、乐器、音色情绪、动态变化、混音质感、参考风格。

你的最终描述必须客观、具象，严禁使用比喻、情感化修辞，也绝不包含"8K"、"masterpiece"等质量元标签或绘制指令。

仅严格输出最终的提示词，不要输出任何其他内容。

用户输入 prompt: {prompt}
"""

# 内置模板正文（文件优先，缺文件时用这里的兜底）
_BUILTIN_FALLBACK = {
    "video-official.txt": VIDEO_TEMPLATE,
    "audio-official.txt": AUDIO_TEMPLATE,
}

# ------------------------------------------------------------------ 采样与长度

# 【官方】InvokeAI 采样口径：temperature=0.7, top_p=0.9, do_sample=True
# 【官方】BFL 官方 upsampler 用 greedy decoding
SAMPLING_PRESETS = {
    "sample": {
        "label": "采样（InvokeAI 口径）",
        "temperature": 0.7,
        "top_p": 0.9,
        "do_sample": True,
        "hint": "temperature 0.7 / top_p 0.9，每次扩写略有不同",
    },
    "greedy": {
        "label": "贪心（BFL 官方口径）",
        "temperature": 0.0,
        "top_p": 1.0,
        "do_sample": False,
        "hint": "同输入必得同输出，可复现",
    },
}

# 【官方】InvokeAI 官方口径：扩写 max_new_tokens = 300
# 【官方】Z-Image 本地 max_sequence_length=1024，官方换算 0.75 word/token
LENGTH_PRESETS = {
    "short": {"label": "精简", "max_tokens": 300, "hint": "官方默认 300 tokens ≈ 225 词"},
    "medium": {"label": "标准", "max_tokens": 300, "hint": "官方默认 300 tokens ≈ 225 词"},
    "long": {"label": "详尽", "max_tokens": 512, "hint": "512 tokens ≈ 384 词（上限 1024）"},
}

DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 300,
    "length": "medium",
    "sampling": "sample",
    "language": "auto",
}

# ------------------------------------------------------------------ 模板装载


def _read_template_file(filename: str) -> str:
    """从 assets/ 读模板正文；缺文件则回退到内置兜底。"""
    path = ASSETS_DIR / filename
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except Exception:
            pass
    return _BUILTIN_FALLBACK.get(filename, "")


def list_specs() -> list:
    """列出全部可用模板（供前端展示）。"""
    specs = []
    for key, spec in TEMPLATE_SPECS.items():
        specs.append({
            "id": spec["id"],
            "label": spec["label"],
            "media": spec["media"],
            "builtin": True,
            "families": list(spec.get("families") or []),
            "dimensions": spec["dimensions"],
            "source": spec["source"],
            "validate_mode": spec["validate_mode"],
        })
    return specs


def mode_media(mode: str) -> str:
    """某模式对应的媒体类型；未知模式返回空字符串（= 不过滤 media）。"""
    return MODE_MEDIA.get(mode or "") or ""


def model_family(model_key: str) -> str:
    """模型 key -> 模型族；未知模型返回空字符串。"""
    return MODEL_FAMILY.get(model_key or "") or ""


def spec_matches_context(spec: dict, media: str = "", family: str = "") -> bool:
    """
    模板是否适用于当前上下文（媒体类型 + 模型族）。

    规则（两条都过才算适用）：
      - media：模板的 media 必须等于当前媒体类型；模板没声明 media 则视为通用
      - families：模板的 families 含 "*" 或含当前模型族才算适用；
                  模板没声明 families 则视为通用
    上下文任一维度为空 -> 该维度不参与过滤（保守不隐藏）。
    """
    spec_media = spec.get("media") or ""
    if media and spec_media and spec_media != media:
        return False
    families = spec.get("families")
    if isinstance(families, str):
        families = [families]
    if not families:
        return True          # 未声明模型族 = 通用模板
    if not family:
        return True          # 上下文里没有模型族 -> 不按族过滤
    if "*" in families:
        return True
    return family in families


def filter_specs(specs: list, mode: str = "", model_key: str = "") -> list:
    """
    按当前上下文（模式 + 模型）过滤模板列表。

    前端下拉与后端校验共用同一套规则。
    mode 为空 / 未知模式 -> 原样返回（不过滤）。
    """
    media = mode_media(mode)
    if not media:
        return list(specs)
    family = model_family(model_key)
    return [s for s in specs if spec_matches_context(s, media, family)]


def default_spec_for_mode(mode: str, specs: list | None = None,
                          model_key: str = "") -> str | None:
    """
    该模式下的默认模板 id。

    优先用 MODE_DEFAULT_SPEC 指定的；它若不在过滤结果里（模板被删/改名），
    退回过滤结果里的第一个。全空则返回 None。
    """
    available = filter_specs(specs if specs is not None else list_specs(),
                             mode, model_key)
    if not available:
        return None
    preferred = MODE_DEFAULT_SPEC.get(mode)
    for s in available:
        if s.get("id") == preferred:
            return preferred
    return available[0].get("id")


def resolve_spec_for_mode(spec_id: str, mode: str = "", specs: list | None = None,
                          model_key: str = "") -> str:
    """
    保证传入的 spec_id 在当前模式下适用。

    - 适用 -> 原样返回
    - 不适用 / 不存在 -> 回落到该模式的默认模板
    这样「下拉显示的模板」与「实际执行的模板」永远一致。
    """
    available = filter_specs(specs if specs is not None else list_specs(),
                             mode, model_key)
    if not available:
        return spec_id
    ids = [s.get("id") for s in available]
    if spec_id in ids:
        return spec_id
    fallback = default_spec_for_mode(mode, available, model_key)
    return fallback if fallback else spec_id


def get_spec(spec_id: str, overrides: dict | None = None) -> dict:
    """
    取模板。overrides 来自用户持久化的自定义模板（可编辑正文）。
    找不到模板就回落到 image 官方 PE 档 —— 保证任何调用都有模板可用。

    overrides 可以是整个 enhancer 保存结构（{"specs": {...}, "params": {...}}），
    也可以直接是 specs 字典 —— 两种形状都支持。
    """
    overrides = overrides or {}
    specs = overrides.get("specs") if isinstance(overrides.get("specs"), dict) else overrides
    custom = specs.get(spec_id) if isinstance(specs, dict) else None
    base = TEMPLATE_SPECS.get(spec_id)
    if base is None:
        base = TEMPLATE_SPECS["image"]
    spec = dict(base)
    spec["system_prompt"] = _read_template_file(base["template"])
    # user_template 默认「用户输入 prompt: {prompt}」；
    # 官方 PE 两档的 system prompt 自己就定义了「用户请求」的角色，
    # 所以 user 消息直接放原始提示词（不加前缀），否则 PE 会把
    # 「用户输入 prompt:」这行字当成画面内容描述的一部分。
    default_user_template = base.get("user_template", "用户输入 prompt: {prompt}")
    spec["user_template"] = default_user_template
    if isinstance(custom, dict):
        for key in ("system_prompt", "user_template"):
            if isinstance(custom.get(key), str) and custom[key].strip():
                spec[key] = custom[key]
        # 只有真正覆盖了内容才算自定义；两字段都空 = 用内置模板
        if spec["system_prompt"] != _read_template_file(base["template"]) \
                or spec["user_template"] != default_user_template:
            spec["builtin"] = False
    return spec


def resolve_params(params: dict | None, overrides: dict | None = None,
                   spec: dict | None = None) -> dict:
    """
    合并参数：内置默认 <- 用户保存的覆盖 <- 本次请求传入。
    采样档优先于裸 temperature/top_p；长度档优先于裸 max_tokens。

    spec 给了的话，用它的 default_length 作为长度档的起点
    （bbox 档要输出 caption + JSON，比纯 caption 长，默认给 longer 的预算）。
    """
    params = params or {}
    overrides = overrides or {}
    saved = overrides.get("params") or {}
    merged = dict(DEFAULT_PARAMS)
    merged.update({k: v for k, v in saved.items() if k in DEFAULT_PARAMS})
    merged.update({k: v for k, v in params.items() if k in DEFAULT_PARAMS})

    # 采样档
    sampling = merged.get("sampling", "sample")
    if sampling in SAMPLING_PRESETS:
        preset = SAMPLING_PRESETS[sampling]
        merged["temperature"] = preset["temperature"]
        merged["top_p"] = preset["top_p"]
        merged["do_sample"] = preset["do_sample"]
    else:
        merged["temperature"] = float(merged.get("temperature", 0.7))
        merged["top_p"] = float(merged.get("top_p", 0.9))
        merged["do_sample"] = True

    # 长度档优先级：
    #   本次请求 > 该模板单独保存的长度 > 模板默认 > 全局保存 > 内置默认
    length = params.get("length")
    if not length:
        spec_lengths = saved.get("spec_lengths") or {}
        length = spec_lengths.get((spec or {}).get("id")) if isinstance(spec_lengths, dict) else None
    if not length:
        length = (spec or {}).get("default_length")
    if not length:
        length = saved.get("length")
    if not length:
        length = merged.get("length", "medium")
    merged["length"] = length
    if length in LENGTH_PRESETS:
        merged["max_tokens"] = LENGTH_PRESETS[length]["max_tokens"]
    else:
        merged["max_tokens"] = int(merged.get("max_tokens") or 300)
    merged["max_tokens"] = min(int(merged["max_tokens"]), 1024)

    if merged.get("language") not in ("auto", "zh", "en"):
        merged["language"] = "auto"
    return merged


def build_messages(spec: dict, original: str, language: str = "auto") -> list:
    """构造统一消息格式（system + user）。"""
    original = (original or "").strip()
    user_text = spec["user_template"].replace("{original}", original)
    user_text = user_text.replace("{prompt}", original)
    if language and language != "auto":
        user_text += f"\n\n请用{language}输出。"
    return [
        {"role": "system", "content": spec["system_prompt"]},
        {"role": "user", "content": user_text},
    ]


# ------------------------------------------------------------------ 输出处理


def strip_wrapping(text: str) -> str:
    """
    清理模型可能的包装：代码块围栏、引号、以及「提示词：」这类前缀。
    只做保守剥离，不动正文内容。
    """
    if not text:
        return ""
    result = text.strip()
    if result.startswith("```"):
        lines = result.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        result = "\n".join(lines).strip()
    for prefix in ("扩写后的提示词：", "扩写结果：", "提示词：", "Prompt:", "prompt:"):
        if result.startswith(prefix):
            result = result[len(prefix):].strip()
    if len(result) >= 2 and result[0] in "\"'“" and result[-1] in "\"'”":
        result = result[1:-1].strip()
    return result


def strip_thinking(text: str) -> str:
    """
    去掉 reasoning 模型混进 content 的思考痕迹。
    与 llm.py 里的同名函数保持一致 —— 这里独立一份，避免 enhancer 对 llm 的耦合。
    """
    if not text:
        return ""
    result = text
    for tag in ("think", "thinking", "reasoning"):
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        while open_tag in result.lower():
            lower = result.lower()
            start = lower.find(open_tag)
            end = lower.find(close_tag, start)
            if end == -1:
                result = result[:start]
                break
            result = result[:start] + result[end + len(close_tag):]
    return result.strip()


def extract_json_block(text: str) -> str:
    """
    从 bbox 档输出里抽出 JSON 数组片段。
    优先匹配 ```json 围栏，其次匹配最外层 [ ... ]。

    注意：max_new_tokens 有限时模型输出可能被截断，JSON 会不完整。
    这种情况下做「尽力修复」：补齐未闭合的字符串与括号，
    并去掉末尾多余的逗号 —— 让尽可能多的元素仍然可用。
    """
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        blob = fence.group(1).strip()
    else:
        start = text.find("[")
        if start == -1:
            return ""
        depth = 0
        blob = ""
        for i in range(start, len(text)):
            blob += text[i]
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    break
    blob = _repair_truncated_json(blob)
    return blob


def _repair_truncated_json(blob: str) -> str:
    """修复被 max_tokens 截断的 JSON 数组。"""
    blob = blob.strip()
    if not blob:
        return blob
    # 去掉末尾多余的逗号
    blob = re.sub(r",\s*$", "", blob)
    # 统计未闭合的字符串与括号
    in_string = False
    escape = False
    brackets = []
    for ch in blob:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "[{":
            brackets.append(ch)
        elif ch in "]}":
            if brackets:
                brackets.pop()
    # 补齐未闭合的字符串
    if in_string:
        blob += '"'
    # 补齐未闭合的括号
    for ch in reversed(brackets):
        blob += "]" if ch == "[" else "}"
    # 去掉截断留下的「键名半截」（如 "desc": "A small white te）
    blob = re.sub(r',?\s*"[a-zA-Z_]+"\s*:\s*"[^"]*$', "", blob)
    # 去掉截断留下的孤立的半个元素
    blob = re.sub(r",\s*\{\s*$", "", blob)
    blob = re.sub(r",\s*$", "", blob)
    return blob


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


# ------------------------------------------------------------------ 校验

_VALIDATOR = None


def _load_validator():
    """加载 scripts/validate.py（只跑不读 —— 不把校验源码塞进内存上下文）。"""
    global _VALIDATOR
    if _VALIDATOR is not None:
        return _VALIDATOR
    path = SCRIPTS_DIR / "validate.py"
    if not path.exists():
        _VALIDATOR = None
        return None
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("h3_prompt_validator", path)
        if spec is None or spec.loader is None:
            _VALIDATOR = None
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _VALIDATOR = module
    except Exception:
        _VALIDATOR = None
    return _VALIDATOR


def validate_output(text: str, original: str = "", mode: str = "pe") -> dict:
    """
    调用校验器。返回 {"ok", "errors", "warnings", "stats"}。
    校验器缺失时降级为「只做 think 段检查」，不因此让整个增强失败。
    """
    module = _load_validator()
    if module is None:
        leaked = strip_thinking(text)
        if leaked != (text or "").strip():
            return {"ok": False, "errors": ["出现 thinking/思考痕迹"], "warnings": [],
                    "stats": {}}
        return {"ok": True, "errors": [], "warnings": [], "stats": {}}
    return module.validate(text, original, mode)


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
