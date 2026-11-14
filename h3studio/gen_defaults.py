# -*- coding: utf-8 -*-
"""
按**生成类型**划分的默认参数（管理页「每个生成类型分别配置默认参数」）。

【为什么单独一个模块，而不是塞进 agent_settings.py】
agent_settings.py 是「持久化 + 能力开关 + 搜索引擎」的单一真源。
而「某个生成类型**真的**有哪些参数可调」是**工作流能力**的知识：
  * 新增一个生成类型 -> 要加一份字段表；
  * 某个采样器从 ComfyUI 的白名单里消失 -> 要改这里的合法值集合。
它跟着**工作流**变，不跟着**配置**变，所以独立成文件。

【字段清单的唯一真源 —— 绝不编造参数】
每一项的合法取值都从**真实注册表**推导：
  * 视频新面板（t2v） -> video_models.VIDEO_MODELS / VIDEO_ATTENTION_MODES /
    VIDEO_ACCEL_LORA_COMBOS / VIDEO_ASPECTS / VIDEO_SAMPLERS / VIDEO_SCHEDULERS
  * 视频旧面板（flf / r2v） -> workflows.ASPECT_MAP / RES_MP / LORAS / UNET
  * 图像 -> workflows.Z_IMAGE_MODELS / *_RES_TIERS、QWEN_*、MOSS_*、h3image.H3_IMAGE_*
  * 音乐 -> music_yue2.YUE2_CHECKPOINTS / YUE2_MODES / YUE2_AUDIO_FORMATS /
    YUE2_SAMPLERS / YUE2_SCHEDULERS / YUE2_AUDIO_ENCODERS
凡是**工作流不读**的字段，一律不列 —— 列了就是骗用户
（例如 H3-Image 的 builder 不读 shift，就不给 shift 控件）。

【为什么 t2v 与 flf/r2v 的字段不一样】
t2v 走**新面板路径**（video_workflow.build_t2v_panel）：有底模 / 注意力机制 /
加速 LoRA 组合 / 总像素。
flf 与 r2v 走**旧工作流路径**（workflows._h3_video_workflow / build_r2v）：
没有注意力机制、没有加速 LoRA 组合，只有比例 / 档位 / 时长 / 加速 LoRA 档。
给它们套同一份字段表 = 会出现「配了但根本不生效」的假控件。

【兼容：t2v_defaults】
老的「t2v_defaults」（6 个字段）是本模块 t2v 字段集的**子集镜像**。
  * 读：t2v_defaults 里**显式给出**的键优先于 gen_defaults["t2v"]
  * 写：两者同步（同一份值），老客户端 / 老脚本读到的永远是最新值
"""

from __future__ import annotations

#: 文本字段的最大长度（防脏数据进库）
TEXT_MAX = 400

#: 负数提示词等长文本字段的较短上限
SHORT_TEXT_MAX = 2000


# ------------------------------------------------------------------ 懒加载注册表
#
# 【为什么懒加载】h3studio.workflows 会拉进 enhancer / music_yue2 / h3image，
# 而 agent_settings 又会被 agent.py 拉进来 —— 顶层 import 容易绕成环。
# 函数内 import 拿到的还是同一份模块对象（Python 会缓存），只是把
# 「导入时机」推迟到第一次真正用到时。

def _wf():
    from . import workflows
    return workflows


def _vm():
    from . import video_models
    return video_models


def _my():
    from . import music_yue2
    return music_yue2


def _h3i():
    from . import h3image
    return h3image


# ------------------------------------------------------------------ 字段构造


def _enum(name: str, label: str, options, default, note: str = "") -> dict:
    """枚举字段。options = [(value, label), ...] 或 [value, ...]"""
    items = []
    for item in options or []:
        if isinstance(item, (tuple, list)):
            value, text = item[0], item[1]
        else:
            value, text = item, item
        items.append({"value": value, "label": text})
    return {"name": name, "label": label, "type": "enum",
            "options": items, "default": default, "note": note}


def _num(name: str, label: str, default, low, high, step: float = 1.0,
         note: str = "") -> dict:
    return {"name": name, "label": label, "type": "number",
            "default": default, "min": low, "max": high, "step": step,
            "note": note}


def _int(name: str, label: str, default, low, high, note: str = "") -> dict:
    return {"name": name, "label": label, "type": "integer",
            "default": default, "min": low, "max": high, "step": 1,
            "note": note}


def _bool(name: str, label: str, default: bool, note: str = "") -> dict:
    return {"name": name, "label": label, "type": "boolean",
            "default": bool(default), "note": note}


def _text(name: str, label: str, default: str = "", note: str = "") -> dict:
    return {"name": name, "label": label, "type": "text",
            "default": default or "", "note": note}


def _plain(name: str, label: str, default: str = "", note: str = "") -> dict:
    return {"name": name, "label": label, "type": "string",
            "default": default or "", "note": note}


# ------------------------------------------------------------------ 各模式字段表


def _sampler_options(pool) -> list:
    return [(key, key) for key in pool]


def _model_options(table: dict) -> list:
    return [(key, (meta.get("label") or key) if isinstance(meta, dict) else key)
            for key, meta in (table or {}).items()]


def _union_samplers(table: dict) -> list:
    """一族模型表里出现过的**全部**采样器（各模型允许的并集）。"""
    seen: list[str] = []
    for meta in (table or {}).values():
        if not isinstance(meta, dict):
            continue
        for key in meta.get("samplers") or []:
            if key not in seen:
                seen.append(key)
    return seen


def _union_schedulers(table: dict) -> list:
    seen: list[str] = []
    for meta in (table or {}).values():
        if not isinstance(meta, dict):
            continue
        for key in meta.get("schedulers") or []:
            if key not in seen:
                seen.append(key)
    return seen


def _video_new_panel_fields() -> list:
    """t2v：新面板路径（video_workflow.build_t2v_panel）的真实参数。"""
    vm = _vm()
    wf = _wf()
    model_meta = vm.VIDEO_MODELS.get(vm.VIDEO_DEFAULT_MODEL) or {}
    attention_options = [
        (key, meta.get("label") or key)
        for key, meta in vm.VIDEO_ATTENTION_MODES.items()
        if key not in vm.VIDEO_REMOVED_ATTENTION_MODES
    ]
    accel_options = [
        (key, meta.get("display") or key)
        for key, meta in vm.VIDEO_ACCEL_LORA_COMBOS.items()
    ]
    aspect_options = [(key, key) for key in vm.VIDEO_ASPECTS.keys()]
    return [
        _enum("model_key", "底模", _model_options(vm.VIDEO_MODELS),
              vm.VIDEO_DEFAULT_MODEL,
              "H3 视频底模；本机缺哪个权重由服务端建任务前校验"),
        _enum("attention", "注意力机制", attention_options, "kitchen",
              "comfy kitchen = 节点 ModelAttentionBackend（用户 2026-11-13 审查目标 5）"),
        _enum("accel_lora", "加速 LoRA 组合", accel_options, "dmad_4step_critic",
              "dmad = dmad_minimax_h3_4step_full_critic（4 步蒸馏）"),
        _enum("aspect", "画面比例", aspect_options, "16:9",
              "取值与 ComfyUI ResolutionSelector 的下拉一一对应"),
        _num("megapixels", "总像素（MP）", 0.4, 0.2, 2.0, 0.1,
             "按比例换算宽高（宽高再对齐到 32 的倍数）"),
        _num("duration", "时长（秒）", 5.0, 2.0, 60.0, 1.0,
             "服务端上限 60 秒（= 1440 帧），超限直接报错不静默截断"),
        _int("steps", "采样步数", int(model_meta.get("steps", 4)), 1, 50,
             "H3 标准路径的步数（与加速 LoRA 档绑定）"),
        _enum("sampler", "采样器", _sampler_options(vm.VIDEO_SAMPLERS),
              model_meta.get("sampler", "euler"),
              "本机 ComfyUI KSamplerSelect 的真实 COMBO 清单"),
        _enum("scheduler", "调度器", _sampler_options(vm.VIDEO_SCHEDULERS),
              model_meta.get("scheduler", "beta"),
              "本机 ComfyUI BasicScheduler 的真实 COMBO 清单"),
        _num("shift_video", "视频 sigma shift", vm.VIDEO_SHIFT_DEFAULT_VIDEO,
             0.01, 100.0, 0.01, "H3 模型内生值 12.0，改动会影响出片"),
        _num("shift_audio", "音频 sigma shift", vm.VIDEO_SHIFT_DEFAULT_AUDIO,
             0.01, 100.0, 0.01, "H3 模型内生值 3.0"),
    ]


def _video_legacy_fields() -> list:
    """flf / r2v：旧工作流路径（_h3_video_workflow / build_r2v）的真实参数。"""
    vm = _vm()
    wf = _wf()
    aspect_options = [(key, key) for key in wf.ASPECT_MAP.keys()]
    precision_options = sorted(
        {key for entry in wf.UNET.values() if isinstance(entry, dict)
         for key in entry.keys()})
    lora_options = [("none", "none（官方无加速档）")] + [
        (key, key) for key in wf.LORAS.keys()]
    return [
        _enum("aspect", "画面比例", aspect_options, "16:9", ""),
        _enum("resolution", "分辨率档位",
              [(key, key) for key in wf.RES_MP.keys()], "768p",
              "544p = 0.4MP / 768p = 0.8MP"),
        _num("duration", "时长（秒）", 5.0, 2.0, 60.0, 1.0, ""),
        _enum("lora", "加速 LoRA 档", lora_options, "ckpt850",
              "none / ckpt850 / v4step600"),
        _num("lora_strength", "LoRA 强度", 1.0, 0.0, 2.0, 0.05, ""),
        _enum("precision", "底模精度", [(key, key) for key in precision_options],
              "int8", ""),
        _int("steps", "采样步数", 4, 1, 50,
             "有加速 LoRA 时 4 步 / 无加速 LoRA 时 20 步"),
        _enum("sampler", "采样器", _sampler_options(vm.VIDEO_SAMPLERS), "euler", ""),
        _enum("scheduler", "调度器", _sampler_options(vm.VIDEO_SCHEDULERS),
              "beta", ""),
        _num("shift_video", "视频 sigma shift", vm.VIDEO_SHIFT_DEFAULT_VIDEO,
             0.01, 100.0, 0.01, ""),
        _num("shift_audio", "音频 sigma shift", vm.VIDEO_SHIFT_DEFAULT_AUDIO,
             0.01, 100.0, 0.01, ""),
    ]


def _image_fields(kind: str) -> list:
    """图像模式的字段表。kind ∈ z / qwen / moss / h3image"""
    wf = _wf()
    vm = _vm()
    if kind == "z":
        table = wf.Z_IMAGE_MODELS
        tiers = wf.Z_IMAGE_RES_TIERS
        default_model = wf.Z_IMAGE_DEFAULT_MODEL
        default_tier = wf.Z_IMAGE_DEFAULT_TIER
        aspects = [(key, key) for key in wf.ASPECT_MAP.keys()]
        extra = []
    elif kind == "qwen":
        table = wf.QWEN_IMAGE_MODELS
        tiers = wf.QWEN_IMAGE_RES_TIERS
        default_model = wf.QWEN_IMAGE_DEFAULT_MODEL
        default_tier = wf.QWEN_IMAGE_DEFAULT_TIER
        aspects = [(key, key) for key in wf.ASPECT_MAP.keys()]
        extra = [_bool("use_pe", "启用官方 PE 扩写", True,
                       "Qwen-Image 官方提示词扩写（关掉则直接用原提示词）")]
    elif kind == "moss":
        table = wf.MOSS_IMAGE_MODELS
        tiers = wf.MOSS_IMAGE_RES_TIERS
        default_model = wf.MOSS_IMAGE_DEFAULT_MODEL
        default_tier = wf.MOSS_IMAGE_DEFAULT_TIER
        aspects = [(key, key) for key in wf.ASPECT_MAP.keys()]
        extra = [
            _plain("slg_single_layers", "SLG 单层（uncond 分支）", "22",
                   "官方推荐 22（0-indexed）；关掉 SLG 画面更写实但细节会崩"),
            _plain("slg_double_layers", "SLG 双层", "",
                   "官方默认留空"),
        ]
    else:  # h3image
        h3i = _h3i()
        table = h3i.H3_IMAGE_MODELS
        tiers = h3i.H3_IMAGE_RES_TIERS
        default_model = h3i.H3_IMAGE_DEFAULT_MODEL
        default_tier = h3i.H3_IMAGE_DEFAULT_TIER
        aspects = [(key, key) for key in h3i.H3_IMAGE_NATIVE_SIZES]
        extra = []
    meta = table.get(default_model) or {}
    fields = [
        _enum("model", "模型版本", _model_options(table), default_model, ""),
        _enum("resolution", "分辨率档位", [(key, key) for key in tiers],
              default_tier, ""),
        _enum("aspect", "画面比例", aspects, "16:9", ""),
        _int("steps", "采样步数", int(meta.get("steps", 8)), 1, 150,
             "该模型版本的官方推荐步数"),
        _enum("sampler", "采样器", _sampler_options(_union_samplers(table)),
              meta.get("sampler", "euler"), "该族模型表里出现过的采样器"),
        _enum("scheduler", "调度器", _sampler_options(_union_schedulers(table)),
              meta.get("scheduler", "simple"), "该族模型表里出现过的调度器"),
        _num("cfg", "CFG", float(meta.get("cfg", 1.0)), 0.0, 20.0, 0.1,
             "cfg=1 时 ComfyUI 跳过 uncond，负面提示词不生效"),
        _int("batch_count", "一次生成张数", 1, 1, 5, "服务端上限 5 张"),
    ]
    # shift：只有 builder **真的读** params["shift"] 的族才有这个控件
    if kind != "h3image":
        fields.append(_num("shift", "shift", float(meta.get("shift", 3.0)),
                           0.0, 100.0, 0.01, "该模型版本的官方值"))
    # 负面提示词：只有 supports_negative 的族才有（其余传了也会被服务端丢弃）
    if any(isinstance(m, dict) and m.get("supports_negative")
           for m in table.values()):
        fields.append(_text("negative_prompt", "负面提示词（默认）", "",
                            "留空 = 不加负面提示词"))
    fields.extend(extra)
    return fields


def _music_fields(with_audio_encoder: bool) -> list:
    """t2a / a2a：YuE2 的真实参数。"""
    my = _my()
    fields = [
        _enum("checkpoint", "音乐模型", _model_options(my.YUE2_CHECKPOINTS),
              my.YUE2_DEFAULT_CHECKPOINT, ""),
        _enum("plan_mode", "谱面模式",
              [(key, key) for key in my.YUE2_MODES], "full",
              "full = 全曲谱 / melody = 单旋律线"),
        _enum("format", "输出格式",
              [(key, key) for key in my.YUE2_AUDIO_FORMATS], "flac", ""),
        _num("max_duration", "时长上限（秒）", my.YUE2_MAX_DURATION,
             my.YUE2_MIN_DURATION, my.YUE2_MAX_DURATION, 1.0,
             "默认 900 = 让模型自然生成到结束标记"),
        _enum("sampler", "采样器", _sampler_options(my.YUE2_SAMPLERS),
              my.YUE2_SAMPLER, ""),
        _enum("scheduler", "调度器", _sampler_options(my.YUE2_SCHEDULERS),
              my.YUE2_SCHEDULER, ""),
        _int("steps", "采样步数", my.YUE2_STEPS, 1, 200, ""),
        _num("cfg", "CFG", my.YUE2_CFG, 0.0, 20.0, 0.1, ""),
        _plain("lyrics_template", "歌词结构模板", "V-C-V-C-bridge-C",
               "留空 = 纯器乐"),
    ]
    if with_audio_encoder:
        fields.append(_enum(
            "audio_encoder", "参考音频编码器",
            _model_options(my.YUE2_AUDIO_ENCODERS),
            my.YUE2_DEFAULT_AUDIO_ENCODER,
            "SheetSage2：把参考音频转成 ABC 乐谱"))
    return fields


#: 模式 -> 字段表构造器（惰性，避免模块级就把工作流全拉进来）
def _mode_fields(mode: str) -> tuple[list, bool]:
    """返回 (字段表, 是否支持配置)。不支持的模式返回 ([], False)。"""
    if mode == "t2v":
        return _video_new_panel_fields(), True
    if mode in ("flf", "r2v"):
        return _video_legacy_fields(), True
    if mode == "t2i":
        return _image_fields("z"), True
    if mode in ("t2i_qwen", "i2i_qwen"):
        return _image_fields("qwen"), True
    if mode == "t2i_moss":
        return _image_fields("moss"), True
    if mode in ("t2i_h3image", "i2i_h3image"):
        return _image_fields("h3image"), True
    if mode == "t2a":
        return _music_fields(False), True
    if mode == "a2a":
        return _music_fields(True), True
    # longfilm：只拼接已完成的视频，没有可调的生成参数
    return [], False


#: 模式的补充说明（前端显示用）
MODE_NOTES = {
    "t2v": "新面板路径：底模 / 注意力机制 / 加速 LoRA 组合都是**真实生效**的"
           "（工作流里会出现对应节点与权重）。",
    "flf": "旧工作流路径：没有注意力机制与加速 LoRA 组合，"
           "只有比例 / 档位 / 时长 / 加速 LoRA 档可调。",
    "r2v": "旧工作流路径：没有注意力机制与加速 LoRA 组合，"
           "只有比例 / 档位 / 时长 / 加速 LoRA 档可调。",
    "t2i": "Z-Image：模型版本决定步数/CFG/采样器的推荐档。",
    "t2i_qwen": "Qwen-Image 2.1：cfg=1 时负面提示词不生效（要生效需 cfg>1）。",
    "i2i_qwen": "Qwen-Image 2.1 图片编辑：参考图必须来自素材库。",
    "t2i_moss": "MOSS-Image：SLG 层数与官方推荐一致（22 / 空）。",
    "t2i_h3image": "H3-Image：6 步蒸馏版，guidance 固定 1（无负面提示词通道）。",
    "i2i_h3image": "H3-Image 图片编辑：参考图必须来自素材库。",
    "t2a": "YuE2 文生音乐：风格描述仍由提示词/结构化字段给出。",
    "a2a": "YuE2 参考生音乐：参考音频必须来自素材库。",
    "longfilm": "长片拼接只拼接**已完成的视频**，没有可调的生成参数。",
}


# ------------------------------------------------------------------ 规格表


def spec() -> dict:
    """全部模式的字段规格（mode -> {fields, supported, note}）。"""
    out: dict = {}
    for mode in _all_modes():
        fields, supported = _mode_fields(mode)
        out[mode] = {
            "fields": fields,
            "supported": supported,
            "note": MODE_NOTES.get(mode, ""),
        }
    return out


def _all_modes() -> list:
    """模式顺序 = 后端真实注册顺序 + longfilm（唯一真源：gen_types）。"""
    try:
        from . import gen_types
        modes = list(gen_types.registered_modes())
    except Exception:
        modes = []
    if "longfilm" not in modes:
        modes.append("longfilm")
    return modes


def default_values(mode: str) -> dict:
    """该模式的**内置默认值**（各字段的 default）。"""
    fields, supported = _mode_fields(mode)
    if not supported:
        return {}
    return {f["name"]: f.get("default") for f in fields
            if f.get("default") is not None}


def default_gen_defaults() -> dict:
    """全部模式的内置默认值（写库时的初始状态）。"""
    out: dict = {}
    for mode in _all_modes():
        values = default_values(mode)
        if values:
            out[mode] = values
    return out


# ------------------------------------------------------------------ 归一


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _norm_enum(field: dict, raw) -> object:
    options = field.get("options") or []
    allowed = [str(o.get("value")) for o in options]
    text = str(raw).strip()
    if text in allowed:
        # 保留选项里的**原始类型**（value 可能是数字）
        for o in options:
            if str(o.get("value")) == text:
                return o.get("value")
        return text
    return field.get("default")


def _norm_number(field: dict, raw) -> object:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return field.get("default")
    low = field.get("min")
    high = field.get("max")
    if low is not None:
        value = max(float(low), value)
    if high is not None:
        value = min(float(high), value)
    # 去掉 1.2000000000000002 这种浮点噪音
    return round(value, 6)


def _norm_integer(field: dict, raw) -> object:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return field.get("default")
    low = field.get("min")
    high = field.get("max")
    if low is not None:
        value = max(int(low), value)
    if high is not None:
        value = min(int(high), value)
    return value


def _norm_boolean(field: dict, raw) -> object:
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on", "是"):
        return True
    if text in ("false", "0", "no", "off", "否"):
        return False
    return field.get("default")


def _norm_text(field: dict, raw) -> object:
    text = str(raw if raw is not None else "").strip()
    if not text:
        # 文本字段允许为空（= 不加负面提示词），空值本身是合法配置
        return field.get("default", "")
    return text[:SHORT_TEXT_MAX]


def _norm_string(field: dict, raw) -> object:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return field.get("default", "")
    return text[:TEXT_MAX]


def normalize_field(field: dict, raw) -> object:
    """单个字段归一：非法值回落默认值，**绝不把脏值写进库**。"""
    kind = field.get("type")
    if kind == "enum":
        return _norm_enum(field, raw)
    if kind == "number":
        return _norm_number(field, raw)
    if kind == "integer":
        return _norm_integer(field, raw)
    if kind == "boolean":
        return _norm_boolean(field, raw)
    if kind == "text":
        return _norm_text(field, raw)
    return _norm_string(field, raw)


def normalize_mode(mode: str, raw) -> dict:
    """某模式的默认参数归一（返回该模式的**完整**字段集）。"""
    fields, supported = _mode_fields(mode)
    if not supported:
        return {}
    source = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for field in fields:
        name = field["name"]
        if name in source:
            value = normalize_field(field, source[name])
        else:
            value = field.get("default")
        if value is None:
            continue
        out[name] = value
    return out


def normalize_gen_defaults(raw) -> dict:
    """全部模式的默认参数归一。"""
    source = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for mode in _all_modes():
        values = normalize_mode(mode, source.get(mode))
        if values:
            out[mode] = values
    return out


# ------------------------------------------------------------------ 兼容镜像


#: 老「t2v_defaults」的字段集（历史结构，前端老脚本按这些 id 取值）
LEGACY_T2V_FIELDS = ("model_key", "attention", "accel_lora", "aspect",
                     "megapixels", "duration")


def normalize_t2v_legacy(raw) -> dict:
    """
    老结构 t2v_defaults 的归一。

    【为什么单独一套归一】老结构的字段名（model_key / accel_lora / megapixels）
    与通用字段表**不一样**（通用表里没有 megapixels，它是「总像素」）。
    但归一规则必须一致：非法值回落默认值，脏值不进库。
    """
    t2v_fields = {f["name"]: f for f in _video_new_panel_fields()}
    out: dict = {}
    for name in LEGACY_T2V_FIELDS:
        field = t2v_fields.get(name)
        if field is None:
            continue
        default = field.get("default")
        if name not in (raw if isinstance(raw, dict) else {}):
            out[name] = default
            continue
        value = normalize_field(field, raw[name])
        out[name] = default if value is None else value
    # 老结构没有的字段（步数 / 采样器 / …）也补上 —— t2v_defaults 现在是
    # gen_defaults["t2v"] 的**完整镜像**，这样「写时两者同步」才成立。
    for field in _video_new_panel_fields():
        name = field["name"]
        if name in out:
            continue
        default = field.get("default")
        if default is not None:
            out[name] = default
    return out


def merge_t2v_with_gen(gen_t2v: dict, legacy_t2v: dict,
                       explicit_keys) -> dict:
    """
    合并两份 t2v 默认值。

    规则：**t2v_defaults 里显式给出的键优先**于 gen_defaults["t2v"]。
    只有「客户端真的给了这个键」才算显式 —— 否则老客户端只 PUT
    t2v_defaults 时会把 gen_defaults 里新加的字段覆盖成默认值。
    """
    out = dict(gen_t2v or {})
    legacy = legacy_t2v or {}
    for key in explicit_keys or ():
        if key in legacy:
            out[key] = legacy[key]
    return out


# ------------------------------------------------------------------ 运行时


#: 图像模式 -> (模型表常量名, 默认模型常量名)
_IMAGE_FAMILIES = {
    "t2i": ("Z_IMAGE_MODELS", "Z_IMAGE_DEFAULT_MODEL"),
    "t2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_DEFAULT_MODEL"),
    "i2i_qwen": ("QWEN_IMAGE_MODELS", "QWEN_IMAGE_DEFAULT_MODEL"),
    "t2i_moss": ("MOSS_IMAGE_MODELS", "MOSS_IMAGE_DEFAULT_MODEL"),
    "t2i_h3image": ("H3_IMAGE_MODELS", "H3_IMAGE_DEFAULT_MODEL"),
    "i2i_h3image": ("H3_IMAGE_MODELS", "H3_IMAGE_DEFAULT_MODEL"),
}


def _family_table(mode: str) -> tuple[dict, str]:
    """该图像模式的 (模型表, 默认模型 key)。"""
    entry = _IMAGE_FAMILIES.get(mode)
    if not entry:
        return {}, ""
    wf = _wf()
    table = getattr(wf, entry[0], None)
    default_key = getattr(wf, entry[1], "") or ""
    if not isinstance(table, dict):
        table = {}
    return table, default_key


def apply_model_sampling(mode: str, values: dict, model=None) -> dict:
    """
    按**用户显式选的模型版本**修正采样参数默认值。

    【为什么要这一步】默认参数里的 steps/sampler/scheduler/shift/cfg 是
    **按该类型的默认模型**给的（Z-Image Turbo = 8 步 / cfg 1）。
    用户换成另一个模型版本时（例如 Z-Image Base 官方推荐 25 步 / cfg 4），
    继续套用旧档位会出坏图（Base 被压到 8 步 + cfg 1）。

    【但用户自己配过的值不能动】管理页里把 steps 手动改成 20 时，
    那是**明确的用户意图** —— 比模型推荐值优先。
    判据：管理页里的值与「该类型默认模型的官方值」相同 -> 视为「没配过」
    （可以让它跟着新模型走）；不同 -> 视为「配过」（不动）。

    【为什么用这个判据而不是「用户是否碰过这个字段」】配置里没有
    「哪些字段被改过」的元信息（值本身就是全部信息）——
    用「值是否等于默认模型的官方推荐值」来近似，是唯一不新增状态的做法。
    """
    out = dict(values or {})
    if not out:
        return out
    table, default_key = _family_table(mode)
    if not table:
        return out
    chosen = str(model or "").strip()
    if not chosen or chosen == default_key:
        return out
    meta = table.get(chosen)
    if not isinstance(meta, dict):
        return out
    default_meta = table.get(default_key)
    if not isinstance(default_meta, dict):
        default_meta = {}
    for key in ("steps", "sampler", "scheduler", "shift", "cfg"):
        if key not in out or meta.get(key) is None:
            continue
        current = out[key]
        base = default_meta.get(key)
        if base is not None and current != base:
            # 用户在管理页里改过这个值 -> 用户的意图优先，不跟着模型走
            continue
        out[key] = meta[key]
    return out


#: 无加速 LoRA 时的工作流档位（workflows._h3_video_workflow 的真实规则）
_LORA_FREE_VIDEO = {"steps": 20, "sampler": "res_multistep",
                    "scheduler": "simple"}


def merge_into_params(mode: str, params: dict, defaults: dict,
                      explicit=(), model=None, overridable=()) -> list:
    """
    把某模式的默认参数合进 params。

    语义与 server._merge_t2v_defaults 一致：用户（模型）显式传的值优先，
    没传的才用默认值。返回本次实际填入（or 覆盖）的字段名列表。

    【explicit】用户/模型**显式给出**的字段名集合 —— 这些字段即使
    defaults 里有值也不动（用户的值优先）。

    【overridable】「已经被工作流内置默认值填过」的字段名集合 ——
    这些字段**允许**被管理页的默认值覆盖。
    理由：工作流内置默认值是「第三优先级」，管理页配的值是「第二优先级」，
    后者必须压过前者；而用户本次显式传的值（explicit）是「第一优先级」，
    谁都不能动。

    【返回值的语义】filled = 本次写入的字段；replaced = 被覆盖的字段。
    两者合起来就是「本次实际由管理页默认值决定的字段」。
    """
    filled: list = []
    replaced: list = []
    values = (defaults or {}).get(mode)
    if not isinstance(values, dict) or not values:
        return filled + replaced
    explicit = set(explicit or ())
    overridable = set(overridable or ())
    target = params if isinstance(params, dict) else {}
    effective = dict(values)

    # 【先定最终模型版本】采样参数要跟着**实际生效**的模型走，
    # 所以必须先算出「这次到底用哪个模型」：
    #   * 用户这次显式传了 model -> 用用户的；
    #   * 管理页配了 model 且能覆盖内置默认 -> 用管理页的；
    #   * 否则用 params 里已有的。
    final_model = target.get("model")
    if "model" not in explicit:
        configured = effective.get("model")
        current = target.get("model")
        if (configured and not _is_empty(configured)
                and (current in (None, "") or "model" in overridable)):
            final_model = configured

    # 【flf / r2v 的档位随加速 LoRA 档走】workflows._h3_video_workflow 的规则：
    # 有加速 LoRA -> 4 步 / euler / beta；没有（lora=none）-> 20 步 /
    # res_multistep / simple。默认值是按 ckpt850 算的，用户把 lora 换成
    # none 时必须同步换档，否则 20 步的工作流被压成 4 步（出坏片）。
    if mode in ("flf", "r2v"):
        if str(target.get("lora") or "") == "none":
            for key, value in _LORA_FREE_VIDEO.items():
                if key not in explicit:
                    effective[key] = value

    # 【图像模式】用户显式选了别的模型版本 -> 采样参数跟随该模型的官方值
    if mode in _IMAGE_FAMILIES:
        effective = apply_model_sampling(mode, effective, final_model)

    for name, value in effective.items():
        if name in explicit:
            # 用户这次显式传了 -> 绝不覆盖
            continue
        if _is_empty(value):
            # 空字符串 = 「这一项不配」-> 不要往 params 里塞空值
            # （塞了会让工作流以为用户「显式清空」了它）。
            continue
        current = target.get(name)
        if current not in (None, ""):
            if name in overridable:
                target[name] = value
                replaced.append(name)
            continue
        target[name] = value
        filled.append(name)
    return filled + replaced


# ------------------------------------------------------------------ 前端载荷


def spec_payload() -> dict:
    """
    给前端「agent 管理」页的规格载荷。

    每个模式一项：中文标签 + 字段表（含合法取值清单）+ 当前生效值。
    前端**不硬编码**任何取值集合 —— 下拉选项全部来自这里。
    """
    try:
        from . import gen_types
    except Exception:
        gen_types = None
    out: dict = {}
    for mode, info in spec().items():
        label = mode
        media = "other"
        category = ""
        if gen_types is not None:
            meta = gen_types.type_for_mode(mode)
            label = meta.get("label") or mode
            media = meta.get("media") or "other"
            category = meta.get("category") or ""
        out[mode] = {
            "mode": mode,
            "label": label,
            "media": media,
            "category": category,
            "supported": bool(info.get("supported")),
            "note": info.get("note") or "",
            "fields": info.get("fields") or [],
        }
    return out
