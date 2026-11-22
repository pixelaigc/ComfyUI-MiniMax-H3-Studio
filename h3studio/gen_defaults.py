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


def _fixed(name: str, label: str, value, note: str = "") -> dict:
    """只读说明字段：该值由工作流固定，面板**不给可编辑控件**。

    【为什么要有这一类而不是直接删掉】
    用户的要求是「固定项要么不暴露，要么明确标注为官方固定值、不可改」。
    直接删掉会让用户不知道「这里为什么没有控件」；
    只读说明 = 明确告诉用户这个值是多少、由谁决定、为什么不能改。
    它**不会**被写进 params（``merge_into_params`` 跳过 fixed 类型）。
    """
    return {"name": name, "label": label, "type": "fixed",
            "default": value, "readonly": True, "note": note}


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
    """t2v：新面板路径（video_workflow.build_t2v_panel）的真实参数。

    【2026-11-22 —— 与 flf / r2v 改为**同一份真源**】

    原来三个视频面板各写一份字段表（t2v 一份、flf 一份、r2v 一份），
    于是必然漂移（采样控件只在 t2v 有、LoRA 清单两套、尺寸口径两套）。
    现在三者全部从 ``h3studio.video_panel_params`` 派生 ——
    同一份采样控件、同一份 LoRA 清单、同一套尺寸口径。

    【t2v 独有的两项】
      * **注意力机制** —— 对应 ``video_workflow.build_t2v_panel`` 里
        真实存在的 ``ModelAttentionBackend`` 节点。flf / r2v 的工作流
        （``h3_video_modes``）里**没有**这个节点，所以那一项只给 t2v
        （给了 flf/r2v 就是假控件）。
      * **加速 LoRA 的字段名** —— t2v 用 ``accel_lora``（单选槽位），
        flf / r2v 用 ``lora``（历史字段名，工作流两处都认）。
        字段名不同是**读侧**的历史包袱，不是「面板不一致」。
    """
    vm = _vm()
    from . import video_panel_params as vpp

    attention_options = [
        (key, meta.get("label") or key)
        for key, meta in vm.VIDEO_ATTENTION_MODES.items()
        if key not in vm.VIDEO_REMOVED_ATTENTION_MODES
    ]

    model_fields = vpp.model_fields("t2v")
    default_model = model_fields[0]["default"]

    # ---- 加速 LoRA：与 flf / r2v **同一份清单**（同一真源）----
    lora_options = vpp.accel_lora_option_pairs(default_model)
    default_lora = vpp.accel_lora_default(default_model)
    lora_note = ("与首尾帧 / 参考生视频面板**同一份清单**：本机真实存在 + "
                 "与该底模逐层实测兼容 + 非第三方蒸馏"
                 "（判据来自权重自己的 safetensors metadata，不是名单）。"
                 "显示名取自权重登记表，不自造名称。")

    fields: list = []
    fields.extend(model_fields)
    fields.append(
        _enum("attention", "注意力机制", attention_options, "kitchen",
              "comfy kitchen = 节点 ModelAttentionBackend"
              "（用户 2026-11-13 审查目标 5）"))
    fields.append(
        _enum("accel_lora", "加速 LoRA组合", lora_options, default_lora,
              lora_note))
    fields.extend(vpp.size_fields("t2v"))
    fields.append(
        _num("duration", "时长（秒）", 5.0, 2.0, 60.0, 1.0,
             "服务端上限 60 秒（= 1440 帧），超限直接报错不静默截断"))
    # ---- 采样参数：与 flf / r2v **同名同义** ----
    fields.extend(vpp.sampling_fields(default_lora))
    fields.append(vpp.precision_readonly_field())
    return fields


def _video_legacy_fields(mode: str = "flf") -> list:
    """flf / r2v：首尾帧 / 参考生视频的真实参数。

    【2026-11-22 重写 —— 与「文生视频」面板**完全同构**】

    用户驳回的原话（本轮）：
    「不还是和文生视频不一样吗。。？你到底在改啥呢，该配置的不能配置，该有的没有」

    【上一轮为什么改错了】
    2026-11-21 那一轮把采样档位 / sigma shift 改成了**只读说明**
    （``_fixed``），理由是「官方模板用的是 H3SigmaRefiner，不是可填的
    sigma shift」。那个理由只对**官方 nonebot 模板**成立，
    对**本工作台自己的 builder** 不成立 —— 本仓库的
    ``h3studio/h3_video_modes.py`` 里：

      * ``_sampling_defaults()`` 读 ``params["steps"] / ["sampler"] /
        ["scheduler"]``（第 135-137 行），
      * ``_build_common()`` 把它们写进 ``BasicScheduler``（第 454 行）与
        ``KSamplerSelect``（第 553 行），
      * ``_shift_node()`` 读 ``params["shift_video"] / ["shift_audio"]``
        并插 ``MiniMaxH3SigmaShift`` 节点（第 675-686 行）。

    ⇒ **后端一直支持这五项**，只是面板没给控件。
    上一轮把「官方模板没有这个节点」当成了「本工作台不可配」，
    于是给用户的是「采样档位（不可改）/ sigma shift（不可改）」——
    正是用户说的「该配置的不能配置」。

    【这一版的做法】
    三个面板（t2v / flf / r2v）的字段表全部改为从
    ``h3studio.video_panel_params`` 这一份真源派生：
      * 采样参数（步数 / 采样器 / 调度器 / 视频+音频 sigma shift）
        —— 五个都**可编辑**，取值来自本机 ComfyUI 真实 COMBO；
      * 加速 LoRA —— 与 t2v **同一份清单、同一套命名**
        （含「不挂」这一项）；
      * 尺寸 —— 同一套「画面比例 × MP 预算 × 百分比」，
        外加一个**只读**的「生效像素」派生项；
      * 底模精度 —— 保留为**唯一**的只读说明项，note 里写清
        「精度不是工作流槽位，换精度 = 换权重 = 换底模」。

    只有 t2v 独有「注意力机制」（那是 ``video_workflow.build_t2v_panel``
    独有的节点），flf / r2v 的工作流里**没有**这个节点 ——
    那一项不给（给了就是假控件）。
    """
    from . import video_panel_params as vpp

    # ---- 底模 ----
    model_fields = vpp.model_fields(mode)
    default_model = model_fields[0]["default"]

    # ---- 加速 LoRA：与 t2v 同一份清单（同一真源）----
    lora_options = vpp.accel_lora_option_pairs(default_model)
    default_lora = vpp.accel_lora_default(default_model)
    lora_meta = _vm().accel_lora_meta(default_lora) or {}
    default_strength = float(lora_meta.get("recommended_strength") or 1.0)
    lora_note = ("与文生视频面板**同一份清单**：本机真实存在 + 与该底模"
                 "逐层实测兼容 + 非第三方蒸馏（判据来自权重自己的 "
                 "safetensors metadata，不是名单）。"
                 "显示名取自权重登记表，不自造名称。")

    fields: list = []
    fields.extend(model_fields)
    fields.extend(vpp.size_fields(mode))
    fields.append(
        _num("duration", "时长（秒）", 5.0, 2.0, 60.0, 1.0,
             "服务端上限 60 秒（= 1440 帧 @ 24fps），超限直接报错不静默截断"))
    fields.append(
        _enum("lora", "加速 LoRA", lora_options, default_lora, lora_note))
    fields.append(
        _num("lora_strength", "加速 LoRA 强度", default_strength, 0.0, 2.0,
             0.05,
             "官方首尾帧模板用的是 0.7（minimax_h3_v1.json 节点 333）"))
    # ---- 采样参数：与文生视频**同名同义**，全部可编辑 ----
    fields.extend(vpp.sampling_fields(default_lora))
    # ---- 唯一保留的只读说明项（且写清「为什么不可改」）----
    fields.append(vpp.precision_readonly_field())
    return fields


def _image_fields(kind: str) -> list:
    """图像模式的字段表。kind ∈ z / qwen / moss / h3image"""
    wf = _wf()
    vm = _vm()
    from . import size_spec
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
        # 【PE 已外置到 WEBUI（2026-11-15 修复①）】
        # 这里**不再**有 "use_pe" 这个布尔开关 —— 它在 Agent 默认参数面板里
        # 表现为一个**独立勾选框**，与「增强」下拉里的官方 PE 选项语义打架
        # （用户明确要求：官方 PE 必须是「增强」下拉列表中的一项，
        #  点增强按钮即执行，不保留独立勾选框形式）。
        # 且工作流内已无 PE 动作，这个开关本就没有可作用的对象。
        extra = []
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
    # 【2026-11-20 修 —— 图片族也统一成「MP 预算 × 百分比」】
    # 原来图片族只有「分辨率档位」枚举（1mp / 480p / 720p / 2k / 4k …），
    # 每个 mode 一张**独立的表**（Z / QWEN / MOSS / H3_IMAGE），
    # 同一档位名在不同族下的 MP 值还不一样（4k：Z=8.3MP、QWEN/MOSS=4.0MP）。
    # 现在统一用「MP 预算 + 百分比」表达，档位仍然保留（存量兼容 +
    # 各族的官方档位表确实有意义），但**上界**由真实档位表派生。
    #
    # 【默认 MP 取「默认档位」的 MP，而不是族上限】
    # 否则管理页会显示「MP 预算 8.3」（= 上限），
    # 而实际生效的是默认档位 1mp（= 1.0 MP）—— 又是「面板说一套、
    # 产物另一套」。默认档位的 MP 才是**用户真正会拿到的**那个值。
    mp_cap = 0.0
    for value in (tiers or {}).values():
        if isinstance(value, (int, float)):
            mp_cap = max(mp_cap, float(value))
    mp_cap = round(mp_cap, 3) if mp_cap else 1.0
    default_mp = (tiers or {}).get(default_tier)
    if not isinstance(default_mp, (int, float)):
        default_mp = mp_cap
    default_mp = round(float(default_mp), 3)
    fields = [
        _enum("model", "模型版本", _model_options(table), default_model, ""),
        _enum("resolution", "分辨率档位", [(key, key) for key in tiers],
              default_tier, ""),
        _num("megapixels", "MP 预算（百万像素）", default_mp, 0.01, mp_cap, 0.01,
             f"该族真实上限 {mp_cap} MP（来自该族的真实档位表）；100% = 上限。"
             f"默认 {default_mp} MP = 默认档位 {default_tier}"),
        _num("resolution_percent", "MP 预算百分比（%）", 100.0,
             size_spec.PERCENT_MIN, size_spec.PERCENT_MAX, 1.0,
             f"相对该族真实上限（{mp_cap} MP）的百分比；100% = {mp_cap} MP"),
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
        return _video_legacy_fields(mode), True
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
#:
#: 【2026-11-21 —— flf / r2v 的说明重写】
#: 原文是「旧工作流路径：没有注意力机制与加速 LoRA 组合，
#: 只有比例 / 档位 / 时长 / 加速 LoRA 档可调。」
#: 用户指出它与上面暴露的项**自相矛盾**：
#:   * 它说「没有加速 LoRA 组合」，面板却挂着「加速 LoRA」下拉；
#:   * 它说「只有 … 档位 … 可调」，而「档位」正是本轮删掉的冲突表达。
#: 改成与实际暴露项一一对应的说明。
MODE_NOTES = {
    "t2v": "新面板路径：底模 / 注意力机制 / 加速 LoRA 组合都是**真实生效**的"
           "（工作流里会出现对应节点与权重）。",
    "flf": "首尾帧用 FL2VA 系权重。尺寸只有一套表达：画面比例 × MP 预算 × 百分比"
           "（与文生视频同一套，上限按该模式的真实面积上限）。"
           "采样档位与 sigma shift 由所选加速 LoRA 与模型内生值决定，"
           "标注为不可改；精度已经编码在权重文件里，选底模即选精度。"
           "加速 LoRA 只列本机真实存在、与该底模逐层实测兼容、"
           "且属于官方 H3 谱系的档位。",
    "r2v": "参考生视频必须用支持 Ref2VA 通道的权重。尺寸只有一套表达："
           "画面比例 × MP 预算 × 百分比（与文生视频同一套）。"
           "采样档位与 sigma shift 由所选加速 LoRA 与模型内生值决定，"
           "标注为不可改；精度已经编码在权重文件里，选底模即选精度。"
           "加速 LoRA 只列本机真实存在、与该底模逐层实测兼容、"
           "且属于官方 H3 谱系的档位。",
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


#: 已经**不再作为控件暴露**、但存量配置里可能还躺着的字段。
#:
#: 【为什么要保留而不是丢掉】
#: 用户 2026-11-21 的要求里有一条：
#:   「只从 UI 选项移除，**不删除服务端 / 配置文件中的记录**，
#:    以兼容已有历史任务 params 与已保存预设的加载与展示，
#:    避免旧任务落库 params 失真。」
#: 归一如果按「字段表里没有就丢」处理，用户在管理页配过的
#: steps / sampler / shift 会在下一次读取时**凭空消失** ——
#: 那是「配置丢了」的错觉，也是用户明确不要的那种失真。
#:
#: 【保留 ≠ 又变成控件】它们不进 ``spec()`` 的字段表 -> 面板不渲染控件；
#: 只是存储里的值原样留着，行为与之前一致（工作流本来就会按
#: 实际挂的加速 LoRA 决定这些档位）。
#:
#: 【为什么还要写清楚是哪些】保留任意脏值会让脏数据永远留在库里。
#: 这里只保留**确实存在过的历史字段名**，其它脏值照旧丢掉。
_LEGACY_PASSTHROUGH_FIELDS = frozenset({
    "resolution",        # 旧的「分辨率档位」（已删，存量值留作迁移输入）
    "precision",         # 旧的「底模精度」（server.py 从未读它）
    "steps", "sampler", "scheduler",
    "shift_video", "shift_audio",
})


def normalize_mode(mode: str, raw) -> dict:
    """
    某模式的默认参数归一（返回该模式的**完整**字段集）。

    【2026-11-20 顺序调整 —— 先迁移再归一】
    原来直接对 raw 做字段归一。改成：
      1. **先迁移**尺寸表达（把旧档位反查成 MP 预算）—— 见
         ``migrate_size_expression``；
      2. 再按字段表归一（缺失字段补默认值）。

    【为什么必须先迁移】``megapixels`` 在迁移后**已经存在于 raw 里**，
    而归一阶段对「raw 里没有的字段」会填**字段默认值**（1.0 MP）。
    顺序反了的话，存量库里只有 ``resolution="768p"`` 的记录会被填成
    1.0 MP —— 那是**编造**了用户从没配过的值（768p 在 16:9 下是 0.8 MP）。
    """
    fields, supported = _mode_fields(mode)
    if not supported:
        return {}
    source = raw if isinstance(raw, dict) else {}
    source = migrate_size_expression(mode, source)
    out: dict = {}
    for field in fields:
        name = field["name"]
        # 【只读说明字段不落库】
        # 它的 default 是「说明用的数值」（4 步 / 12.0），
        # 存进 gen_defaults 会被 merge_into_params 当成真默认值套进 params ——
        # 那就等于服务端主动写了「用户改不了的档位」，反而覆盖了
        # 「工作流按实际挂的加速 LoRA 自己选档」这条真实行为。
        # 面板靠 spec（字段表）渲染说明，不靠存储里的值。
        if field.get("type") == "fixed" or field.get("readonly"):
            continue
        if name in source:
            value = normalize_field(field, source[name])
        else:
            value = field.get("default")
        if value is None:
            continue
        out[name] = value

    # ---- 存量 legacy 字段原样保留（不当控件，也不丢值）----
    # 见 _LEGACY_PASSTHROUGH_FIELDS 的说明：丢掉会让用户以为
    # 「我配过的值凭空消失了」，那是用户明确不要的失真。
    for name, value in source.items():
        if name in out or name not in _LEGACY_PASSTHROUGH_FIELDS:
            continue
        if _is_empty(value):
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


# ------------------------------------------------------------------ 存量迁移


def migrate_size_expression(mode: str, values: dict) -> dict:
    """
    把**存量**默认参数迁移到「MP 预算 × 百分比」表达。

    【为什么需要迁移】
    存量库里存的是旧表达：
      * 视频旧面板（flf / r2v）：``resolution`` = "544p" / "768p"
        （那两档是旧界面 ``#resSeg`` 的遗留，不是真实能力上限）；
      * 视频新面板（t2v）：``megapixels`` = 0.4 / 1.0（有 MP，但**没有百分比**）；
      * 图片族：``resolution`` = "1mp" / "480p" / "2k" …（各族的独立表）。

    迁移后的统一表达是 **MP 预算 + 百分比**。迁移规则：

      1. 存量里**已有** ``megapixels`` -> 保留（它就是 MP 预算）；
         缺的 ``resolution_percent`` 补 100（= 该模式真实上限的百分比
         语义下，100% 意味着「用满这个 MP 预算」，不改变原有像素）；
      2. 存量里**只有** ``resolution`` 档位、没有 ``megapixels``
         -> 用 ``size_spec`` 把档位反查成 MP 预算写进 ``megapixels``，
         ``resolution_percent`` 补 100；
      3. 档位反查不到（脏值）-> ``megapixels`` 留空，由字段默认值兜底。

    【为什么在**读取时**迁移而不是只做一次性脚本】
    一次性脚本会漏掉：别的机器上的库、手工导入的库、以及未来从旧版本
    升级的库。读取时迁移 = 任何来源的存量数据都会自动变成新表达，
    且**幂等**（迁移后再迁移不会变）。
    """
    if not isinstance(values, dict) or not values:
        return values
    out = dict(values)
    try:
        from . import size_spec
    except Exception:
        return out

    # 已经有 MP 预算 -> 只补百分比
    if out.get("megapixels") not in (None, ""):
        if out.get("resolution_percent") in (None, ""):
            out["resolution_percent"] = size_spec.default_percent()
        return out

    # 只有档位 -> 反查 MP
    tier = out.get("resolution")
    if tier in (None, ""):
        return out
    aspect = str(out.get("aspect") or "16:9")
    # 【视频族优先旧口径】存量档位字符串是旧口径（544p=0.4 / 768p=0.8），
    # 必须按旧口径反查，否则用户已有的 1216×672 会变成 1344×768。
    mp = size_spec.legacy_tier_megapixels(tier)
    if mp is None:
        mp = size_spec.tier_to_megapixels(mode, tier)
    if mp is None:
        mp = size_spec.video_tier_megapixels(tier, aspect)
    if mp is None:
        return out
    out["megapixels"] = round(float(mp), 3)
    out["resolution_percent"] = size_spec.default_percent()
    return out


def migrate_all_gen_defaults(raw) -> dict:
    """对整份 ``gen_defaults`` 做尺寸表达迁移（幂等）。"""
    source = raw if isinstance(raw, dict) else {}
    out: dict = {}
    for mode, values in source.items():
        out[mode] = migrate_size_expression(mode, values)
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

#: 采样类字段（面板提交路径**不套**这些 —— 见 merge_into_params 的
#: skip_sampling 说明与 server._SAMPLING_ONLY_FIELDS）。
_SAMPLING_FIELDS = frozenset({
    "sampler", "scheduler", "steps",
    "shift_video", "shift_audio",
})

#: **历史上出现过的**只读说明字段名。
#:
#: 【为什么需要这张静态清单】2026-11-21 那一轮把采样档位 / sigma shift
#: 做成了只读项，字段名是 ``sampling_fixed`` / ``shift_fixed``；
#: 2026-11-22 那一轮把它们改成**可编辑**（后端一直支持），字段名不再存在。
#: 管理页的**存量配置**里还躺着那两个键 —— 它们是「只读说明」的残留，
#: 绝不能被写进 params（``merge_into_params`` 会裁掉它们）。
#:
#: 【为什么不能靠「当前字段表」推导】当前字段表里已经没有这两个名字了，
#: 靠它裁不到存量脏值。
_LEGACY_FIXED_FIELD_NAMES = frozenset({
    "sampling_fixed",   # 2026-11-21 的「采样档位（不可改）」
    "shift_fixed",      # 2026-11-21 的「sigma shift（不可改）」
})


def merge_into_params(mode: str, params: dict, defaults: dict,
                      explicit=(), model=None, overridable=(),
                      skip_sampling=False) -> list:
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

    # 【2026-11-20 —— MP 预算与百分比**只能有一个生效值】】
    #
    # 【为什么必须在这里裁】
    # 「MP 预算」与「MP 预算百分比」是**同一件事的两种写法**，
    # 而 ``size_spec.plan_size`` 的优先级是 megapixels > percent。
    # 于是会出现：用户显式给了 ``resolution_percent=50``，
    # 但管理页默认值里的 ``megapixels=0.8`` 被套了进来
    # -> params 里两个都有 -> megapixels 胜出 -> **用户的 50% 被无视**。
    #
    # 【线上实测复现】提交 ``resolution_percent=50``，
    # 落库却是 1216×672（= 管理页默认的 0.8 MP），
    # 而不是 50% x 2.0MP = 1.0 MP 的 1376×768。
    #
    # 【修法】用户显式给了哪一个，就**只保留那一个**：
    #   * 显式给了 percent -> 不填管理页默认的 megapixels；
    #   * 显式给了 megapixels -> 不填管理页默认的 percent。
    # 没显式给任何一个 -> 两个都按管理页默认填（自洽：预算 + 100%）。
    _size_modes = ("t2v", "flf", "r2v") + tuple(_IMAGE_FAMILIES.keys())
    if mode in _size_modes:
        if "resolution_percent" in explicit and "megapixels" not in explicit:
            effective.pop("megapixels", None)
        elif "megapixels" in explicit and "resolution_percent" not in explicit:
            effective.pop("resolution_percent", None)

    # ---- 只读说明字段（type == "fixed"）绝不写进 params ----
    #
    # 【为什么必须显式裁】它们的 default 是真实数值（4 步 / 12.0 / …），
    # 如果不裁，``default_values()`` 会把它们当普通默认值填进 params ——
    # 于是「面板上明确写着不可改」的值反而变成了服务端主动写入的值，
    # 把「工作流按实际 LoRA 自己选档」这条真实行为覆盖掉了。
    #
    # 【2026-11-22 补 —— 必须连**历史遗留**的只读键一起清】
    #   原来这里只清「当前字段表里 type == fixed 的那些」。但管理页的
    #   **存量配置**里躺着上一版的 ``sampling_fixed`` / ``shift_fixed``
    #   （上一轮那两个只读项的名字）。字段表改了名，它们就成了
    #   「不在白名单里的普通键」-> 被原样写进 params。
    #   ⇒ params 里会多出 ``sampling_fixed: 4`` 这种**谁都不读**的垃圾键，
    #   而它的值 4 与真实的 steps 4 巧合相同 —— 看起来「正常」，
    #   实际是两条不同的来源，容易再次漂移。
    #   所以这里用**静态清单**把历史上出现过的只读键全部裁掉。
    fixed_names = {f["name"] for f in _mode_fields(mode)[0]
                   if f.get("type") == "fixed"}
    fixed_names |= _LEGACY_FIXED_FIELD_NAMES
    for _k in fixed_names:
        effective.pop(_k, None)

    # ---- 【2026-11-22 新增】清掉**与面板口径冲突**的遗留字段 ----
    #
    # 【用户原话（本轮）】
    # 「LORA模型都不对，并切同时存在百万像素和分辨率挡位和比例，还有底模精度，
    #   全部都是冲突的。和文生视频完全不一样好吧」
    #
    # 【实测冲突链（2026-11-22 复现）】
    #   管理页存量配置 flf = {megapixels: 0.8, resolution: "768p",
    #                        precision: "int8", steps: 4, ...}
    #   -> merge_into_params 把 megapixels / resolution / precision 全套进 params
    #   -> h3_video_size.apply_size 按 megapixels 算出 **1216×672**（0.8 MP）
    #   -> 但 params["resolution"] 仍然是 **"768p"**（= 1344×768）
    #   -> 任务详情弹窗「分辨率档位」显示 768p、「分辨率」显示 1216×672
    #   -> **同一个任务两个尺寸**，且档位那个是假的。
    #
    # 【为什么这些字段必须在这里被清掉，而不是「留着不显示」】
    #   * ``resolution`` —— 尺寸口径已收敛为「比例 × MP 预算 × 百分比」；
    #     官方模板的 ResolutionSelector 也只有比例 + MP 预算。
    #     留着它 = 留着一个**与实际像素矛盾**的档位串（而且它会一路
    #     落库、显示、导出）。
    #   * ``precision`` —— 精度编码在权重文件里，选底模即选精度；
    #     server.py 里 ``precision`` **一次都没有被读**（实测 grep 零命中），
    #     它是纯装饰。
    #
    # 【为什么不能删用户「显式传的值」】
    #   显式值是**用户这次的意图**，优先级最高 —— 不能被管理页默认值
    #   覆盖或删除。所以这里只清 ``effective``（管理页默认值），
    #   不动 ``target``（本次请求）里用户已经显式给的值。
    #   但「档位」与「MP 预算」同时存在就会矛盾 —— 所以即便用户显式传了
    #   ``resolution``，在**已有 MP 预算口径**（megapixels 或 percent）
    #   的情况下也要让档位让位（MP 预算是新口径，档位是旧口径）。
    if mode in ("t2v", "flf", "r2v"):
        # 旧口径「分辨率档位」与新口径「MP 预算」不能并存。
        # 有新口径（显式给了 megapixels / percent，或管理页配了）-> 档位让位。
        has_new_size = ("megapixels" in effective
                        or "resolution_percent" in effective
                        or "megapixels" in explicit
                        or "resolution_percent" in explicit)
        if has_new_size:
            effective.pop("resolution", None)
        # 底模精度：编码在权重里，没有独立参数
        effective.pop("precision", None)

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
    # 【2026-11-20 —— skip_sampling：面板提交路径不写采样类字段】
    #
    # 【为什么需要】上面那段是「管理页把 lora 换成 none 时，
    # 采样档位要跟着换」（20 步 / res_multistep / simple）——
    # 那是**管理页显式配置**的语义。
    #
    # 但面板提交路径（老客户端重放）有另一条契约（2026-11-12 定）：
    # **客户端没传 sampler / scheduler 时服务端不写回** ——
    # 工作流会按 lora 自己选档，服务端写回等于改老任务的画质。
    # 实测守卫：tests/test_video_sampling_params.py::
    #   test_B2_legacy_client_without_sampler_keeps_workflow_default
    #
    # 两条规则冲突时：**面板提交路径听老契约**（skip_sampling=True），
    # Agent 路径听管理页语义（默认 False）——
    # Agent 是在**主动建任务**，不是在重放老任务。
    if skip_sampling:
        for _k in _SAMPLING_FIELDS:
            effective.pop(_k, None)

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
