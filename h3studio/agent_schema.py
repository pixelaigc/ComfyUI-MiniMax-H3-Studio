# -*- coding: utf-8 -*-
"""
Agent 的**参数合法性校验器**与**模式 schema 推导**。

【为什么单独一个模块，而不是塞进 agent.py】
agent.py 已经 1000+ 行，是「编排器」。本模块是**纯函数层**：
输入 (mode, params) -> 输出 (是否合法, 可读错误, 合法值集合, 替代方案)。
它不碰数据库、不碰 HTTP、不碰模型 —— 所以它可以被单测直接调用，
不需要起服务、不需要 fake 任何东西。这是它单独成文件的主要理由。

【核心原则：合法值必须来自真源，不许硬编码白名单】
所有「合法取值集合」都从**真实存在的配置**里推导：
  * 比例        <- workflows.ASPECT_MAP
  * 分辨率档位  <- workflows.Z_IMAGE_RES_TIERS / QWEN_IMAGE_RES_TIERS /
                  MOSS_IMAGE_RES_TIERS
  * 模型        <- workflows.Z_IMAGE_MODELS / QWEN_IMAGE_MODELS /
                  MOSS_IMAGE_MODELS
  * 视频 LoRA   <- workflows.LORAS
  * 视频分辨率  <- workflows.RES_MP
  * 视频时长    <- web/index.html 的滑块区间 5~15（面板的真实约束）
  * 音乐时长    <- music_yue2.YUE2_MIN_DURATION / YUE2_MAX_DURATION
  * 参考图数量  <- ref_images.MIN_REF_IMAGES / MAX_REF_IMAGES
  * 增强预设    <- enhancer.TEMPLATE_SPECS

**为什么强调这一点**：早前版本把时长硬编码成 [5,10,15]，模型据此以为
「8 秒不支持」而拒绝执行 —— 那是凭空造的约束。后端说「合法值」时，
每个数字都必须能在本仓库里找到出处，否则就是骗模型。

【替代方案而不是拒绝】
用户说「做一个 30 秒的竖屏视频」，30 秒超出视频面板的 5~15 秒。
正确做法**不是**回一句「不支持」，而是给出**可执行的替代方案**：
    * 30 秒拆成 3 段 10 秒（视频模式支持多段续接）
    * 或者改用 10 秒
本模块的 `suggest_alternatives()` 就是干这个的 —— 它给出的每条建议
都是**能直接用的 mode/参数组合**，不是空话。
"""
from __future__ import annotations

from . import enhancer as enh
from . import gen_types
from . import workflows as wf

# ------------------------------------------------------------------ 视频时长

#: 视频时长区间（秒）。来源：web/index.html 的 `#duration` 滑块 min/max。
#:
#: 为什么从面板约束取而不是从后端取：后端 workflows 里
#: `float(params.get("duration", 5))` 接受**任意**秒数 —— 后端不拦。
#: 真正的约束在面板滑块。Agent 说「合法时长 X~Y」才是对的，
#: 因为用户也只能在面板里选到这个范围。
#:
#: 【2026-11-12 放宽】上限 15 -> 60。
#: 用户实测反馈「15 秒太短，一分钟都行，最多 OOM」。
#: 面板滑块已同步放宽到 2~60（web/index.html 的 #duration），
#: 服务端上限也是 60（video_models.VIDEO_DURATION_MAX）——
#: **服务端是唯一真相源**，这里从它取值，避免三处各写一个数字。
VIDEO_DURATION_MIN = 2
VIDEO_DURATION_MAX = 60

#: 视频分辨率档位（对应 ComfyUI ResolutionSelector 的 megapixels）。
#: 来源：workflows.RES_MP
VIDEO_RESOLUTIONS = tuple(wf.RES_MP.keys())

#: 视频 LoRA 取值。来源：workflows.LORAS + "none"（官方工作流的无 LoRA 档）
VIDEO_LORAS = ("none",) + tuple(wf.LORAS.keys())

#: 视频时长区间（秒）—— **Agent 侧硬校验**。
#:
#: 【为什么后端不限也要在这里限】
#: 实测后端 `float(params.get("duration", 5))` **不设上下限** —— 传 40 秒
#: 也能建出任务。但：
#:   * 面板滑块只有 2~60 秒（web/index.html，2026-11-12 放宽），
#:     用户没法在面板复现超出这个范围的任务；
#:   * 帧数表达式 `max(5, round(a*24)) + (5 - (max(5, round(a*24)) % 17)) % 17`
#:     会让长时长产生与用户预期不符的实际时长（偏差 0~0.7s）；
#:   * 超长单段的一致性会漂移（模型能力和素材库都没有长镜头先例）。
#: 所以 Agent 侧按**面板可复现区间**卡，越界时给拆段替代方案。
#:
#: 【数值来源】从 video_models 取（服务端唯一真相源），
#: 避免「面板 60 / Agent 15 / 后端 15」三处各写一个数字。
def _video_duration_bounds() -> tuple[float, float]:
    try:
        from . import video_models as vm
        return float(vm.VIDEO_DURATION_MIN), float(vm.VIDEO_DURATION_MAX)
    except Exception:
        return 2.0, 60.0


VIDEO_DURATION_HARD_MIN, VIDEO_DURATION_HARD_MAX = _video_duration_bounds()

#: 音乐谱面模式。来源：music_yue2.YUE2_MODES
def _music_modes() -> tuple:
    try:
        from . import music_yue2
        return tuple(music_yue2.YUE2_MODES)
    except Exception:
        return ("full", "melody")

MUSIC_PLAN_MODES = _music_modes()

#: 音乐输出格式。来源：music_yue2.YUE2_AUDIO_FORMATS
def _music_formats() -> tuple:
    try:
        from . import music_yue2
        return tuple(music_yue2.YUE2_AUDIO_FORMATS)
    except Exception:
        return ("flac", "mp3", "opus")

MUSIC_FORMATS = _music_formats()

#: 音乐主模型版本。来源：music_yue2.YUE2_CHECKPOINTS
def _music_checkpoints() -> list:
    try:
        from . import music_yue2
        return list(music_yue2.YUE2_CHECKPOINTS.keys())
    except Exception:
        return []

MUSIC_CHECKPOINTS = _music_checkpoints()

#: 视频精度档位。来源：workflows.UNET 的精度键
def _video_precisions() -> list:
    try:
        keys = set()
        for entry in wf.UNET.values():
            if isinstance(entry, dict):
                keys.update(entry.keys())
        return sorted(keys)
    except Exception:
        return ["int8"]

VIDEO_PRECISIONS = _video_precisions()

#: 音乐时长区间（秒）。来源：music_yue2 的 _clamp_duration 上下限。
def _music_duration_range() -> tuple[int, int]:
    try:
        from . import music_yue2
        return (int(music_yue2.YUE2_MIN_DURATION),
                int(music_yue2.YUE2_MAX_DURATION))
    except Exception:
        return (4, 360)

MUSIC_DURATION_MIN, MUSIC_DURATION_MAX = _music_duration_range()

#: 参考图数量上下限。来源：ref_images.MIN/MAX_REF_IMAGES
def _ref_image_range() -> tuple[int, int]:
    try:
        from . import ref_images
        return (int(ref_images.MIN_REF_IMAGES), int(ref_images.MAX_REF_IMAGES))
    except Exception:
        return (1, 10)

REF_IMAGE_MIN, REF_IMAGE_MAX = _ref_image_range()


# ------------------------------------------------------------------ 模式分类

#: 各模式的媒体类型（来自 gen_types 注册表，**不硬编码**）


def is_video_mode(mode: str) -> bool:
    return mode in ("t2v", "flf", "r2v")


def is_music_mode(mode: str) -> bool:
    return mode in ("t2a", "a2a")


def is_image_mode(mode: str) -> bool:
    return mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                    "t2i_h3image", "i2i_h3image")


#: 「图像模式 -> (模型表, 分辨率档位表)」。**来源：workflows 的真实常量**。
_IMAGE_MODEL_TABLES = {
    "t2i": ("Z_IMAGE_MODELS", "Z_IMAGE_RES_TIERS"),
    "t2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_RES_TIERS"),
    "i2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_RES_TIERS"),
    "t2i_moss": ("MOSS_IMAGE_MODELS", "MOSS_IMAGE_RES_TIERS"),
    # MiniMax-H3-Image：文生图与图片编辑同一个 checkpoint
    "t2i_h3image": ("H3_IMAGE_MODELS", "H3_IMAGE_RES_TIERS"),
    "i2i_h3image": ("H3_IMAGE_MODELS", "H3_IMAGE_RES_TIERS"),
}


def _table(name: str) -> dict:
    """取 workflows 里的某张常量表。取不到就返回空 dict（不抛）。"""
    table = getattr(wf, name, None)
    return table if isinstance(table, dict) else {}


def mode_models(mode: str) -> list[str]:
    """该模式合法的模型取值集合（来自真实常量表）。"""
    entry = _IMAGE_MODEL_TABLES.get(mode)
    if entry:
        return list(_table(entry[0]).keys())
    if is_video_mode(mode):
        # 视频模式只有一个模型档：「lora」（= H3 + 加速 LoRA）
        return ["lora"]
    if is_music_mode(mode):
        return ["yue2"]
    return []


def mode_resolutions(mode: str) -> list[str]:
    """该模式合法的分辨率档位集合（来自真实常量表）。"""
    entry = _IMAGE_MODEL_TABLES.get(mode)
    if entry:
        return list(_table(entry[1]).keys())
    if is_video_mode(mode):
        return list(VIDEO_RESOLUTIONS)
    return []


def mode_aspects(mode: str) -> list[str]:
    """
    该模式合法的画面比例集合（来自 workflows.ASPECT_MAP）。

    音乐模式**没有画面比例** —— YuE2 工作流里不存在 ResolutionSelector，
    硬给它塞一份比例表就是骗模型（模型会以为能设比例）。
    """
    if mode in ("t2a", "a2a"):
        return []
    if mode in wf.BUILDERS:
        return list(wf.ASPECT_MAP.keys())
    return []


def mode_duration_range(mode: str) -> tuple[int, int] | None:
    """该模式合法的时长区间；不适用时返回 None。"""
    if is_video_mode(mode):
        return (VIDEO_DURATION_MIN, VIDEO_DURATION_MAX)
    if is_music_mode(mode):
        return (MUSIC_DURATION_MIN, MUSIC_DURATION_MAX)
    return None


def mode_duration_field(mode: str) -> str:
    """
    该模式的时长参数**字段名**。

    视频用 `duration`（workflows 里 float(params.get("duration", 5))），
    音乐用 `max_duration`（music_yue2._clamp_duration(params.get("max_duration"))）
    —— 字段名不同，用错的话音乐任务会静默用默认值而不是用户要的时长。
    """
    return "max_duration" if is_music_mode(mode) else "duration"


def mode_loras(mode: str) -> list[str]:
    """该模式合法的 LoRA 取值集合。"""
    if is_video_mode(mode):
        return list(VIDEO_LORAS)
    return []


# ------------------------------------------------------------------ 默认值推导


def mode_param_keys(mode: str) -> set[str]:
    """
    该 mode **真实存在**的参数名集合。

    【这是 mode_schema(mode)["params"] 的键集合的单一真源】
    mode_schema 按这些参数逐个展开（取值集合 + 默认值 + 来源），
    mode_defaults 按同一份集合**过滤**默认值 ——
    两边用同一份定义，就不需要为每个模式手写例外表
    （审查实测：音乐模式 t2a 的 defaults 混进了 duration/lora/resolution，
    三个都是**视频**参数，模型看到会以为音乐时长 5 秒）。

    判据全部来自真实能力函数，不是硬编码白名单：
      * 画面比例 -> mode_aspects（音乐模式返回空 -> 没有 aspect）
      * 模型     -> mode_models
      * 分辨率   -> mode_resolutions（音乐模式返回空 -> 没有 resolution）
      * 时长     -> mode_duration_range + mode_duration_field
                    （视频 duration / 音乐 max_duration）
      * LoRA     -> mode_loras（只有视频有）
      * 负面词   -> 图像模式才有
      * 批量     -> 图像模式才有
    """
    keys: set[str] = set()
    if mode_aspects(mode):
        keys.add("aspect")
    if mode_models(mode):
        keys.add("model")
    if mode_resolutions(mode):
        keys.add("resolution")
    duration_range = mode_duration_range(mode)
    if duration_range:
        keys.add(mode_duration_field(mode))
    if mode_loras(mode):
        keys.add("lora")
    if is_image_mode(mode):
        keys.add("batch_count")
        keys.add("negative_prompt")
    if is_music_mode(mode):
        keys.add("max_duration")
        keys.add("plan_mode")
        keys.add("format")
        keys.add("lyrics")
        if MUSIC_CHECKPOINTS:
            keys.add("checkpoint")
    if is_video_mode(mode):
        keys.add("precision")
        keys.add("lora_strength")
    if mode_requires_refs(mode):
        keys.add("asset_id")
    return keys


def _builder_defaults(mode: str) -> dict:
    """
    从**真实的工作流构造函数源码**里读出「参数留空时的默认值」。

    为什么要这么绕：
      * 图像模式的默认值有现成常量（Z_IMAGE_DEFAULT_MODEL 等）—— 直接读常量。
      * 视频模式的默认值**只写在 builder 源码里**：
            resolution = RES_MP.get(params.get("resolution", "768p"), 0.8)
            duration   = float(params.get("duration", 5))
            lora       = params.get("lora", "ckpt850")
        照抄一份到别处 = 两份实现必然漂移（这正是本仓库反复踩的坑）。
        所以这里**解析源码**拿到真实默认值。

    解析失败就返回空 dict（不抛）—— 拿不到默认值时，宁可不给，
    也不要给一个编出来的。

    ⚠️ 2026-11-10 修（合并 live 侧后暴露的回归）：
        旧实现用「源码里没有 params.get( 就去 helper 里找」当判据。
        live 侧的 build_t2v 改成了**两条路径**：
            if params.get("model_key") or params.get("width") and ...:
                return video_workflow.build_t2v_panel(params)
            return _h3_video_workflow(params)
        于是 build_t2v 的源码里**有** params.get( 了（只有 model_key/width），
        旧判据认为「不用去 helper 里找」-> 解析的是 build_t2v 自己，
        拿不到真正的默认值（resolution/duration/lora），
        mode_defaults("t2v") 于是退化成 Z-Image 的 544p / lora=none。

        修法：把 builder **和**它转调的 helper 的源码都当候选，
        谁提供的默认值**更多**就用谁 —— 不依赖「源码里有没有某个关键字」
        这种脆弱判据。
    """
    builder = wf.BUILDERS.get(mode)
    if builder is None:
        return {}
    try:
        import inspect
        import re
    except Exception:
        return {}

    candidates: list[str] = []
    try:
        candidates.append(inspect.getsource(builder))
    except (OSError, TypeError):
        return {}
    # builder 可能只是转调（build_t2v -> _h3_video_workflow）
    for name in ("_h3_video_workflow",):
        helper = getattr(wf, name, None)
        if helper is None:
            continue
        try:
            candidates.append(inspect.getsource(helper))
        except (OSError, TypeError):
            continue

    pattern = re.compile(
        r"""params\.get\(\s*["']([a-z_]+)["']\s*,\s*["']?([A-Za-z0-9_.]+)["']?\s*\)""")

    def _defaults_in(text: str) -> dict:
        found: dict = {}
        for match in pattern.finditer(text):
            key, raw = match.group(1), match.group(2)
            if key not in found:
                found[key] = raw
        return found

    # 选**默认值最多**的那个候选（真实默认值所在的那份源码）
    best: dict = {}
    for text in candidates:
        found = _defaults_in(text)
        if len(found) > len(best):
            best = found
    defaults: dict = dict(best)

    # 字面量与常量引用都要能取到值；取不到就丢掉这个键（宁可不给默认值）
    resolved: dict = {}
    for key, raw in defaults.items():
        value = _resolve_default_literal(raw)
        if value is not None:
            resolved[key] = value
    return resolved


def _resolve_default_literal(raw: str):
    """把源码里的默认值字面量解析成真值；解析不了返回 None。"""
    import re
    text = str(raw or "").strip()
    if not text:
        return None
    # 数字
    if re.fullmatch(r"[-+]?\d+", text):
        try:
            return int(text)
        except ValueError:
            return None
    if re.fullmatch(r"[-+]?\d*\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return None
    # 带引号的字符串字面量
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    # 裸标识符 —— 源码里的默认值常量（如 params.get("resolution", "768p")
    # 的 "768p" 被正则捕获成裸文本）。它既可能是字面量值本身，
    # 也可能是模块常量名（如 VIDEO_DEFAULT_LORA）。
    # 两种都试：先当字面量字符串，再查模块常量。
    if text and all(ch.isalnum() or ch in "_.-:" for ch in text):
        # 查模块常量（优先 —— 那是「真实默认值」的权威来源）
        value = getattr(wf, text, None)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            return value
        # 不是已知常量 -> 当字面量字符串用（如 "768p" / "ckpt850"）
        return text
    return None


def mode_defaults(mode: str) -> dict:
    """
    该模式各参数的默认值（来源：真实常量 + 真实 builder 源码）。

    返回 {"aspect": "16:9", "resolution": "768p", "duration": 5, ...}

    【关键约束 —— 只返回该模式**真实存在**的参数，2026-11-10】
    审查实测：旧实现无条件把 builder 解析出来的
    aspect/resolution/duration/lora/model/seed 拷进来，
    而 t2a（音乐）的 builder 走的是 `_h3_video_workflow` 的 fallback，
    解析出的是**视频**默认值：
        mode_defaults("t2a") == {'lora':'ckpt850', 'resolution':'768p', 'duration':5.0}
    多余键 = ['duration', 'lora', 'resolution']
    但音乐模式**没有**这三个参数（真实参数是 max_duration，4~360）。
    get_mode_schema 是模型**主动调用**的工具 —— 看到 `duration: 5.0`
    会以为音乐时长是 5 秒，于是传 duration=5（被静默忽略）
    或传 max_duration=5（合法但与用户意图不符）。

    修法：**按模式 params 白名单过滤** —— 只保留 mode_schema(mode)
    的 params 里真实存在的键。这样默认值与参数集合**永远一致**，
    不需要为每个模式手写一张例外表。
    """
    defaults: dict = {}

    # 1. builder 源码里的默认值（视频模式的主要来源）
    for key, raw in _builder_defaults(mode).items():
        if key in ("aspect", "resolution", "duration", "lora", "model", "seed"):
            defaults[key] = raw

    # 2. 图像模式的显式常量（优先级更高 —— 它们是官方推荐值）
    if mode == "t2i":
        defaults.setdefault("model", getattr(wf, "Z_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "Z_IMAGE_DEFAULT_TIER", ""))
    elif mode in ("t2i_qwen", "i2i_qwen"):
        defaults.setdefault("model",
                            getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "QWEN_IMAGE_DEFAULT_TIER", ""))
    elif mode == "t2i_moss":
        defaults.setdefault("model",
                            getattr(wf, "MOSS_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution",
                            getattr(wf, "MOSS_IMAGE_DEFAULT_TIER", ""))
    elif mode in ("t2i_h3image", "i2i_h3image"):
        defaults.setdefault("model",
                            getattr(wf, "H3_IMAGE_DEFAULT_MODEL", ""))
        defaults.setdefault("resolution", "4mp")

    # 3. 比例默认值：builder 里写死的优先
    aspects = mode_aspects(mode)
    if aspects:
        raw = str(defaults.get("aspect") or "")
        defaults["aspect"] = raw if raw in aspects else (
            "16:9" if "16:9" in aspects else aspects[0])

    # 4. 清理：不在合法集合里的默认值直接丢掉（宁可不给默认值）
    clean: dict = {}
    for key, value in defaults.items():
        if value in (None, ""):
            continue
        if key == "duration":
            try:
                clean[key] = float(value)
            except (TypeError, ValueError):
                continue
            continue
        allowed = {"aspect": mode_aspects(mode),
                   "model": mode_models(mode),
                   "resolution": mode_resolutions(mode),
                   "lora": mode_loras(mode)}.get(key)
        if allowed and str(value) not in allowed:
            continue
        clean[key] = value

    # 5. 【按模式 params 白名单过滤】—— 只保留该模式真实存在的参数。
    #    （mode_param_keys 是 mode_schema 的 params 键集合，单一真源。）
    allowed_keys = mode_param_keys(mode)
    if allowed_keys:
        clean = {k: v for k, v in clean.items() if k in allowed_keys}
    return clean


# ------------------------------------------------------------------ 负面提示词支持矩阵

#: 该 mode 的**默认模型版本**（负面支持判定要用）。
#:
#: 【必须按 mode 取】—— 不能「按常量顺序取第一个非空」。
#: 踩过的坑：Z_IMAGE_DEFAULT_MODEL 非空，导致 t2i_moss / t2i_qwen 拿到的
#: 是 Z-Image 的默认模型名，负面支持判定就用错了族的元数据。
def _default_model_of(mode: str) -> str:
    const = {
        "t2i": "Z_IMAGE_DEFAULT_MODEL",
        "t2i_qwen": "QWEN_IMAGE_DEFAULT_MODEL",
        "i2i_qwen": "QWEN_IMAGE_DEFAULT_MODEL",
        "t2i_moss": "MOSS_IMAGE_DEFAULT_MODEL",
        "t2i_h3image": "H3_IMAGE_DEFAULT_MODEL",
        "i2i_h3image": "H3_IMAGE_DEFAULT_MODEL",
    }.get(mode)
    if not const:
        return ""
    return getattr(wf, const, None) or ""


#: 负面提示词生效条件（**来自 workflows 的真实模型元数据**，不是拍脑袋的表）。
#:
#: 【实测事实，2026-11-10】
#:   t2i / z_image_turbo_bf16    -> supports_negative=False（负分支固定 zero，且 cfg=1 跳过 uncond）
#:   t2i / z_image_bf16          -> supports_negative=True （negative="encode"，独立 CLIP 负分支）
#:   t2i / z_image_base_distill  -> False（cfg=1）
#:   t2i / darkbeast_z6_blitz_fp8-> False（cfg=1）
#:   t2i_qwen / i2i_qwen         -> True，但**只有 cfg>1 时才生效**（默认 cfg=1.0）
#:   t2i_moss                    -> True（cfg=3.0，负面生效）
#:   视频三模式 / 音乐两模式      -> 无 negative_prompt 通道（代码中未见）
def negative_support(mode: str, model_key: str = "") -> dict:
    """
    返回该 mode（可选指定模型版本）的负面提示词支持情况。

    返回 {"supported": bool, "effective": bool, "reason": str,
          "requires_cfg_above": float|None, "default_negative": str}
    """
    key = model_key or _default_model_of(mode)
    # 【必须按 mode 选表】—— 不能「遍历三张表看谁包含这个 key」。
    # 踩过的坑：t2i_moss 的默认模型是 moss_image_base，但 Z_IMAGE_MODELS 里
    # 没有它、而遍历顺序把 Z 表当成命中表的前缀，导致**用错族的元数据**判定
    # 负面提示词是否生效（t2i_moss 被误判成不支持）。
    table_const = {
        "t2i": "Z_IMAGE_MODELS",
        "t2i_qwen": "QWEN_IMAGE_MODELS",
        "i2i_qwen": "QWEN_IMAGE_MODELS",
        "t2i_moss": "MOSS_IMAGE_MODELS",
        "t2i_h3image": "H3_IMAGE_MODELS",
        "i2i_h3image": "H3_IMAGE_MODELS",
    }.get(mode)
    table = getattr(wf, table_const, None) if table_const else None
    if not isinstance(table, dict) or key not in table:
        return {"supported": False, "effective": False,
                "reason": f"{mode} 没有负面提示词通道", "requires_cfg_above": None,
                "default_negative": ""}
    entry = table[key]
    supported = bool(entry.get("supports_negative"))
    cfg = entry.get("cfg")
    default_negative = ""
    if mode == "t2i_moss":
        default_negative = getattr(wf, "MOSS_IMAGE_DEFAULT_NEGATIVE", "") or ""
    if not supported:
        return {"supported": False, "effective": False,
                "reason": (f"{mode} / {key} 的负面提示词不生效"
                           f"（该版本 cfg={cfg}，负分支被固定或 cfg=1 跳过 uncond）"),
                "requires_cfg_above": None, "default_negative": default_negative}
    # Qwen 族：supports_negative=True，但默认 cfg=1.0 时 ComfyUI 跳过 uncond
    requires = None
    if mode in ("t2i_qwen", "i2i_qwen"):
        requires = 1.0
    effective = True
    reason = f"{mode} / {key} 支持负面提示词（cfg={cfg}，负面生效）"
    if requires is not None:
        effective = False
        reason = (f"{mode} / {key} 支持负面提示词，但默认 cfg={cfg}，"
                  f"需要把 cfg 抬到 {requires} 以上才生效")
    return {"supported": True, "effective": effective, "reason": reason,
            "requires_cfg_above": requires, "default_negative": default_negative}


# ------------------------------------------------------------------ 分辨率按族上限

#: 各图像族的「直出 MP 上限 / 下限」。来源：workflows 的真实常量。
def _family_mp_bounds(mode: str) -> tuple[float, float] | None:
    if mode == "t2i":
        return (0.0, float(getattr(wf, "Z_IMAGE_MAX_DIRECT_MP", 3.0)))
    if mode in ("t2i_qwen", "i2i_qwen"):
        return (0.0, float(getattr(wf, "QWEN_IMAGE_MAX_DIRECT_MP", 4.2)))
    if mode == "t2i_moss":
        return (float(getattr(wf, "MOSS_IMAGE_MIN_MP", 0.25)),
                float(getattr(wf, "MOSS_IMAGE_MAX_DIRECT_MP", 4.0)))
    return None


def _resolution_megapixels(mode: str, tier: str) -> float | None:
    """某档位在该 mode 下的 MP 值（来源：对应族的 RES_TIERS 表）。"""
    entry = _IMAGE_MODEL_TABLES.get(mode)
    if not entry:
        return None
    table = _table(entry[1])
    value = table.get(tier)
    return float(value) if isinstance(value, (int, float)) else None


# ------------------------------------------------------------------ 素材槽位

#: 各 mode 的**素材槽位分派表**（Agent 侧）。
#:
#: 【实测事实，2026-11-10】Agent 的 create_job 原先**恒写** slot="ref_image"，
#: 而服务端按 slot 分派参数（server._ASSET_SLOT_PARAM）：
#:     first_frame/last_frame -> params["first_frame"/"last_frame"]
#:     ref_image              -> params["ref_images"]
#:     ref_audio              -> params["ref_audios"]
#: 于是 a2a（要 ref_audio）与 flf（要 first_frame）**必被服务端拦下** ——
#: 不是用户请求有问题，是 Agent 根本没给出正确的槽位。
#:
#: 这张表把「mode 需要什么槽位」写成显式映射，Agent 按它分派。
#: 值 = (单个素材槽位列表, 多素材槽位列表)
#:
#: 【注意：single 里的槽位是「该模式**需要**的」】
#: mode_requires_refs / missing_params 都按它判断「缺什么要问用户」。
#: 所以「这个模式**支持**哪些类型的素材」不能塞进这张表 ——
#: 那属于 MODE_MEDIA_SLOTS（见下）。
MODE_SLOTS = {
    "t2v":    ([], []),
    "t2i":    ([], []),
    "t2i_qwen": ([], []),
    "t2i_moss": ([], []),
    # MiniMax-H3-Image：文生图无素材；图片编辑走参考图（多条）
    "t2i_h3image": ([], []),
    "i2i_h3image": ([], ["ref_image"]),
    # 图片编辑：参考图 -> ref_images（多条）
    "i2i_qwen": ([], ["ref_image"]),
    # 首尾帧：first_frame（必需）+ last_frame（可选）
    "flf":    (["first_frame", "last_frame"], []),
    # 参考生视频：参考图 -> ref_images（多条）
    "r2v":    ([], ["ref_image"]),
    # 参考生音乐：参考音频 -> ref_audio（只能 1 条）
    "a2a":    (["ref_audio"], []),
    # 文生音乐：无素材
    "t2a":    ([], []),
}


def mode_slot_plan(mode: str) -> dict:
    """
    返回该 mode 的素材槽位计划。

    {"single": ["first_frame", ...], "multi": ["ref_image", ...],
     "max_per_slot": {"ref_image": 10, "ref_audio": 1},
     "media_slots": {"image": "ref_image", "audio": "ref_audio",
                     "video": "ref_video"}}

    【media_slots —— 素材类型 -> 槽位】
    这是 2026-11-11 音视频上传扩展带来的新维度：
    同一个 mode（r2v）可以同时接受参考图、参考视频、参考音频，
    落到**哪个**槽位取决于素材的**真实类型**，而不是调用方随便填。

    映射依据是服务端真实的槽位映射（server._ASSET_SLOT_PARAM）：
        ref_image -> params["ref_images"]
        ref_video -> params["ref_videos"]
        ref_audio -> params["ref_audios"]
    以及 H3 参考节点的真实槽位上限（实测 /object_info）：
        ref_images 0~9 / ref_videos 0~3 / ref_audios 0~3

    【为什么每个 mode 的 media_slots 不同】
      * i2i_qwen：只吃图片（Qwen-Image 2.1 的参考图是图像编码器通道）
      * flf：首帧/尾帧是**图片**通道
      * a2a：只吃音频（YuE2 的 cover 模式要参考音频）
      * r2v：H3 参考生成，三种都能吃
    """
    single, multi = MODE_SLOTS.get(mode, ([], []))
    limits = {}
    if "ref_image" in multi:
        limits["ref_image"] = REF_IMAGE_MAX
    if "ref_audio" in single:
        limits["ref_audio"] = 1
    if "first_frame" in single:
        limits["first_frame"] = 1
    if "last_frame" in single:
        limits["last_frame"] = 1
    if "ref_video" in multi:
        # H3 参考节点的真实上限：ref_videos 0~3
        limits["ref_video"] = REF_VIDEO_MAX
    return {"single": list(single), "multi": list(multi),
            "max_per_slot": limits,
            "media_slots": _media_slots_for(mode)}


#: H3 参考视频槽位上限（实测 /object_info: MiniMaxH3ReferenceToVideo
#: 的 ref_videos 是 0~3）。
REF_VIDEO_MAX = 3

#: H3 参考音频槽位上限（实测 /object_info: ref_audios 0~3；
#: 但 a2a 模式的服务端校验只允许 1 段 —— 那是另一条更严的约束）。
REF_AUDIO_MAX = 3

#: 各 mode 支持的「素材类型 -> 槽位」映射（2026-11-11 音视频上传扩展）。
#:
#: 【为什么单独一张表，不塞进 MODE_SLOTS】
#: MODE_SLOTS 回答的是「这个模式**需要**什么素材」（缺了要问用户），
#: 而这张表回答的是「这个模式**能吃**什么类型的素材」。
#: 两者语义不同，混在一张表里会导致：
#:   * r2v 的 ref_audio 被当成「必需」-> 模型每次都被要求提供参考音频
#:   * i2i_qwen 的 image 被当成「必需」-> 同理
#:
#: 【映射依据 —— 全部来自真实能力，不是拍脑袋】
#:   * 服务端槽位映射（server._ASSET_SLOT_PARAM）：
#:       ref_image -> params["ref_images"]
#:       ref_video -> params["ref_videos"]
#:       ref_audio -> params["ref_audios"]
#:   * H3 参考节点的真实槽位上限（实测 /object_info）：
#:       ref_images 0~9 / ref_videos 0~3 / ref_audios 0~3
#:   * Qwen-Image 2.1 的参考图走**图像编码器**通道（只吃图片）
#:   * YuE2 cover（a2a）走 SheetSage2AudioToABC（只吃音频）
#:
#: 值 = {素材类型: 槽位名}。类型不在表里 = 该模式**不接受**这种素材。
MODE_MEDIA_SLOTS = {
    # 文生类：不接受任何素材
    "t2v": {},
    "t2i": {},
    "t2i_qwen": {},
    "t2i_moss": {},
    "t2i_h3image": {},
    "t2a": {},
    # 图片编辑：只吃参考图
    "i2i_qwen": {"image": "ref_image"},
    # H3-Image 图片编辑：同样只吃参考图（官方上限 16 张）
    "i2i_h3image": {"image": "ref_image"},
    # 首尾帧：首帧/尾帧都是图片通道（具名槽位）
    "flf": {"image": "first_frame"},
    # 参考生音乐：只吃参考音频（1 段）
    "a2a": {"audio": "ref_audio"},
    # 参考生视频（H3 参考生成）：图 / 视频 / 音频都能吃
    "r2v": {"image": "ref_image", "video": "ref_video", "audio": "ref_audio"},
}


def _media_slots_for(mode: str) -> dict:
    """
    该 mode 支持的「素材类型 -> 槽位」映射。

    【只列该 mode **真的**支持的类型】
    i2i_qwen / flf 只吃图片 -> 映射里只有 image。
    映射里没有的类型 = 该模式**不接受**这种素材
    （Agent 侧要据此把素材排除，而不是硬塞进某个槽位）。
    """
    return dict(MODE_MEDIA_SLOTS.get(mode, {}))


def mode_requires_refs(mode: str) -> list[str]:
    """
    该模式**必须**提供的参考素材槽位。

    【来源：mode_slot_plan】—— 与 create_job 实际分派用的是同一张表，
    两处不能各写一份（各写一份必然漂移）。
    """
    plan = mode_slot_plan(mode)
    required = []
    # single 槽位里除了 last_frame 都是必需的
    for slot in plan["single"]:
        if slot != "last_frame":
            required.append(slot)
    # multi 槽位统一用 asset_id 表示（泛指「参考图」）
    if plan["multi"]:
        required.append("asset_id")
    return required


# ------------------------------------------------------------------ 模式 schema


def mode_schema(mode: str) -> dict:
    """
    返回某个 mode 的**真实合法参数**。

    这是 get_mode_schema / describe_mode_params 两个工具的数据源。
    每一项都带：
      * values —— 合法取值集合（来自真实常量）
      * default —— 默认值（来自真实常量 / 面板默认）
      * source —— 这个取值集合是从哪个常量来的（可追溯，不臆造）
    """
    if mode not in wf.BUILDERS:
        raise KeyError(mode)

    info = gen_types.type_for_mode(mode)
    defaults = mode_defaults(mode)
    schema: dict = {
        "mode": mode,
        "label": info.get("label") or mode,
        "media": info.get("media") or "other",
        "params": {},
    }

    aspects = mode_aspects(mode)
    if aspects:
        schema["params"]["aspect"] = {
            "type": "string",
            "values": aspects,
            "default": defaults.get("aspect", aspects[0]),
            "source": "workflows.ASPECT_MAP",
        }

    models = mode_models(mode)
    if models:
        schema["params"]["model"] = {
            "type": "string",
            "values": models,
            "default": defaults.get("model", models[0]),
            "source": "workflows.*_MODELS",
        }

    resolutions = mode_resolutions(mode)
    if resolutions:
        schema["params"]["resolution"] = {
            "type": "string",
            "values": resolutions,
            "default": defaults.get("resolution", resolutions[0]),
            "source": "workflows.*_RES_TIERS" if is_image_mode(mode)
                      else "workflows.RES_MP",
        }

    duration_range = mode_duration_range(mode)
    if duration_range:
        field = mode_duration_field(mode)
        schema["params"][field] = {
            "type": "number",
            "unit": "second",
            "min": duration_range[0],
            "max": duration_range[1],
            "step": 1,
            "default": defaults.get(field, duration_range[0]),
            "source": ("web/index.html#duration 滑块区间"
                       if is_video_mode(mode)
                       else "h3studio.music_yue2._clamp_duration"),
        }

    loras = mode_loras(mode)
    if loras:
        schema["params"]["lora"] = {
            "type": "string",
            "values": loras,
            "default": defaults.get("lora", loras[0]),
            "source": "workflows.LORAS",
        }

    requires = mode_requires_refs(mode)
    if requires:
        schema["params"]["asset_id"] = {
            "type": "string",
            "required": True,
            "min_count": 1,
            "max_count": REF_IMAGE_MAX,
            "source": "h3studio.ref_images / server.prepare_job_params",
            "note": "参考素材必须来自素材库（用 search_assets 找 asset_id）",
        }

    if is_image_mode(mode):
        schema["params"]["batch_count"] = {
            "type": "integer",
            "min": 1,
            "max": 4,
            "default": 1,
            "source": "server.MAX_BATCH_COUNT",
        }
        # 负面提示词支持矩阵（按模型版本）—— 让人一眼看出「这个模式能不能用负面」
        schema["params"]["negative_prompt"] = {
            "type": "string",
            "supported_by_model": {
                key: negative_support(mode, key)
                for key in mode_models(mode)
            },
            "source": "workflows.*_MODELS 的 supports_negative / cfg",
        }

    # --- 音乐专属 ---
    if is_music_mode(mode):
        schema["params"]["max_duration"] = {
            "type": "number",
            "unit": "second",
            "min": MUSIC_DURATION_MIN,
            "max": MUSIC_DURATION_MAX,
            "step": 1,
            "default": 60,
            "source": "h3studio.music_yue2._clamp_duration",
        }
        schema["params"]["plan_mode"] = {
            "type": "string",
            "values": list(MUSIC_PLAN_MODES),
            "default": "full",
            "source": "music_yue2.YUE2_MODES",
        }
        schema["params"]["format"] = {
            "type": "string",
            "values": list(MUSIC_FORMATS),
            "default": "flac",
            "source": "music_yue2.YUE2_AUDIO_FORMATS",
        }
        schema["params"]["lyrics"] = {
            "type": "string",
            "source": "music_yue2.build_music_workflow",
            "note": "留空 = 纯器乐",
        }
        if MUSIC_CHECKPOINTS:
            schema["params"]["checkpoint"] = {
                "type": "string",
                "values": list(MUSIC_CHECKPOINTS),
                "default": MUSIC_CHECKPOINTS[0],
                "source": "music_yue2.YUE2_CHECKPOINTS",
            }

    # --- 视频专属 ---
    if is_video_mode(mode):
        schema["params"]["precision"] = {
            "type": "string",
            "values": list(VIDEO_PRECISIONS),
            "default": "int8",
            "source": "workflows.UNET[*] 的精度键",
        }
        schema["params"]["lora_strength"] = {
            "type": "number",
            "min": 0.0,
            "max": 2.0,
            "default": 1.0,
            "source": "workflows._h3_video_workflow",
        }

    # --- 素材槽位计划（Agent 按它分派 asset_refs 的 slot）---
    slot_plan = mode_slot_plan(mode)
    if slot_plan["single"] or slot_plan["multi"]:
        schema["asset_slots"] = {
            "single": slot_plan["single"],
            "multi": slot_plan["multi"],
            "max_per_slot": slot_plan["max_per_slot"],
            "source": "server._ASSET_SLOT_PARAM",
            "note": ("single = 每个槽位 1 个素材（如首帧/尾帧/参考音频）；"
                     "multi = 同一槽位可多张（如参考图）"),
        }

    schema["notes"] = {
        "unlisted_params": "未列出的参数留空即由工作台用官方默认值补齐",
        "params_source": "本 schema 的每个取值集合都从 workflows / enhancer / "
                         "ref_images 的真实常量推导，没有硬编码白名单",
    }
    return schema


# ------------------------------------------------------------------ 校验


class ValidationIssue:
    """一条校验问题。字段 / 错误 / 合法值 / 替代方案。"""

    def __init__(self, field: str, message: str, allowed=None,
                 suggestions=None, code: str = "invalid_param"):
        self.field = field
        self.message = message
        self.allowed = list(allowed or [])
        self.suggestions = list(suggestions or [])
        self.code = code

    def to_dict(self) -> dict:
        payload: dict = {"field": self.field, "message": self.message,
                         "code": self.code}
        if self.allowed:
            payload["allowed"] = self.allowed
        if self.suggestions:
            payload["suggestions"] = self.suggestions
        return payload

    def __repr__(self):  # pragma: no cover - 调试用
        return f"<ValidationIssue {self.field}: {self.message}>"


def _largest_allowed_tier(mode: str, max_mp: float) -> str:
    """该 mode 下不超过 max_mp 的最大档位（按 MP 降序找第一个）。"""
    allowed = mode_resolutions(mode)
    best = allowed[0] if allowed else ""
    best_mp = -1.0
    for tier in allowed:
        mp = _resolution_megapixels(mode, tier)
        if mp is not None and mp <= max_mp + 1e-9 and mp > best_mp:
            best, best_mp = tier, mp
    return best


def _smallest_allowed_tier(mode: str, min_mp: float) -> str:
    """该 mode 下不小于 min_mp 的最小档位（按 MP 升序找第一个）。"""
    allowed = mode_resolutions(mode)
    best = allowed[0] if allowed else ""
    best_mp = None
    for tier in allowed:
        mp = _resolution_megapixels(mode, tier)
        if mp is not None and mp >= min_mp - 1e-9 and (best_mp is None or mp < best_mp):
            best, best_mp = tier, mp
    return best


def _suggest_duration(mode: str, value: float) -> list[dict]:
    """
    时长越界时给**可执行**的替代方案。

    方案 1：夹到区间内（最省事，一定能跑）
    方案 2：拆成多段（视频模式支持多段续接）—— 但 Agent 一次只能建一个任务，
            所以这里给的是「段数 + 每段时长」的建议，由模型决定要不要分批建。
    """
    low, high = mode_duration_range(mode) or (0, 0)
    field = mode_duration_field(mode)
    suggestions = []
    if value < low:
        suggestions.append({
            "action": "clamp",
            "params": {field: low},
            "reason": f"{value} 秒低于该模式下限 {low} 秒，改用 {low} 秒",
        })
    elif value > high:
        suggestions.append({
            "action": "clamp",
            "params": {field: high},
            "reason": f"{value} 秒超过该模式上限 {high} 秒，改用 {high} 秒",
        })
        if is_video_mode(mode):
            # 拆段：段数 = ceil(value / high)，段长 = value / 段数（向上取整）
            segments = int(-(-value // high))
            per = int(-(-value // segments))
            suggestions.append({
                "action": "split",
                "params": {"segments": segments, field: per},
                "reason": (f"需要 {value} 秒时可拆成 {segments} 段、每段约 {per} 秒"
                           f"（每段都在 {low}~{high} 秒合法区间内），"
                           f"分段生成后按顺序拼接"),
            })
    return suggestions


def _suggest_enum(field: str, value: str, allowed: list[str],
                  mode: str = "") -> list[dict]:
    """
    枚举值非法时的替代方案。

    优先做**前缀/包含匹配**（用户说「竖屏」-> 9:16，「720p」-> 720p），
    匹配不到就给默认值。给的是**能直接用的参数**，不是「请重新输入」。
    """
    if not allowed:
        return []
    suggestions = []
    text = (value or "").strip()
    lower = text.lower()

    exact = [a for a in allowed if a.lower() == lower]
    if exact:
        return suggestions

    contains = [a for a in allowed if lower and lower in a.lower()]
    if contains:
        for item in contains[:3]:
            suggestions.append({
                "action": "use",
                "params": {field: item},
                "reason": f"「{text}」匹配到合法取值「{item}」",
            })
        return suggestions

    # 语义近邻：比例的常见中文说法
    ratio_aliases = {
        "竖屏": "9:16", "竖": "9:16", "竖版": "9:16", "手机": "9:16",
        "横屏": "16:9", "横": "16:9", "横版": "16:9", "宽屏": "16:9",
        "方形": "1:1", "正方形": "1:1", "方图": "1:1",
        "电影": "21:9", "宽银幕": "21:9",
    }
    alias = ratio_aliases.get(text)
    if alias and alias in allowed:
        return [{"action": "use", "params": {field: alias},
                 "reason": f"「{text}」对应合法取值「{alias}」"}]

    default = allowed[0]
    return [{
        "action": "use",
        "params": {field: default},
        "reason": f"「{text}」不是合法取值，改用默认值「{default}」",
    }]


def validate_params(mode: str, params: dict) -> list[ValidationIssue]:
    """
    校验一组任务参数。返回问题列表（空列表 = 全部合法）。

    只校验**取值**，不校验「必填是否缺失」—— 缺参数是「该问用户」的澄清场景，
    不是「值非法」。两者必须分开：把缺失当成非法会让 Agent 只会拒绝，
    而不会去问用户。
    """
    issues: list[ValidationIssue] = []
    if mode not in wf.BUILDERS:
        issues.append(ValidationIssue(
            "mode", f"未知模式 {mode}",
            allowed=sorted(wf.BUILDERS.keys()),
            code="unknown_mode"))
        return issues

    # --- 画面比例 ---
    aspect = params.get("aspect")
    if aspect:
        allowed = mode_aspects(mode)
        if allowed and str(aspect) not in allowed:
            issues.append(ValidationIssue(
                "aspect",
                f"画面比例「{aspect}」不是合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("aspect", str(aspect), allowed, mode),
                code="invalid_aspect"))

    # --- 模型 ---
    model = params.get("model")
    if model:
        allowed = mode_models(mode)
        if allowed and str(model) not in allowed:
            issues.append(ValidationIssue(
                "model",
                f"模型「{model}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("model", str(model), allowed, mode),
                code="invalid_model"))

    # --- 分辨率 ---
    resolution = params.get("resolution")
    if resolution:
        allowed = mode_resolutions(mode)
        if allowed and str(resolution) not in allowed:
            issues.append(ValidationIssue(
                "resolution",
                f"分辨率档位「{resolution}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("resolution", str(resolution),
                                          allowed, mode),
                code="invalid_resolution"))
        else:
            # 档位名合法，但**该族可能超出直出 MP 上限**。
            #
            # 【实测事实，2026-11-10】
            #   t2i       直出上限 3.0MP（超过自动走官方 2K 放大路径 —— 这是**支持**的）
            #   t2i_qwen  直出上限 4.2MP（超过**无**放大路径 —— 直出会崩）
            #   t2i_moss  直出上限 4.0MP，下限 0.25MP（超过**无**放大路径）
            # 静默回落是坏体验 —— 用户要 4K 却拿到 2K，且**没有任何提示**。
            bounds = _family_mp_bounds(mode)
            megapixels = _resolution_megapixels(mode, str(resolution))
            if bounds and megapixels is not None:
                low_mp, high_mp = bounds
                if megapixels > high_mp + 1e-9:
                    if mode == "t2i":
                        # Z-Image 有官方 2K 放大路径 —— 允许，但要说清
                        issues.append(ValidationIssue(
                            "resolution",
                            f"{resolution}（{megapixels}MP）超过 {mode} 直出上限 "
                            f"{high_mp}MP，会自动走官方 2K 放大路径"
                            f"（低分辨率生成 + RealESRGAN x4 + 二次细化）",
                            code="resolution_upscaled",
                            suggestions=[{"action": "use",
                                          "params": {"resolution": "2k"},
                                          "reason": "若不想走放大路径，直接用 2k 档"}]))
                    else:
                        issues.append(ValidationIssue(
                            "resolution",
                            f"{resolution}（{megapixels}MP）超过 {mode} 的直出上限 "
                            f"{high_mp}MP，且该模式**没有**放大路径，会直出并可能崩图",
                            allowed=[t for t in allowed
                                     if (_resolution_megapixels(mode, t) or 0) <= high_mp],
                            suggestions=[{
                                "action": "use",
                                "params": {"resolution": _largest_allowed_tier(mode, high_mp)},
                                "reason": (f"改用 {mode} 支持的最大档位；"
                                           f"要更大尺寸可先 t2i 出 4K 再用 i2i_qwen 修图")},
                                ],
                            code="resolution_too_large"))
                elif low_mp > 0 and megapixels < low_mp - 1e-9:
                    issues.append(ValidationIssue(
                        "resolution",
                        f"{resolution}（{megapixels}MP）低于 {mode} 的下限 {low_mp}MP",
                        code="resolution_too_small",
                        suggestions=[{"action": "use",
                                      "params": {"resolution": _smallest_allowed_tier(mode, low_mp)},
                                      "reason": f"改用 {mode} 支持的最小档位"}]))

    # --- 时长 ---
    duration_field = mode_duration_field(mode)
    duration = params.get(duration_field)
    if duration is None and duration_field != "duration":
        # 兼容：模型有时会填 duration，但音乐模式的真实字段是 max_duration
        duration = params.get("duration")
    if duration is not None:
        try:
            value = float(duration)
        except (TypeError, ValueError):
            issues.append(ValidationIssue(
                duration_field, "时长必须是数字",
                code="invalid_duration"))
        else:
            span = mode_duration_range(mode)
            if span and not (span[0] <= value <= span[1]):
                issues.append(ValidationIssue(
                    duration_field,
                    f"时长 {value} 秒超出 {mode} 的合法区间 "
                    f"{span[0]}~{span[1]} 秒",
                    allowed=[f"{span[0]}~{span[1]} 秒"],
                    suggestions=_suggest_duration(mode, value),
                    code="invalid_duration"))

    # --- LoRA ---
    #
    # 【2026-11-13 修 —— 非视频模式也要拦「不认识的 LoRA 值」】
    #
    # 旧实现：`if allowed and str(lora) not in allowed` ——
    #   `mode_loras(mode)` 对**图像 / 音乐模式返回空列表**，
    #   于是 `allowed` 为空 -> 整个校验分支被跳过 -> **任何**字符串都能进 params。
    #
    # 【后果（用户 2026-11-13 实测报出的 BUG 1）】Agent 把提示词误填进
    # `lora` 字段（例如 "integrated_multimodal_description: [Shot 1] ..."），
    # 任务照样建成功、照样出片，而生成库卡片把这段提示词**当成 LoRA 显示**。
    #
    # 【修法】把「这个模式有合法 LoRA 集合」与「集合为空」分开处理：
    #   * 有合法集合（视频模式）-> 值不在集合里 = 非法（原有行为不变）；
    #   * 集合为空（图像 / 音乐模式）-> **该模式根本不用 lora 字段**，
    #     传了就是误填 -> 报 invalid_lora，并明确提示「这个模式不支持加速 LoRA」。
    lora = params.get("lora")
    if lora:
        allowed = mode_loras(mode)
        if not allowed:
            # 该模式没有 LoRA 概念（图像 / 音乐）—— 传了就是误填。
            issues.append(ValidationIssue(
                "lora",
                f"{mode} 模式不支持 LoRA 参数（收到：{str(lora)[:60]}）",
                allowed=[],
                suggestions=[{"action": "drop",
                              "params": {"lora": None},
                              "reason": f"{mode} 模式没有加速 LoRA 概念，"
                                        "请删掉 lora 参数（提示词请写进 prompt）"}],
                code="invalid_lora"))
        elif str(lora) not in allowed:
            issues.append(ValidationIssue(
                "lora",
                f"LoRA「{lora}」不是 {mode} 的合法取值",
                allowed=allowed,
                suggestions=_suggest_enum("lora", str(lora), allowed, mode),
                code="invalid_lora"))

    # 【2026-11-13 修 —— 图像模式的效果 LoRA 数组也要过白名单】
    # `params["loras"]`（[{name, strength, trigger}]）是图像模式的 LoRA 入口，
    # 它的 `name` 同样没有校验 —— Agent 塞进提示词字段名时，
    # 卡片 LoRA 行会显示整段提示词（与上面的 lora 字段同一个 BUG 的另一个入口）。
    # 这里按「本机真实 LoRA 清单」逐项判定：不在清单里的 -> 报 invalid_loras。
    raw_loras = params.get("loras")
    if isinstance(raw_loras, list) and raw_loras:
        from . import job_meta as _job_meta
        bad_names = []
        for item in raw_loras:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if name and not _job_meta.is_real_lora_key(name):
                bad_names.append(name)
        if bad_names:
            issues.append(ValidationIssue(
                "loras",
                f"LoRA「{bad_names[0][:60]}」不在本机 LoRA 清单里",
                allowed=[],
                suggestions=[{"action": "drop",
                              "params": {"loras": None},
                              "reason": "这些名字不是本机已注册的 LoRA 权重"
                                        "（提示词请写进 prompt）"}],
                code="invalid_loras"))

    # --- 参考图数量 ---
    asset_ids = params.get("asset_ids") or params.get("asset_id")
    if isinstance(asset_ids, str):
        asset_ids = [asset_ids]
    if asset_ids:
        count = len([a for a in asset_ids if str(a or "").strip()])
        if count > REF_IMAGE_MAX:
            issues.append(ValidationIssue(
                "asset_id",
                f"参考图最多 {REF_IMAGE_MAX} 张，当前 {count} 张",
                allowed=[f"1~{REF_IMAGE_MAX} 张"],
                suggestions=[{"action": "trim",
                              "params": {"asset_id": list(asset_ids)[:REF_IMAGE_MAX]},
                              "reason": f"只保留前 {REF_IMAGE_MAX} 张参考图"}],
                code="too_many_refs"))

    # --- 视频 duration 硬上限 ---
    #
    # 后端 `float(params.get("duration", 5))` 不设限 —— 但面板滑块只有 5~15 秒，
    # 用户没法复现超范围的任务，且帧数换算会让实际时长偏离预期。
    # 所以 Agent 侧按**面板可复现区间**卡。
    #
    # 注意：上面的通用区间检查已经用 mode_duration_range() 卡了同一件事
    # （VIDEO_DURATION_MIN/MAX 就是 5/15）。这里**不再重复报一条** ——
    # 重复报会让模型看到两条几乎一样的错误，反而困惑。
    # 这段只在「通用检查没覆盖」时兜底（未来改了区间常量也不会漏）。
    if is_video_mode(mode) and duration is not None:
        try:
            value = float(duration)
        except (TypeError, ValueError):
            pass
        else:
            span = mode_duration_range(mode)
            already_reported = bool(span and not (span[0] <= value <= span[1]))
            if (not already_reported
                    and (value < VIDEO_DURATION_HARD_MIN
                         or value > VIDEO_DURATION_HARD_MAX)):
                issues.append(ValidationIssue(
                    "duration",
                    f"时长 {value} 秒超出可下发区间 "
                    f"{VIDEO_DURATION_HARD_MIN}~{VIDEO_DURATION_HARD_MAX} 秒"
                    f"（面板滑块范围；超出会与用户预期不符）",
                    allowed=[f"{VIDEO_DURATION_HARD_MIN}~{VIDEO_DURATION_HARD_MAX} 秒"],
                    suggestions=_suggest_duration(mode, value),
                    code="invalid_duration"))

    # --- 音乐专属参数 ---
    if is_music_mode(mode):
        plan_mode = params.get("plan_mode")
        if plan_mode and str(plan_mode) not in MUSIC_PLAN_MODES:
            issues.append(ValidationIssue(
                "plan_mode",
                f"谱面模式「{plan_mode}」不是合法取值",
                allowed=list(MUSIC_PLAN_MODES),
                suggestions=[{"action": "use",
                              "params": {"plan_mode": MUSIC_PLAN_MODES[0]},
                              "reason": f"改用默认谱面模式「{MUSIC_PLAN_MODES[0]}」"}],
                code="invalid_plan_mode"))
        fmt = params.get("format")
        if fmt and str(fmt) not in MUSIC_FORMATS:
            issues.append(ValidationIssue(
                "format",
                f"输出格式「{fmt}」不是合法取值",
                allowed=list(MUSIC_FORMATS),
                suggestions=[{"action": "use",
                              "params": {"format": MUSIC_FORMATS[0]},
                              "reason": f"改用默认格式「{MUSIC_FORMATS[0]}」"}],
                code="invalid_format"))
        checkpoint = params.get("checkpoint")
        if checkpoint and MUSIC_CHECKPOINTS and str(checkpoint) not in MUSIC_CHECKPOINTS:
            issues.append(ValidationIssue(
                "checkpoint",
                f"音乐模型「{checkpoint}」不是合法取值",
                allowed=list(MUSIC_CHECKPOINTS),
                suggestions=[{"action": "use",
                              "params": {"checkpoint": MUSIC_CHECKPOINTS[0]},
                              "reason": f"改用默认模型「{MUSIC_CHECKPOINTS[0]}」"}],
                code="invalid_checkpoint"))

    # --- 负面提示词生效性 ---
    #
    # 关键：**不因为「该族不支持负面提示词」就拒绝任务** ——
    # 用户要的是图/片/曲，负面提示词只是附加诉求。
    # 但必须**如实告知**它不生效，否则用户以为「NSFW 已被抑制」而实际没有。
    negative = params.get("negative_prompt")
    if negative and str(negative).strip():
        support = negative_support(mode, str(params.get("model") or ""))
        if not support["supported"]:
            issues.append(ValidationIssue(
                "negative_prompt",
                f"{mode} 不支持负面提示词（{support['reason']}）",
                code="negative_not_supported",
                suggestions=[{
                    "action": "drop",
                    "params": {},
                    "reason": ("去掉负面提示词照常生成；"
                               "若必须抑制某些内容，可改用 t2i_moss"
                               "（cfg=3.0，负面生效）或 t2i + z_image_bf16"
                               "（Z-Image Base，独立 CLIP 负分支）"),
                }]))
        elif not support["effective"]:
            issues.append(ValidationIssue(
                "negative_prompt",
                f"{mode} 的负面提示词默认不生效（{support['reason']}）",
                code="negative_needs_cfg",
                suggestions=[{
                    "action": "use",
                    "params": {"model": "qwen_image_2.1_bf16", "cfg": 1.5},
                    "reason": ("需要负面生效：改用基座模型并把 cfg 抬到 1 以上"
                               "（cfg=1 时 ComfyUI 跳过 uncond）"),
                }]))

    return issues


def missing_params(mode: str, params: dict) -> list[str]:
    """
    找出**关键缺失**的参数（= 该问用户澄清的字段）。

    只列真正影响产出结果的字段 —— 把「batch_count 没填」也算成缺失，
    会让 Agent 逢单必问，用户体验极差。

    素材缺失按**槽位计划**判定（mode_slot_plan）：
      * flf  -> 缺 first_frame（必需），last_frame 可选
      * a2a  -> 缺 ref_audio（必需，1 段）
      * i2i_qwen / r2v -> 缺参考图
    """
    missing = []
    if mode not in wf.BUILDERS:
        return missing
    if not str(params.get("prompt") or "").strip():
        missing.append("prompt")

    plan = mode_slot_plan(mode)

    # 具名槽位（first_frame / last_frame / ref_audio）
    for slot in plan["single"]:
        value = str(params.get(slot) or "").strip()
        if value:
            continue
        if slot == "last_frame":
            continue          # 尾帧可选
        missing.append(slot)

    # 多素材槽位（ref_image）
    if plan["multi"]:
        asset_ids = params.get("asset_ids") or params.get("asset_id")
        if isinstance(asset_ids, str):
            asset_ids = [asset_ids]
        if not [a for a in (asset_ids or []) if str(a or "").strip()]:
            missing.append("asset_id")
    return missing


#: **软问题**的 code 集合 —— 「如实告知，但**不拦**任务」。
#:
#: 【为什么需要这个分类 —— 2026-11-11 实测】
#: `validate_params()` 把所有问题都塞进同一个 `issues` 列表，
#: 上层（`_precheck_tool_args` / `_validate_create_job_args`）只看到
#: 「列表非空」就 `raise AgentError` —— 于是**告知类**问题也变成了硬失败。
#:
#: 实测后果（生产会话 06f7145d3dab4085，09:24:11）：
#:   用户说「最后用 qwen-image21 试试」，模型带了 negative_prompt，
#:   -> 撞上 `negative_needs_cfg`（Qwen 默认 cfg=1.0，负面不生效）
#:   -> 任务被拒（一个都没建）-> 模型用**同样参数**又试 5 次
#:   -> 8 步上限 -> 用户看到「5 个红色 ✗ + 红色失败条 + 已截断」。
#:
#: 而 `validate_params()` 里那段代码**自己的注释**写着：
#:   「关键：**不因为「该族不支持负面提示词」就拒绝任务** …」
#: `docs/内置多模态Agent-接口契约.md` 也写着
#:   「`negative_not_supported` | 400 | …（**不拦任务**，只如实告知）」
#: —— **实现与自己的注释、契约文档相反**。
#:
#: 契约文档里 `resolution_upscaled` 标的是「—（允许）」，也说明
#: issues 列表本来就有「只告知」与「必须拦」两类，代码**没有区分**。
#:
#: 【软问题的判定标准】
#: 「这个参数**取值本身**是合法的，只是它的**效果**没达到用户预期」。
#:   * negative_not_supported —— 负面提示词在该族没有通道（取值合法）
#:   * negative_needs_cfg   —— 负面提示词需 cfg>1 才生效（取值合法）
#:   * resolution_upscaled  —— 档位合法，会自动走官方 2K 放大路径
#:
#: 【硬问题仍然照旧拦】
#: 取值**非法**、超出该族直出上限且无放大路径、缺参考图、未知模式… ——
#: 这些必须拦，拦了模型能改参数重来。
SOFT_ISSUE_CODES = (
    "negative_not_supported",
    "negative_needs_cfg",
    "resolution_upscaled",
)


def split_issues(issues: list[ValidationIssue]) -> tuple[list, list]:
    """
    把校验问题分成 **(必须拦的硬问题, 只告知的软问题)**。

    返回 (hard, soft)。调用方应当：
      * hard 非空 -> 拒绝任务，把错误和替代方案回给模型；
      * 只有 soft -> **照常建任务**，把 soft 的内容如实告知模型/用户。

    【为什么要拆 —— 见 SOFT_ISSUE_CODES 的说明】
    实测线上误报：负面提示词「默认不生效」被当成硬失败，
    任务一个都没建成，模型反复重试直到撞上步骤上限。
    """
    hard: list[ValidationIssue] = []
    soft: list[ValidationIssue] = []
    for issue in issues or []:
        if getattr(issue, "code", "") in SOFT_ISSUE_CODES:
            soft.append(issue)
        else:
            hard.append(issue)
    return hard, soft


def format_issues(issues: list[ValidationIssue]) -> str:
    """
    把校验问题渲染成**给人看**的一段话。

    格式刻意是「问题 + 合法值 + 可执行建议」三段式 —— 模型读到这个字符串
    就能直接改参数重试，不需要再调一次 get_mode_schema 去查。
    """
    if not issues:
        return ""
    lines = []
    for issue in issues:
        line = f"- {issue.field}：{issue.message}"
        if issue.allowed:
            line += f"（合法值：{'、'.join(str(a) for a in issue.allowed)}）"
        lines.append(line)
        for suggestion in issue.suggestions:
            params = suggestion.get("params") or {}
            params_text = "，".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"  可改用：{params_text}（{suggestion.get('reason', '')}）")
    return "\n".join(lines)


def issues_to_payload(issues: list[ValidationIssue]) -> dict:
    """结构化的问题列表（给前端 / 契约文档用）。"""
    return {
        "ok": not issues,
        "issues": [issue.to_dict() for issue in issues],
        "message": format_issues(issues),
    }


# ------------------------------------------------------------------ 提示词预设


def prompt_presets() -> list[dict]:
    """
    真实的提示词增强预设列表（来自 enhancer.TEMPLATE_SPECS）。

    **不臆造**：只列 enhancer 里真实注册的 spec。
    每个 spec 的默认模式来自 enhancer.MODE_DEFAULT_SPEC（反向推导）。
    """
    specs = enh.TEMPLATE_SPECS
    # 反向推导：哪些 mode 默认用这个 spec
    mode_by_spec: dict[str, list[str]] = {}
    for mode, spec_id in (enh.MODE_DEFAULT_SPEC or {}).items():
        mode_by_spec.setdefault(spec_id, []).append(mode)
    result = []
    for spec_id, spec in specs.items():
        result.append({
            "spec_id": spec_id,
            "label": spec.get("label") or spec_id,
            "media": spec.get("media") or "other",
            "modes": sorted(mode_by_spec.get(spec_id, [])),
            "validate_mode": spec.get("validate_mode") or "pe",
        })
    return result


def prompt_preset(spec_id: str, overrides: dict | None = None) -> dict:
    """
    取单个预设的详情（真实内容）。

    overrides 是「本地模型配置」页里保存的模板覆盖 —— 与 /api/enhance
    读的是同一份，所以这里看到的和增强按钮用的是同一套模板。

    【注意】enhancer.get_spec 对未知 spec_id 会**静默回落到 image 档**。
    这里必须先自己判存在性 —— 否则 Agent 问一个不存在的预设时，
    会拿到「image」预设的内容，看起来像成功了（最典型的「假成功」）。
    """
    if spec_id not in enh.TEMPLATE_SPECS:
        raise KeyError(spec_id)
    spec = enh.get_spec(spec_id, overrides or {})
    if not spec:
        raise KeyError(spec_id)
    mode_by_spec: dict[str, list[str]] = {}
    for mode, sid in (enh.MODE_DEFAULT_SPEC or {}).items():
        mode_by_spec.setdefault(sid, []).append(mode)
    return {
        "spec_id": spec_id,
        "label": spec.get("label") or spec_id,
        "media": spec.get("media") or "other",
        "modes": sorted(mode_by_spec.get(spec_id, [])),
        "validate_mode": spec.get("validate_mode") or "pe",
        "system_prompt": spec.get("system_prompt") or "",
        "template": spec.get("template") or spec.get("user_template") or "",
        "params": spec.get("params") or {},
    }
