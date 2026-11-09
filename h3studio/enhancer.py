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
    "t2v": "video",
    "flf": "video",
    "r2v": "video",
    "longfilm": "video",
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
#
# 【2026-11-09 修正】原先的 300/300/512 + min(..., 1024) 硬上限是**真实缺陷**：
#   逐物体枚举（二次增强）需要 2000+ tokens 才能写完 L1/L2/L3/环境现象/主体细化，
#   512 会在 L2 中途硬切，后面的背景/环境/氛围/构图整段丢失 —— 表现为
#   「二次增强只剩人物特写，骑楼/霓虹/海报/路面/光线全部消失」。
#   官方 PE 的 300 是**单段 caption** 的口径，不是逐物体枚举的口径；
#   直接套用到 refine 档就是拿错了尺子。
#
# 输出 token 上限的**可配置天花板**。
# 用户可在「本地模型配置 → 增强 Agent」里改，默认 32768。
# 后端实际能力：llama.cpp server 的 n_ctx=262144，本机 VRGDG_LocalLLM 节点
# 的 max_tokens 字段上限是 262144 —— 所以 65536 也是后端允许的。
MAX_TOKENS_CEILING = 32768
# 允许的取值范围（前端输入框也要用同一套边界）
MAX_TOKENS_FLOOR = 256
MAX_TOKENS_MAX = 65536

# 现在的口径：
#   - short/medium 保留官方 300（单段 caption 场景确实够用，且是官方可复现值）
#   - long 用**用户配置的上限**（默认 32768）—— 逐物体枚举的实际需求随
#     提示词长度变化（实测 381 字中文 -> 2254 tokens），
#     写死一个数不是「上限」而是「另一个硬编码」。
#   - 硬上限改为 MAX_TOKENS_CEILING，由用户在「本地模型配置」里可调，
#     默认 32768（≈ 2.4 万汉字，远超任何一次提示词增强的实际需要）。
LENGTH_PRESETS = {
    "short": {"label": "精简", "max_tokens": 300, "hint": "官方默认 300 tokens ≈ 225 词"},
    "medium": {"label": "标准", "max_tokens": 300, "hint": "官方默认 300 tokens ≈ 225 词"},
    "long": {"label": "详尽", "max_tokens": MAX_TOKENS_CEILING,
             "hint": "用「最大输出 token 上限」（默认 32768）—— 逐物体枚举档"},
}

DEFAULT_PARAMS = {
    "temperature": 0.7,
    "top_p": 0.9,
    "max_tokens": 300,
    "length": "medium",
    "sampling": "sample",
    "language": "auto",
    # 输出 token 上限的天花板 —— 用户可配置，不再是硬编码 512/1024。
    "max_tokens_ceiling": MAX_TOKENS_CEILING,
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
    merged["max_tokens"] = _tier_budget(length, merged)
    merged["max_tokens"] = clamp_max_tokens(merged["max_tokens"], merged)

    if merged.get("language") not in ("auto", "zh", "en"):
        merged["language"] = "auto"
    return merged


def _tier_budget(tier: str, merged_params: dict) -> int:
    """
    该长度档实际使用的输出 token 预算。

      short / medium —— 官方单段 caption 口径 300 tokens（可复现官方值）。
                        单段扩写本来就不需要更多，硬给大预算只是让模型
                        有空间去絮絮叨叨。
      long           —— 逐物体枚举档（二次增强用它）。预算交给**用户配置的
                        上限**：实际需求随提示词长度变化
                        （实测 381 字中文 -> 2254 tokens），
                        写死一个数不是「上限」而是「另一个硬编码」。
    """
    ceiling = _ceiling_of(merged_params)
    if tier in ("short", "medium"):
        return min(LENGTH_PRESETS.get(tier, {}).get("max_tokens", 300), ceiling)
    return ceiling


def _ceiling_of(merged_params: dict | None) -> int:
    """取（并归一）输出 token 上限天花板。"""
    merged_params = merged_params or {}
    ceiling = merged_params.get("max_tokens_ceiling")
    if ceiling is None:
        ceiling = MAX_TOKENS_CEILING
    try:
        ceiling = int(ceiling)
    except (TypeError, ValueError):
        ceiling = MAX_TOKENS_CEILING
    return max(MAX_TOKENS_FLOOR, min(ceiling, MAX_TOKENS_MAX))


def clamp_max_tokens(value, merged_params: dict | None = None) -> int:
    """
    把 max_tokens 夹到 [MAX_TOKENS_FLOOR, 用户配置的天花板] 区间。

    天花板来源（优先级从高到低）：
      1. 本次请求传入的 max_tokens_ceiling
      2. 用户在「本地模型配置」里保存的 params.max_tokens_ceiling
      3. 内置默认 MAX_TOKENS_CEILING（32768）

    无论夹到多少，都**不会**再被一个更小的硬编码数字（如原来的 1024）盖回去。
    """
    ceiling = _ceiling_of(merged_params)
    try:
        val = int(value)
    except (TypeError, ValueError):
        val = 300
    return max(MAX_TOKENS_FLOOR, min(val, ceiling))


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


# ------------------------------------------------------------------ 要素覆盖核查

# 中文分句符（中英文标点 + 换行）
_CLAUSE_SPLIT = re.compile(r"[，。；、！？!?;,\n\r]+")
# 英文单词（>=4 字母，避免 a/an/is 之类）
_EN_WORD = re.compile(r"[A-Za-z][A-Za-z\-']{3,}")
# 连续汉字（>=2 字）
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")

# 纯连接词/代词：本身不承载画面要素，切句时剔除。
# 注意：只剔除**整个片段恰好等于**这些词的情况；
# 「光线」「构图」「氛围」「霓虹」这类**画面维度**必须保留
# （用户明确要求这些不能丢），所以不在这个表里。
_CLAUSE_NOISE = {
    "她的", "他的", "它的", "他们", "她们", "以及", "并且", "同时", "还有",
    "其中", "这个", "那个", "可以", "能够", "整体", "感觉",
}

# 二次增强（refine 档）的结构分段标题 —— 模板规定按此顺序输出。
# 若输出被 token 上限硬切，**后面的分段会整段消失**，
# 这正是「背景/环境/氛围全丢」的可观测特征。
#
# 注意：模板写明「若某一级没有物体，跳过该级标题」——
# 所以**不能**要求每一级都在（模型跳过 L1 是合法的）。
# 真正可靠的判据是**末段标题**：主体细化是模板的最后一段，
# 它不在 = 输出在中途被切断。
REFINE_SECTIONS = ("【L1", "【L2", "【L3", "【环境现象】", "【主体细化】")
REFINE_TERMINAL_SECTION = "【主体细化】"


def _clause_units(original: str) -> list:
    """
    把用户原文切成「可核查的要素单元」。

    中文按标点切句后取连续汉字片段；英文按单词。
    过滤掉纯连接词/泛称，避免把句法变化误判成内容丢失。
    """
    units = []
    for chunk in _CLAUSE_SPLIT.split(original or ""):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        for run in _CJK_RUN.findall(chunk):
            if run in _CLAUSE_NOISE or len(run) < 2:
                continue
            units.append(run)
        for word in _EN_WORD.findall(chunk):
            if word.lower() in _CLAUSE_NOISE:
                continue
            units.append(word)
    return units


def _lcs_len(a: str, b: str) -> int:
    """最长公共子序列长度（用于中文改写后的语义近似判定）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
            else:
                left = prev[j]
                up = cur[j - 1]
                cur[j] = left if left >= up else up
        prev = cur
    return prev[len(b)]


def _unit_covered(unit: str, enhanced_low: str) -> bool:
    """
    一个要素单元是否在扩写结果里被保留。

    中文：逐字命中，或存在 >= 3 个字的公共子序列即视为保留
    （模型常把「褪色的繁体中文海报」写成「繁体中文海报（右侧墙面）」，
    逐字比对会因为插入字而误判，改用 LCS 更贴近「语义是否还在」）。
    英文：按词边界整词匹配。

    这是一个**保守**的信号：refine 模板是「重新表述」而不是「逐字复制」，
    所以单个要素单元匹配不上不代表要素丢了 —— 它只用于给出粗粒度的
    「保留了多少原文片段」的量化指标，不能单独当作失败判据。
    真正的失败判据是下面的 truncation 检测（finish_reason + 结构完整性）。
    """
    if re.search(r"[\u4e00-\u9fff]", unit):
        if unit in enhanced_low:
            return True
        return _lcs_len(unit, enhanced_low) >= 3
    pattern = r"(?<![a-z0-9])" + re.escape(unit.lower()) + r"(?![a-z0-9])"
    return bool(re.search(pattern, enhanced_low))


def check_element_coverage(original: str, enhanced: str) -> dict:
    """
    核查「扩写结果保留了多少原文要素」。

    返回 {"ratio", "missing", "missing_count", "total"}。
    ratio 是**粗粒度指标**，不是通过/失败判据（refine 模板是重述式改写，
    单个片段匹配不上不代表要素丢了）。
    """
    units = _clause_units(original)
    if not original or not enhanced or not units:
        return {"ratio": 1.0, "missing": [], "missing_count": 0, "total": len(units)}
    low = enhanced.lower()
    missing = [u for u in units if not _unit_covered(u, low)]
    ratio = 1.0 - (len(missing) / len(units))
    return {"ratio": round(ratio, 4), "missing": missing,
            "missing_count": len(missing), "total": len(units)}


def check_truncation(enhanced: str, finish_reason: str = "",
                     validate_mode: str = "", expected_sections=None) -> dict:
    """
    检测「输出被 token 上限硬切」。

    两个独立信号，任意一个命中即判定为截断：

    1. finish_reason == "length"
       —— 后端明确告诉我们：生成是因为达到 max_tokens 而停止的。
          这是**权威**信号（llama.cpp / OpenAI 兼容后端都会给）。

    2. 末段标题缺失（refine 档）
       —— refine 模板规定按 L1 → L2 → L3 → 环境现象 → 主体细化 顺序输出，
          「主体细化」是**最后一段**。它不在 = 输出在中途被切断，
          后面该出现的背景 / 环境 / 氛围 / 构图整段丢了。

    注意：**不能**用「每一级标题都必须存在」当判据 ——
    模板明确写了「若某一级没有物体，跳过该级标题」，
    模型合法地跳过 L1 是正常的（实测本地模型确实会这么干，
    早先按「所有级都在」判会误报截断，把好结果当失败拒掉）。

    返回 {"truncated": bool, "reason": str, "missing_sections": [...]}。
    """
    text = enhanced or ""
    missing_sections = []
    # 记录「调用方是否显式指定了分段口径」——
    # 必须在把 None 换成默认口径**之前**记下来，
    # 否则后面判断「末段缺失」时会误以为调用方指定过口径。
    explicit_sections = expected_sections is not None
    if expected_sections is None:
        expected_sections = REFINE_SECTIONS if validate_mode == "refine" else ()
    for sec in expected_sections:
        if sec not in text:
            missing_sections.append(sec)

    # 信号 1：后端明确说「因为长度停止」
    if finish_reason == "length":
        reason = (f"输出被 max_tokens 上限硬切（finish_reason=length），"
                  f"内容不完整")
        if missing_sections:
            reason += "，缺失分段：" + "、".join(missing_sections)
        return {"truncated": True, "reason": reason,
                "missing_sections": missing_sections}

    # 信号 2：refine 档的末段标题缺失 —— 输出在中途被切断。
    # 只看末段（主体细化），中间各级允许被合法跳过。
    if validate_mode == "refine" and not explicit_sections \
            and REFINE_TERMINAL_SECTION not in text:
        reason = (f"输出缺少末段「{REFINE_TERMINAL_SECTION}」，"
                  "说明在中途被截断 —— 后面的背景/环境/氛围/构图整段丢失")
        return {"truncated": True, "reason": reason,
                "missing_sections": missing_sections}

    return {"truncated": False, "reason": "", "missing_sections": missing_sections}


def coverage_report(original: str, enhanced: str, finish_reason: str = "",
                     validate_mode: str = "", expected_sections=None) -> dict:
    """
    要素覆盖 + 截断检测 -> 完整报告。供 /api/enhance 回传并在 UI 里显示。
    """
    cov = check_element_coverage(original, enhanced)
    trunc = check_truncation(enhanced, finish_reason, validate_mode, expected_sections)
    return {
        "ok": not trunc["truncated"],
        "ratio": cov["ratio"],
        "missing": cov["missing"],
        "missing_count": cov["missing_count"],
        "total": cov["total"],
        "truncated": trunc["truncated"],
        "truncation_reason": trunc["reason"],
        "missing_sections": trunc["missing_sections"],
    }


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
