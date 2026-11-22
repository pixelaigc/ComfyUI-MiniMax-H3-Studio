# -*- coding: utf-8 -*-
"""
视频面板（t2v / flf / r2v）的**参数真源** —— 三个面板共用同一份。

【用户 2026-11-22 的原话】
  「不还是和文生视频不一样吗。。？你到底在改啥呢，该配置的不能配置，该有的没有」

【这一层解决什么】
用户截图里的三个面板（文生视频 / 首尾帧生成 / 参考图生成）**不是**主生成
面板（``web/video-panel.js``），而是**内置 Agent 管理页的「默认参数」面板**
（数据源 = ``/api/agent/settings`` 的 ``gen_defaults_spec``，渲染器 =
``web/agent-manage.js`` 的 ``renderField``）。

三个面板的字段表原本是**三份手写的**：
  * t2v  -> ``gen_defaults._video_new_panel_fields()``
  * flf  -> ``gen_defaults._video_legacy_fields("flf")``
  * r2v  -> ``gen_defaults._video_legacy_fields("r2v")``

而 flf / r2v 那份把采样参数写成了 ``_fixed(...)``（只读说明），
所以用户看到的是「采样档位（不可改）/ sigma shift（不可改）/ 底模精度（不可改）」
而不是可编辑控件 —— 这正是「该配置的不能配置」。

【为什么这三项其实**可以**配 —— 逐条查证】
  * **采样步数 / 采样器 / 调度器**：``h3studio/h3_video_modes.py`` 的
    ``_sampling_defaults()`` 已经在读 ``params["steps"] / ["sampler"] /
    ["scheduler"]``（第 135-137 行），``_build_common()`` 把它们写进
    ``BasicScheduler``（第 454 行）与 ``KSamplerSelect``（第 553 行）。
    ⇒ **后端一直支持**，只是面板没给控件。取值来自
    ``video_models.VIDEO_SAMPLERS`` / ``VIDEO_SCHEDULERS`` ——
    那两张表实测自本机 ComfyUI 的 ``/object_info``
    （``KSamplerSelect.sampler_name`` 68 项 / ``BasicScheduler.scheduler`` 14 项）。
  * **sigma shift**（视频 / 音频）：``h3_video_modes._shift_node()``
    插 ``MiniMaxH3SigmaShift`` 节点，读 ``params["shift_video"] /
    ["shift_audio"]``（第 679-686 行）。⇒ **后端一直支持**。
  * **底模精度**：精度编码在权重文件里（``int8_convrot`` / ``bf16`` /
    ``pruned_w4a8``），选底模就等于选精度 —— 这一项**确实没有**独立参数。
    保留为只读说明，并在 note 里写清「哪个上游节点锁死」。

【尺寸口径 —— 为什么不能同时有「百万像素 / 分辨率挡位 / 比例 / 底模精度」】
官方首尾帧模板（nonebot ``workflows/minimax_h3_v1.json``）的尺寸表达
**只有一套**：``ResolutionSelector {aspect_ratio, megapixels, multiple: 32}``
—— 比例 + MP 预算，**没有「分辨率档位」这个东西**。
所以本模块把尺寸收敛为 **画面比例 × MP 预算 × 百分比**（与 t2v 同一套，
同一个 ``size_spec.plan_size``），并额外给一个**只读**的「生效像素」派生项
（供用户核对，不参与提交）。

【LoRA 清单 —— 为什么必须三个面板完全一致】
用户截图里 t2v 显示「DMAD 4 步（critic 版）」、flf/r2v 显示「Turbo 4 步 · ckpt850」，
二者矛盾。根因是两份清单：
  * t2v  -> ``video_models.VIDEO_ACCEL_LORA_COMBOS``（12 枚，含第三方）
  * flf/r2v -> ``accel_lora_policy.available_accel_loras()``（只官方 turbo 系 2 枚）

本模块用**同一份**判定（``accel_lora_policy.classify`` + 实测兼容性矩阵）
给三个面板产出**同一个下拉清单**，并带上
「真实文件名 + 步数 + 是否 critic/加速版 + 适用底模」四项事实。

【命名口径 —— 禁止自造名称】
显示名一律取自 ``video_models.VIDEO_ACCEL_LORAS`` / ``VIDEO_ACCEL_LORA_COMBOS``
的 ``display`` 字段（那是从权重文件名 + 官方谱系派生的既有口径），
本模块**不新增任何名字**，也不改写既有 display。
"""

from __future__ import annotations

# ------------------------------------------------------------------ 采样参数真源
#
# 【为什么在这里集中】三个面板的采样控件必须来自**同一张表**，
# 否则「文生视频能调步数、首尾帧不能」这种分裂会再次出现。
# 底层来源：
#   * VIDEO_SAMPLERS  —— 实测自本机 ComfyUI KSamplerSelect.sampler_name（68 项）
#   * VIDEO_SCHEDULERS —— 实测自本机 ComfyUI BasicScheduler.scheduler（14 项）
#   * 步数区间 —— BasicScheduler 的 int(1~10000)，面板收窄到 1~50
#   * shift 区间 —— MiniMaxH3SigmaShift 的 float 槽位


def sampling_sampler_options() -> list[tuple[str, str]]:
    """采样器下拉选项（本机 ComfyUI KSamplerSelect 的真实 COMBO 清单）。"""
    from . import video_models as vm
    return [(name, name) for name in vm.VIDEO_SAMPLERS]


def sampling_scheduler_options() -> list[tuple[str, str]]:
    """调度器下拉选项（本机 ComfyUI BasicScheduler 的真实 COMBO 清单）。"""
    from . import video_models as vm
    return [(name, name) for name in vm.VIDEO_SCHEDULERS]


def sampling_steps_bounds() -> tuple[int, int]:
    """采样步数的合法区间（与 t2v 面板滑杆同一份）。"""
    from . import video_models as vm
    return int(vm.VIDEO_STEPS_MIN), int(vm.VIDEO_STEPS_MAX)


def sampling_shift_bounds() -> tuple[float, float]:
    """sigma shift 的合法区间（与 t2v 面板滑杆同一份）。"""
    from . import video_models as vm
    return float(vm.VIDEO_SHIFT_MIN), float(vm.VIDEO_SHIFT_MAX)


def sampling_defaults_for_lora(lora_key: str | None) -> dict:
    """
    某个加速 LoRA 档位下的**推荐**采样参数。

    与工作流里的真实取值同源（``h3_video_modes._sampling_defaults``）：
      * 挂了加速 LoRA -> 该 LoRA 的 recommended_steps / sampler / scheduler；
      * 不挂（空串 / "none"）-> 20 步 / simple / res_multistep（标准多步采样）。

    【为什么不写死 4 步 / euler / beta】那只是 ckpt850 一枚的推荐值；
    换成 LightX2V 8 步档就该是 8 步。
    """
    from . import video_models as vm

    key = str(lora_key or "").strip()
    if key and key != "none":
        meta = vm.accel_lora_meta(key) or {}
        steps = meta.get("recommended_steps")
        sampler = meta.get("recommended_sampler")
        scheduler = meta.get("recommended_scheduler")
    else:
        steps = None
        sampler = None
        scheduler = None

    # ---- 【2026-11-22 修】recommended_steps == 0 不是「0 步」 ----
    #
    # 【实测】``speed_slider`` 的登记值是 ``recommended_steps: 0`` ——
    # 那是「**作者未指定**步数」的占位写法（它是个概念 LoRA，
    # 只 patch token_refiner 的 fc2，本来就不该有专属步数）。
    # 直接拿它当默认值会得到「采样步数 = 0」——
    # 那是**非法值**（BasicScheduler 的 steps 最小是 1），
    # 会让面板显示 0、提交 0、工作流按 0 跑 = 出不了片。
    #
    # 【修法】0 / 负数 / 缺失 一律视为「未指定」-> 回落到标准多步档。
    try:
        steps_int = int(steps) if steps is not None else 0
    except (TypeError, ValueError):
        steps_int = 0
    if steps_int <= 0:
        steps_int = int(vm.VIDEO_STEPS_WITHOUT_ACCEL)
        # 没指定采样器/调度器时也用标准多步档那一组（自洽）
        if not sampler:
            sampler = vm.VIDEO_SAMPLER_WITHOUT_ACCEL
        if not scheduler:
            scheduler = vm.VIDEO_SCHEDULER_WITHOUT_ACCEL

    return {
        "steps": steps_int,
        "sampler": str(sampler) if sampler
        else str(vm.VIDEO_SAMPLER_WITHOUT_ACCEL),
        "scheduler": str(scheduler) if scheduler
        else str(vm.VIDEO_SCHEDULER_WITHOUT_ACCEL),
    }


# ------------------------------------------------------------------ 加速 LoRA 真源


def accel_lora_options(model_key: str) -> list[dict]:
    """
    某个底模下**本机真实可用**的加速 LoRA 清单（三个面板共用同一份）。

    判据（全部实测，缺一不可）：
      1. 本机 ComfyUI 的模型清单里真的有这枚权重；
      2. 与该底模逐层实测兼容（含「加载节点能不能做 AdaLN 运行期重注入」）；
      3. 不是第三方蒸馏（权重 metadata 声明 ``base_model=Comfy-Org``）。

    【与主生成面板的口径一致】主生成面板的 ``/api/video-options`` 用
    ``server.py`` 里那段 ``selectable = compatible and available and not third_party``
    —— 本模块用**同一个式子**，由 ``accel_lora_policy`` 提供字节级判据。
    两边必须同口径，否则「主面板能选、Agent 面板不能选」又是分裂。

    返回 [{key, display, name, steps, kind, loader, compatible, reason, ...}]，
    ``display`` 直接取既有 registry 的 display（**不自造名称**）。
    """
    from . import accel_lora_policy as policy
    from . import video_models as vm

    out: list[dict] = []
    matrix = vm.accel_lora_compat_matrix(model_key) or {}
    for key in vm.all_accel_lora_keys():
        meta = vm.accel_lora_meta(key) or {}
        name = meta.get("name") or ""
        if not name:
            continue
        check = matrix.get(key) or {}
        if not vm.lora_available(name):
            continue
        if not check.get("compatible"):
            continue
        info = policy.classify(key, model_key)
        if info.get("third_party"):
            continue
        out.append({
            "key": key,
            # 显示名一律取既有 registry —— 禁止自造
            "display": meta.get("display") or key,
            "name": name,
            "steps": meta.get("recommended_steps"),
            "kind": info.get("kind") or "other",
            "loader": check.get("loader") or vm.detect_accel_lora_loader(name),
            "matched": check.get("matched"),
            "total": check.get("total"),
        })
    # 官方 turbo 系优先（与主面板的排序口径一致）
    out.sort(key=lambda item: (0 if item["kind"] == "official_turbo" else 1,
                                item["key"]))
    return out


def accel_lora_option_pairs(model_key: str) -> list[tuple[str, str]]:
    """加速 LoRA 下拉选项 [(value, label), ...]，含「不挂」这一项。"""
    pairs: list[tuple[str, str]] = [
        ("none", "none（不挂加速 LoRA，走标准多步采样）")]
    for item in accel_lora_options(model_key):
        pairs.append((item["key"], item["display"]))
    return pairs


def accel_lora_default(model_key: str) -> str:
    """
    该底模下的默认加速 LoRA 档位。

    与 ``gen_defaults._video_legacy_fields`` 的原口径一致：
    优先取「官方 turbo 系里与该底模完整命中的那枚」；没有就退到第一枚可用档。
    """
    items = accel_lora_options(model_key)
    official = [it for it in items if it["kind"] == "official_turbo"]
    pool = official or items
    return pool[0]["key"] if pool else "none"


# ------------------------------------------------------------------ 尺寸口径


def size_fields(mode: str) -> list[dict]:
    """
    三个面板**共用**的尺寸字段（唯一一套表达）。

    = 画面比例（enum）+ MP 预算（number）+ MP 预算百分比（number）
    + **只读**的「生效像素」派生项。

    【为什么「生效像素」是只读】它是 ``size_spec.plan_size`` 的**派生结果**
    （由比例 + 预算 + 百分比算出，再对齐 32），不是独立可写维度。
    展示它是为了让用户**当场核对**「我填的预算 = 多少像素」，
    而它**不进 params**（``type == "fixed"`` -> ``merge_into_params`` 跳过）。

    【为什么不再有「分辨率档位」】官方模板只有 ResolutionSelector 的
    比例 + MP 预算。档位是我们自己派生出来的第三套表达，与 MP 预算并列
    展示就是用户说的「全部都是冲突的」。

    【为什么不再有独立的「底模精度」】精度编码在权重文件里
    （int8_convrot / bf16 / pruned_w4a8），选底模即选精度。
    """
    from . import size_spec
    from . import video_models as vm

    cap = size_spec.mode_max_megapixels(mode)
    aspect_options = [(key, key) for key in vm.VIDEO_ASPECTS.keys()]
    default_mp = vm.VIDEO_DEFAULT_MP
    default_pct = size_spec.default_percent()

    # 只读派生项：默认档位（预算 + 100%）算出的真实像素
    derived = _derived_pixels(mode, default_mp, default_pct, "16:9")

    return [
        {"name": "aspect", "label": "画面比例", "type": "enum",
         "options": [{"value": v, "label": t} for v, t in aspect_options],
         "default": "16:9",
         "note": "与官方工作流的 ResolutionSelector.aspect_ratio 同一取值；"
                 "与 MP 预算一起换算出真实宽高（对齐 32）。"},
        {"name": "megapixels", "label": "MP 预算（百万像素）",
         "type": "number", "default": default_mp, "min": size_spec.MP_MIN,
         "max": cap, "step": 0.01,
         "note": f"该模式真实上限 {cap} MP；100% = 上限。"
                 "宽高按比例换算并对齐 32 —— 这是唯一的尺寸表达，"
                 "没有独立的「分辨率档位」。"},
        {"name": "resolution_percent", "label": "MP 预算百分比（%）",
         "type": "number", "default": default_pct,
         "min": size_spec.PERCENT_MIN, "max": size_spec.PERCENT_MAX,
         "step": 1.0,
         "note": f"相对该模式真实上限（{cap} MP）的百分比；100% = {cap} MP。"},
        {"name": "derived_pixels", "label": "生效像素（只读派生）",
         "type": "fixed", "default": derived, "readonly": True,
         "note": "由「画面比例 × MP 预算 × 百分比」换算出的真实宽高"
                 "（对齐 32 的倍数）。它是**派生结果**，不是可写维度 —— "
                 "改上面三项它就跟着变。"},
    ]

def _derived_pixels(mode: str, megapixels, percent, aspect: str) -> str:
    """算出只读派生项的显示文本（不抛异常，失败就显示「—」）。"""
    try:
        from . import size_spec
        width, height, _notes = size_spec.plan_size(
            mode, aspect=aspect, megapixels=megapixels, percent=percent)
        if width > 0 and height > 0:
            return f"{width}×{height}"
    except Exception:
        pass
    return "—"


# ------------------------------------------------------------------ 采样字段


def sampling_fields(lora_key: str | None) -> list[dict]:
    """
    三个面板**共用**的采样字段（全部可编辑）。

    = 采样步数 / 采样器 / 调度器 / 视频 sigma shift / 音频 sigma shift。

    【取值全部来自真源，没有硬编码】
      * 步数区间 -> ``video_models.VIDEO_STEPS_MIN/MAX``
      * 采样器   -> ``video_models.VIDEO_SAMPLERS``（本机 ComfyUI COMBO）
      * 调度器   -> ``video_models.VIDEO_SCHEDULERS``（本机 ComfyUI COMBO）
      * shift    -> ``video_models.VIDEO_SHIFT_MIN/MAX``
      * 默认值   -> 所选加速 LoRA 档位的 recommended_*（与工作流同源）

    【为什么这些**可以**配 —— 见本模块顶部的逐条查证】
    ``h3_video_modes._sampling_defaults`` + ``_shift_node`` 一直读这些键。
    之前面板只给 t2v 控件，是因为 flf/r2v 的字段表写成了 ``_fixed``。
    """
    from . import video_models as vm

    steps_min, steps_max = sampling_steps_bounds()
    shift_min, shift_max = sampling_shift_bounds()
    default = sampling_defaults_for_lora(lora_key)

    return [
        {"name": "steps", "label": "采样步数", "type": "integer",
         "default": default["steps"], "min": steps_min, "max": steps_max,
         "step": 1,
         "note": "与文生视频面板同一控件、同一区间。"
                 "推荐值随所选加速 LoRA 档位变化"
                 "（4 步档 / 20 步标准多步档）。"},
        {"name": "sampler", "label": "采样器", "type": "enum",
         "options": [{"value": v, "label": t}
                     for v, t in sampling_sampler_options()],
         "default": default["sampler"],
         "note": "本机 ComfyUI KSamplerSelect 的真实 COMBO 清单。"},
        {"name": "scheduler", "label": "调度器", "type": "enum",
         "options": [{"value": v, "label": t}
                     for v, t in sampling_scheduler_options()],
         "default": default["scheduler"],
         "note": "本机 ComfyUI BasicScheduler 的真实 COMBO 清单。"},
        {"name": "shift_video", "label": "视频 sigma shift", "type": "number",
         "default": vm.VIDEO_SHIFT_DEFAULT_VIDEO,
         "min": shift_min, "max": shift_max, "step": 0.01,
         "note": "H3 模型内生值 12.0；对应工作流里的 "
                 "MiniMaxH3SigmaShift 节点 shift_video 槽位。"},
        {"name": "shift_audio", "label": "音频 sigma shift", "type": "number",
         "default": vm.VIDEO_SHIFT_DEFAULT_AUDIO,
         "min": shift_min, "max": shift_max, "step": 0.01,
         "note": "H3 模型内生值 3.0；对应工作流里的 "
                 "MiniMaxH3SigmaShift 节点 shift_audio 槽位。"},
    ]


# ------------------------------------------------------------------ 底模字段


def model_fields(mode: str) -> list[dict]:
    """
    底模下拉（三个面板共用同一份清单与同一套过滤规则）。

    【过滤规则 —— 按 mode】
      * r2v -> 只列**支持 Ref2VA 通道**的权重。
        FL2VA 权重没有参考通道，用它做参考生成等于「参考没生效」
        （``video_models.model_for_mode`` 的注释与实现）。
      * flf -> 只列 **FL2VA 系**权重（保持既有行为）。
      * t2v -> 列**全部**登记的权重。

    【为什么 t2v 不过滤】``video_workflow.build_t2v_panel`` 直接用
    ``params["model_key"]`` 取权重（``model = VIDEO_MODELS[key]``），
    **任何**登记的权重都能跑 —— 包括 Ref2VA 系（它同样有 FL2VA 通道）。
    而 t2v 的字段表**原来**就是列全部的（``_model_options(vm.VIDEO_MODELS)``），
    这次统一时不该把它收窄 —— 收窄 = 「面板能选的东西变少了」，
    那是**回退**，不是修复。

    【为什么 flf 保持过滤】虽然 ``h3_video_modes.build_flf`` 事实上
    也能加载 Ref2VA 权重（实测 ``model_for_mode("flf", "ref2va_*")``
    会原样返回它），但**既有面板口径**就是「首尾帧用 FL2VA 系权重」，
    注释写明理由是「那类权重没有 I2VA 通道」。
    统一面板时**不动**这条既有口径 —— 改它属于本任务范围外的行为变更。
    """
    from . import video_models as vm

    need_ref2va = (mode == "r2v")
    #: t2v 列出全部登记权重（保持既有行为）；flf / r2v 按模式过滤。
    list_all = (mode == "t2v")
    options = []
    for key, meta in vm.VIDEO_MODELS.items():
        supports = bool(meta.get("supports_ref2va"))
        if need_ref2va and not supports:
            continue
        if not list_all and not need_ref2va and supports:
            # FL2VA 模式不用 Ref2VA 专用权重（那类权重没有 I2VA 通道）
            continue
        options.append((key, meta.get("label") or key))

    if need_ref2va:
        default_model = "ref2va_pruned_int8"
        for key in ("ref2va_pruned_int8", "ref2va_int8",
                    "hybrid_fl2va_ref2va"):
            if key in vm.VIDEO_MODELS:
                default_model = key
                break
        note = ("参考生视频必须用支持 Ref2VA 的权重 —— FL2VA 权重没有"
                "参考通道，用它等于「参考没生效」。")
    else:
        default_model = vm.VIDEO_DEFAULT_MODEL
        note = ("首尾帧用 FL2VA 系权重（它有 I2VA / FL2VA / L2VA 通道）。"
                "精度已经编码在权重文件里，选底模就等于选精度。")

    return [{"name": "model_key", "label": "底模", "type": "enum",
             "options": [{"value": v, "label": t} for v, t in options],
             "default": default_model, "note": note}]


def precision_readonly_field() -> dict:
    """
    底模精度的**只读说明**（唯一保留的 fixed 项）。

    【它确实不可改 —— 原因写在 note 里】
    精度不是工作流里的一个槽位，而是**权重文件自身的量化格式**
    （``minimax_h3_fl2va_int8_convrot`` / ``_bf16`` / ``_pruned_w4a8``）。
    上游没有「精度」节点 —— 换精度 = 换权重文件 = 换底模。
    所以它是「选底模即选精度」，不是缺失的可配置项。
    """
    return {
        "name": "precision_fixed", "label": "底模精度（随底模，不可改）",
        "type": "fixed", "default": "随底模", "readonly": True,
        "note": "精度**不是工作流里的槽位**，而是权重文件自身的量化格式"
                "（int8_convrot / bf16 / pruned_w4a8）。"
                "上游没有「精度」节点可填 —— 换精度 = 换权重 = 换上面的底模。"
                "选底模即选精度，这里没有独立的精度参数。",
    }
