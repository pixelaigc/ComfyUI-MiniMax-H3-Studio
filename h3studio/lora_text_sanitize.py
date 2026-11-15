"""LoRA 清单「用户可见文案」的出站清洗。

【为什么需要这一层】
LoRA 目录（``lora_catalog_ea.py`` / ``lora_catalog_nsfw.py`` /
``lora_catalog_effects.py`` …）里的 ``scene`` / ``strength_source`` 字段是
**调研记录**：它们直接引用上游模型卡的原始字段名与判定过程，例如

    基座判定：原生（Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']; vision_foundation=Z_IMAGE_TURBO）。
    作者未给触发词（README instance_prompt 为 null）。
    HF 镜像 key_map 命中判定：{'attn_hit': 128, 'attn_total': 128, 'attn_ratio': 1.0, ...}

这些字段会**原样**出现在用户界面上（前端 web/app.js 把 ``scene`` 写进
``<option title>``、下拉选项文本、LoRA 详情模态框），用户看到的是
``Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']`` 这种机器字段名 ——
它对「这个 LoRA 适不适合我的底模、触发词是什么」没有任何帮助。

【清洗原则 —— 只改「机器字段名」，不删「语义」】
  * **保留**：方向描述、触发词、推荐强度与依据、来源平台、近似适用判定、
    兼容性结论、需要哪种加载器这类**行动指引** —— 这些是用户真正需要的。
  * **改写**：机器字段名 → 自然语言（``Data.BaseModel`` → 「基座模型」）。
  * **删除**：纯机器判定过程的中间量（``key_map`` 命中字典、tensor 逐项拆解）
    —— 这些是判定**过程**，不是结论；结论（原生 / 近似适用）已经在同一句里。
  * **不改**：不含机器字段名的普通文案，一个字都不动。

设计成**幂等的纯函数**：同样的输入永远得到同样的输出，
这样调用点可以随便加，重复清洗不会把文案洗坏。
"""

from __future__ import annotations

import re

__all__ = ["sanitize_user_text", "sanitize_scene", "sanitize_strength_source"]


# --------------------------------------------------------------------------
# 1) 整段机器判定过程 —— 删掉
# --------------------------------------------------------------------------
#
# 例：HF 镜像 key_map 命中判定：{'attn_hit': 128, 'attn_total': 128, 'attn_ratio': 1.0,
#     'all_hit': 224, 'all_total': 224, 'all_ratio': 1.0, 'blocks': 32}
#
# 整段是「怎么判出来的」，不是「判定结果」。判定结果（同句里的「原生」）
# 保留。这里匹配到第一个右花括号为止。

_DICT_DUMP = re.compile(
    r"[(（]?\s*(?:HF\s*)?(?:镜像\s*)?key_map\s*命中判定\s*[:：]\s*\{[^{}]*\}[)）]?",
    re.IGNORECASE,
)

# 「attn_hit 128/128」这类裸统计（不在字典里的写法）
_BARE_HIT_STATS = re.compile(
    r"[(（]?\s*attn_hit\s*[:：]?\s*\d+\s*/\s*\d+[^)）]*[)）]?",
    re.IGNORECASE,
)

# 权重内部结构的逐项拆解。
# 【边界很关键】只删「这一小段」，不能吃掉后面的句子 ——
# 「ComfyUI 需支持 LoKR 的加载器」是**行动指引**，必须留下。
# 所以：句子边界（。！？）内匹配，句末的标点保留。
_TENSOR_SENTENCE = re.compile(
    r"[^。！？]*?\d+\s*tensor[^。！？]*?(?:lora_A|lora_B|lokr_w)[^。！？]*",
    re.IGNORECASE,
)
# 「权重 480 tensor 全为 LoRA（lora_A/lora_B 各 240）」—— 括号内是配对括号
_TENSOR_FULL = re.compile(
    r"权重\s*\d+\s*tensor\s*全为\s*LoRA（[^）]*）",
    re.IGNORECASE,
)
# 「权重 480 tensor 全为 LoRA」这种没有括号的变体
_TENSOR_BARE = re.compile(
    r"权重\s*\d+\s*tensor\s*全为\s*LoRA",
    re.IGNORECASE,
)
# 孤立的「480 tensor」计数
_TENSOR_COUNT = re.compile(
    r"\d+\s*tensor\s*全为\s*LoRA",
    re.IGNORECASE,
)
# 「630 tensor / 420 lora」这类「tensor 计数 / lora 计数」配对。
# 【为什么要单独一条】单纯的「\d+ tensor」删除会留下悬空的「/ 420 lora」。
# 所以这里把「N tensor / M lora」**整体**替换成一句自然语言，
# 而不是删掉一半。
_TENSOR_SLASH = re.compile(
    r"\d+\s*tensor\s*/\s*\d+\s*lora",
    re.IGNORECASE,
)
# 全角分隔符 / 空格变体
_TENSOR_SLASH2 = re.compile(
    r"\d+\s*tensor\s*[/／]\s*\d+\s*lora",
    re.IGNORECASE,
)
# 「480 tensor 全 LoRA」/「480 tensor 全为 LoRA」/「630 tensor」等变体。
# 【边界】只删「tensor」这个计数短语本身，不要吃掉后面的分句。
_TENSOR_PHRASE = re.compile(
    r"\d+\s*tensor\s*(?:全为|全|均为)?\s*(?:LoRA|LoKr)?\s*",
    re.IGNORECASE,
)
# 「kohya 格式 LoRA（lora_down/lora_up 命名，630 tensor，含 alpha），140MB 真…」
# —— 权重内部键名清单
_LORA_DOWN_UP = re.compile(r"lora_down\s*/\s*lora_up", re.IGNORECASE)
# 「*.safetensors」这类文件名后缀出现在说明里
_SUFFIX_TOKENS = re.compile(r"\*\.[a-z0-9]+", re.IGNORECASE)
# 权重内部**键名路径**（diffusion_model.layers.N.* / diffusion_model.blocks.N.*）
# —— 这类路径对用户判断「能不能挂到本机底模」没有直接用处，
# 但「是 z-image 架构还是 Krea-2 架构」这个结论要保留。
_WEIGHT_KEY_PATH = re.compile(
    r"键名\s*[A-Za-z_][\w.]*\.\w+\.\w+\.N\.\*|"
    r"键名\s*[A-Za-z_][\w.]*\.\w+\.N\.\*",
    re.IGNORECASE,
)
# 裸的键名路径
_BARE_KEY_PATH = re.compile(
    r"\bdiffusion_model\.(?:layers|blocks)\.N\.\*",
    re.IGNORECASE,
)
# 「ss_output_name = xll567」这类单个调试字段
_SS_OUTPUT_NAME = re.compile(r"ss_output_name\s*=\s*[\w.\-]+", re.IGNORECASE)


# --------------------------------------------------------------------------
# 2) 机器字段名 → 自然语言（逐项改写）
# --------------------------------------------------------------------------
#
# 顺序有意义：先处理长串（Data.BaseModel / modelVersions[].baseModel），
# 再处理单词，避免长串被单词规则切碎。

_FIELD_REWRITES: list[tuple[re.Pattern[str], str]] = [
    # Civitai modelVersions[].baseModel = ZImageTurbo
    (re.compile(r"Civitai\s*modelVersions\[\]\.baseModel\s*=\s*(\S+?)(?=[)）,；。（]|$)",
                re.IGNORECASE),
     r"平台标注的基座模型为 \1"),
    # Data.BaseModel=['Tongyi-MAI/Z-Image-Turbo']
    (re.compile(r"Data\.BaseModel\s*=\s*\[([^\]]*)\]",
                re.IGNORECASE),
     r"基座模型为 [\1]"),
    (re.compile(r"BaseModelRelation\s*=\s*(\S+?)(?=[)）,；。（]|$)",
                re.IGNORECASE),
     r"基座关系为 \1"),
    # README frontmatter vision_foundation: Z_IMAGE_TURBO
    (re.compile(r"README\s*frontmatter\s*vision_foundation\s*[:：]\s*(\S+?)(?=[;；)）,。]|$)",
                re.IGNORECASE),
     r"模型卡头部标注的视觉基座为 \1"),
    # AigcAttributes.SubVisionFoundation
    (re.compile(r"AigcAttributes\.SubVisionFoundation\s*=\s*(\S+?)(?=[;；)）,。]|$)",
                re.IGNORECASE),
     r"平台属性记录的视觉基座为 \1"),
    (re.compile(r"frontmatter\s+vision_foundation\s*[:：]\s*(\S+?)(?=[;；)）,。]|$)",
                re.IGNORECASE),
     r"模型卡头部标注的视觉基座为 \1"),
    (re.compile(r"README\s+frontmatter\b", re.IGNORECASE), "模型卡头部"),
    (re.compile(r"\bfrontmatter\b", re.IGNORECASE), "模型卡头部"),
    # vision_foundation
    (re.compile(r"vision_foundation\s*[:：=]\s*(\S+?)(?=[;；)）,。]|$)",
                re.IGNORECASE),
     r"视觉基座为 \1"),
    (re.compile(r"\bvision_foundation\b", re.IGNORECASE), "视觉基座"),
    # instance_prompt / trainedWords
    (re.compile(r"README\s+instance_prompt\s+为\s*null", re.IGNORECASE),
     "模型卡未标注触发词字段"),
    (re.compile(r"instance_prompt\s+为\s*null", re.IGNORECASE),
     "触发词字段为空"),
    (re.compile(r"\binstance_prompt\b", re.IGNORECASE), "触发词字段"),
    (re.compile(r"trainedWords\s+为空", re.IGNORECASE), "训练词列表为空"),
    (re.compile(r"\btrainedWords\b", re.IGNORECASE), "训练词列表"),
    # modelVersions[].baseModel（无 Civitai 前缀）
    (re.compile(r"modelVersions\[\]\.baseModel\s*=\s*(\S+?)(?=[)）,；。（]|$)",
                re.IGNORECASE),
     r"平台标注的基座模型为 \1"),
    # 权重内部字段名
    (re.compile(r"\blora_A\b\s*/\s*\blora_B\b", re.IGNORECASE), "标准 LoRA 权重"),
    (re.compile(r"\blora_A\b", re.IGNORECASE), "LoRA 权重"),
    (re.compile(r"\blora_B\b", re.IGNORECASE), "LoRA 权重"),
    (re.compile(r"\blokr_w\d?\b", re.IGNORECASE), "LoKr 权重"),
    # 基座判定的内部取值
    (re.compile(r"基座判定：原生（Base 训）", re.IGNORECASE), "基座判定：原生（基础模型训练）"),
    (re.compile(r"基座判定：原生（SFT 原生）", re.IGNORECASE), "基座判定：原生（SFT 版本原生）"),
    (re.compile(r"基座判定：原生（MOSS 原生）", re.IGNORECASE), "基座判定：原生（MOSS 版本原生）"),
    (re.compile(r"基座判定：原生（Base 原生）", re.IGNORECASE), "基座判定：原生（基础模型原生）"),
    (re.compile(r"HF 镜像", re.IGNORECASE), "镜像站"),
    (re.compile(r"\bkey_map\b", re.IGNORECASE), "权重键名匹配"),
    # safetensors 头部元数据字段名（出现在「说明来源」里）
    (re.compile(r"ss_tag_frequency\s*/\s*ss_base_model_version", re.IGNORECASE),
     "标签与基座版本元数据"),
    (re.compile(r"\bss_tag_frequency\b", re.IGNORECASE), "标签频次元数据"),
    (re.compile(r"\bss_base_model_version\b", re.IGNORECASE), "基座版本元数据"),
    (re.compile(r"extracted_by\s*[:：]\s*([\w.\-]+)", re.IGNORECASE),
     r"由 \1 提取"),
    (re.compile(r"\bextracted_by\b", re.IGNORECASE), "提取工具"),
    # 「base」字段的取值本身可能是机器字段名
    (re.compile(r"^modelspec\.architecture$", re.IGNORECASE), "架构标识"),
    (re.compile(r"^ss_base_model_version$", re.IGNORECASE), "基座版本标识"),
    # 句中的「元数据 modelspec.architecture = Krea-2/lora」
    (re.compile(r"元数据\s*modelspec\.architecture\s*=\s*(\S+?)(?=[)）,；。]|$)",
                re.IGNORECASE),
     r"元数据标注的架构为 \1"),
    (re.compile(r"modelspec\.architecture\s*=\s*(\S+?)(?=[)）,；。]|$)",
                re.IGNORECASE),
     r"架构标识为 \1"),
    (re.compile(r"\bmodelspec\.architecture\b", re.IGNORECASE), "架构标识"),
    # 目录里的**文件名键**（上游仓库的主文件 / 架构示例文件）
    (re.compile(r"按体积排序的\s*main_file\s*恰好是", re.IGNORECASE),
     "按体积排序的主文件恰好是"),
    (re.compile(r"\bzimage_arch_sample\b", re.IGNORECASE), "架构示例文件"),
    (re.compile(r"\bmain_file\b", re.IGNORECASE), "主文件"),
]


def _collapse_spaces(text: str) -> str:
    """收尾清理：去掉清洗留下的空括号 / 悬空标点 / 多余空格。"""
    out = text
    # 空括号
    out = re.sub(r"[（(]\s*[)）]", "", out)
    # 连续的分隔符只留一个
    out = re.sub(r"\s*([，；、])\s*([，；、])", r"\1", out)
    # 删段后可能留下重复的句末标点（如「...585MB。ComfyUI 需支持」被删成「。。」）
    out = re.sub(r"([。！？])\s*([。！？])+", r"\1", out)
    out = re.sub(r"([。！？])\s*([。！？])", r"\1", out)
    # 标点前的空格
    out = re.sub(r"[ \t]+([。！？，；、）)])", r"\1", out)
    # 开括号后的空格
    out = re.sub(r"([（(])[ \t]+", r"\1", out)
    # 多余空格收敛为单个
    out = re.sub(r"[ \t]{2,}", " ", out)
    # 结尾多余的括号 / 分隔符
    out = re.sub(r"[（(]\s*[,，;；]\s*[)）]", "", out)
    out = re.sub(r"[,，;；]\s*[)）]", "", out)
    out = re.sub(r"\s*([，；、])\s*$", "", out)
    return out.strip()


def sanitize_user_text(text: str | None) -> str:
    """把 LoRA 目录里的一段「面向用户的说明文字」清洗成不含机器字段名的版本。

    幂等：对已清洗过的文本再跑一次，结果不变。

    >>> sanitize_user_text("作者未给触发词（README instance_prompt 为 null）。")
    '作者未给触发词（模型卡未标注触发词字段）。'
    """
    if not text:
        return ""
    out = str(text)

    # --- 第一步：删掉机器判定过程 ---
    out = _DICT_DUMP.sub("", out)
    out = _BARE_HIT_STATS.sub("", out)
    # 「权重 480 tensor 全为 LoRA（lora_A/lora_B 各 240）」先整段替换 ——
    # 括号是配对的，必须先处理，否则后面的规则会留下「权重）」这种残渣。
    out = _TENSOR_FULL.sub("权重结构为标准 LoRA", out)
    out = _TENSOR_BARE.sub("权重结构为标准 LoRA", out)
    out = _TENSOR_COUNT.sub("标准 LoRA 权重", out)
    # 逐句删 tensor 拆解（保留句子末尾标点）
    out = _TENSOR_SENTENCE.sub("", out)
    # 「630 tensor / 420 lora」先整体处理，否则会留下悬空的「/ 420 lora」
    out = _TENSOR_SLASH.sub("权重结构为标准 LoRA", out)
    # _TENSOR_SLASH 没匹配到的「N tensor / M lora」变体（分隔符可能是全角 /）
    out = _TENSOR_SLASH2.sub("权重结构为标准 LoRA", out)
    # 剩下的「480 tensor 全 LoRA」/「630 tensor」这类计数短语
    out = _TENSOR_PHRASE.sub("", out)
    out = _LORA_DOWN_UP.sub("标准 LoRA 权重", out)
    out = _WEIGHT_KEY_PATH.sub("键名结构已核对", out)
    out = _BARE_KEY_PATH.sub("", out)
    out = _SUFFIX_TOKENS.sub("", out)
    out = _SS_OUTPUT_NAME.sub("", out)

    # --- 第二步：改写机器字段名 ---
    for pattern, repl in _FIELD_REWRITES:
        out = pattern.sub(repl, out)

    return _collapse_spaces(out)


def sanitize_scene(text: str | None) -> str:
    """``scene`` 字段的出站清洗（LoRA 场景说明）。"""
    return sanitize_user_text(text)


def sanitize_strength_source(text: str | None) -> str:
    """``strength_source`` 字段的出站清洗（推荐强度依据）。"""
    return sanitize_user_text(text)
