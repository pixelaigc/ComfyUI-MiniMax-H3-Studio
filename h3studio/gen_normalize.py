# -*- coding: utf-8 -*-
"""生成参数的**服务端归一**（图像族 + 视频族的公共契约层）。

【为什么单独一个模块 —— 这是第 5 轮代码审查的收口项】

第 4 轮审查发现 C-5 / C-6 / C-9 / C-10 四条缺陷，根因是同一个：
**「参数归一」这件事没有唯一实现**，于是三条链路各写各的：

  1. 面板路径（POST /api/jobs）
       -> server.normalize_t2v_params   （只对 t2v / flf / r2v 跑）
  2. Agent 路径（create_job / batch_create_jobs / retry_job）
       -> server.prepare_agent_t2v_params（**只对 t2v 跑**，且无 mode 形参）
  3. 老任务重放路径（worker 里 build_workflow）
       -> 拿到什么算什么，**不归一**

结果就是：
  * 图像模式（t2i / t2i_qwen / i2i_qwen / t2i_moss / t2i_h3image）的
    steps / cfg / shift / sampler / scheduler **完全不归一** ——
    绕过面板直接调 API 传 `"sampler": "不存在"` 就会一路送到
    ComfyUI 的 KSamplerSelect，报一个用户看不懂的英文错；
  * Agent 建 flf / r2v 任务时**不跑**归一 -> 宽高不吸附、模型通道不校验；
  * 老任务重放时 params 里若没有 width/height，``build_t2v_panel``
    会产出 **0×0 的 latent**（实测确认，见 C-10）。

【本模块的契约 —— 三条硬规则】

  R1 **只归一，不新增语义**：只改「值非法」的部分。合法值**一个字节都不动**。
     这是老任务重放语义安全的根本保证 —— 历史任务 params 里的
     sampler="res_multistep" / steps=20 之类都原样保留。

  R2 **回落而不是报错**：非法值回落到该模型的官方推荐值（写在模型表里），
     并把「改了什么、为什么」记进 ``params["normalize_notes"]``。
     为什么不是报错：老任务重放路径上抛错会让历史任务**永久无法复用**，
     而「用推荐值跑」是可预期、可解释的。

  R3 **白名单来自真实模型表**：采样器 / 调度器的合法集合来自各模型自己的
     ``samplers`` / ``schedulers`` 字段（Z_IMAGE_MODELS / QWEN_IMAGE_MODELS /
     MOSS_IMAGE_MODELS / H3_IMAGE_MODELS），**不新造常量** ——
     新造就会与模型表漂移（模型表改了、白名单没改 = 又一次静默失效）。

【数值区间从哪来 —— R4：区间必须与前端滑杆**同一份真源**】
  steps / cfg / shift 的合法区间**按模型族**区分（Z-Image / Qwen-Image /
  MOSS-Image / H3-Image 四套），真源是本模块底部的
  ``IMAGE_PARAM_RANGES`` —— 前端滑杆的 min/max/step 也从这里下发
  （/api/image-options 的 ``param_ranges`` 字段）。

  【为什么必须同源 —— 这就是本次缺陷的根因】
  2026-11-16 实测：MOSS-Image 面板的「采样步数」滑杆量程是
  **1~100**（web/app.js 的 isMossMode() 分支），而本模块当时写死
  ``IMAGE_STEPS_MAX = 60``（Z-Image 的量程）-> 用户拖到 100 提交后，
  后端判「越界」-> 静默回落模型推荐值 50，
  面板显示 100、实际跑 50（队列参数卡如实显示 50）。
  根因 = **前端允许、后端不允许** —— 两套量程各写一份，必然漂移。

  修法：区间只写一份（``IMAGE_PARAM_RANGES``），前后端都读它。
  前端不再硬编码量程；后端不再用一套扁平的全局上限。
  越界值（Agent 传 9999 步、直接调 API 传 -1）仍回落到模型推荐值。
"""

from __future__ import annotations


#: 图像族**按模型族**划分的采样参数合法区间。
#:
#: 【唯一真源 —— 前后端共用】
#:   * 后端：``normalize_image_sampling()`` 用它判「越界 -> 回落」；
#:   * 前端：``/api/image-options`` 的 ``param_ranges`` 下发它，
#:     web/app.js 的 ``renderImagePanel()`` 用它设滑杆 min/max/step。
#:
#: 【为什么按族分而不是一套全局值】
#: 各族的**官方推荐**参数域差别很大：
#:   * MOSS-Image 官方 USERGUIDE：steps >= 50（面板量程给到 100）、
#:     CFG 3.0~6.0、shift 5.0；
#:   * Qwen-Image 2.1 内置 shift=0.69（面板量程 0~3、步进 0.01）、
#:     CFG 可到 0；
#:   * H3-Image 官方模板：steps=6（6 步蒸馏，上限 12）、shift 0~3；
#:   * Z-Image Turbo/Base：steps 8/25、CFG 1~8、shift 1~7。
#: 拿任何一族的量程去卡别的族，就是「用户拖得动、后端不认」。
#:
#: 【键名 = 模型族 key】
#: 与 ``/api/image-options`` 里的 ``zimage`` / ``qwen`` / ``moss`` /
#: ``h3image`` 四个子清单**同名** —— 前端按当前模式取对应那一份。
IMAGE_PARAM_RANGES: dict[str, dict[str, dict[str, float]]] = {
    # Z-Image（Turbo 8 步 / Base 25 步 / DarkBeast 15 步）
    "zimage": {
        "steps": {"min": 1, "max": 60, "step": 1},
        "cfg": {"min": 1.0, "max": 8.0, "step": 0.5},
        "shift": {"min": 1.0, "max": 7.0, "step": 0.5},
    },
    # Qwen-Image 2.1（基座 40 步 / Turbo 8 步固定 sigma 表）
    # shift 内置 0.69 -> 步进必须 0.01，否则面板显示的值与送出的值不一致
    "qwen": {
        "steps": {"min": 1, "max": 60, "step": 1},
        "cfg": {"min": 0.0, "max": 8.0, "step": 0.1},
        "shift": {"min": 0.0, "max": 3.0, "step": 0.01},
    },
    # MOSS-Image（官方 USERGUIDE：steps >= 50 / CFG 3.0~6.0 / shift 5.0）
    "moss": {
        "steps": {"min": 1, "max": 100, "step": 1},
        "cfg": {"min": 0.0, "max": 10.0, "step": 0.1},
        "shift": {"min": 0.0, "max": 10.0, "step": 0.1},
    },
    # MiniMax-H3-Image（官方模板 6 步蒸馏 / guidance 固定 1）
    "h3image": {
        "steps": {"min": 1, "max": 12, "step": 1},
        "cfg": {"min": 1.0, "max": 8.0, "step": 0.5},
        "shift": {"min": 0.0, "max": 3.0, "step": 0.1},
    },
}

#: 模型表里没有的族 -> 用 Z-Image 的量程兜底（历史行为，不放宽也不收紧）
DEFAULT_PARAM_RANGES_FAMILY = "zimage"


def param_ranges_for_family(family: str) -> dict[str, dict[str, float]]:
    """取某个模型族的采样参数合法区间（未知族回落到默认族）。"""
    return IMAGE_PARAM_RANGES.get(family) or IMAGE_PARAM_RANGES[DEFAULT_PARAM_RANGES_FAMILY]


#: 兼容旧引用（外部脚本 / 老测试按模块级常量取区间）。
#: 注意：这三个常量现在只是**Z-Image 族**的区间（历史上它们被当全局上限用，
#: 那是本次缺陷的根因）。新代码请用 ``param_ranges_for_family()``。
IMAGE_STEPS_MIN = 1
IMAGE_STEPS_MAX = IMAGE_PARAM_RANGES["zimage"]["steps"]["max"]
IMAGE_CFG_MIN = IMAGE_PARAM_RANGES["zimage"]["cfg"]["min"]
IMAGE_CFG_MAX = IMAGE_PARAM_RANGES["zimage"]["cfg"]["max"]
IMAGE_SHIFT_MIN = IMAGE_PARAM_RANGES["zimage"]["shift"]["min"]
IMAGE_SHIFT_MAX = IMAGE_PARAM_RANGES["zimage"]["shift"]["max"]


def _note(notes: list[str], field: str, raw, fallback) -> None:
    """记一条「改了什么、为什么」—— 供任务详情如实展示。"""
    notes.append(f"{field}：{raw!r} 不是合法取值，已改用 {fallback!r}")


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_int_like(value) -> bool:
    """这个值**本身**能不能解析成整数（不是「解析后落在区间内」）。"""
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


def _is_float_like(value) -> bool:
    """这个值**本身**能不能解析成浮点数。"""
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _as_float(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:  # NaN
        return default
    return out


def normalize_image_sampling(params: dict, model: dict, notes: list[str],
                            family: str | None = None) -> None:
    """就地归一**图像模式**的采样参数。

    参数
      params  要归一的 params（就地修改）
      model   该模式的模型定义（必须含 steps / cfg / shift / sampler /
              scheduler 字段；``samplers`` / ``schedulers`` 是白名单，
              缺失时视为「该模型不限制」）
      notes   归一说明收集列表（本函数 append，不清空）
      family  模型族 key（"zimage" / "qwen" / "moss" / "h3image"）——
              决定 steps / cfg / shift 的合法区间。**省略时**按 model
              里有没有 ``param_family`` 字段推断，都没有则用 Z-Image 的
              量程（历史行为）。

    【为什么放在 builder 之外、归一层之内】
    builder（build_t2i / build_qwen_t2i / build_moss_t2i）读的是
    ``params.get("sampler") or model["sampler"]`` 这种写法 ——
    它**信任** params 里的值。若归一在这里做，builder 拿到的就是干净值；
    若在 builder 里做，三份 builder 就要各改一遍（又是一次漂移）。

    【注意】本函数**不改** model 自己的推荐值 ——
    它只保证 params 里若存在「非法值」就被换掉。

    【2026-11-16 修 —— 区间按族取，不再用一套扁平全局值】
    旧实现用模块级 IMAGE_STEPS_MAX=60 / IMAGE_CFG_MIN=1.0 /
    IMAGE_SHIFT_MIN=1.0 去卡**所有**族。MOSS-Image 面板量程是
    steps 1~100 / cfg 0~10 / shift 0~10 —— 于是 steps=100 被判越界、
    静默回落 50（本次用户报的那个 BUG）。
    现在区间从 ``IMAGE_PARAM_RANGES[family]`` 取 ——
    与前端滑杆**同一份真源**（前端量程由 /api/image-options 下发）。
    """
    if not isinstance(params, dict) or not isinstance(model, dict):
        return

    # ---- 量程（按族）----
    if not family:
        family = model.get("param_family") or DEFAULT_PARAM_RANGES_FAMILY
    ranges = param_ranges_for_family(str(family))

    steps_range = ranges["steps"]
    cfg_range = ranges["cfg"]
    shift_range = ranges["shift"]

    # ---- 步数 ----
    default_steps = _as_int(model.get("steps"), 8)
    if "steps" in params and params.get("steps") is not None:
        raw = params.get("steps")
        steps = _as_int(raw, default_steps)
        if steps < steps_range["min"] or steps > steps_range["max"]:
            _note(notes, "steps", raw, default_steps)
            steps = default_steps
        elif not _is_int_like(raw):
            # 【2026-11-16 第5轮代码审查 —— 归一必须如实记录】
            # ``_as_int("abc", default)`` 会静默返回 default —— 值被换了，
            # 却没留任何说明。那就是「归一做了事但没人知道」。
            # 契约 R2 要求「改了什么、为什么」都记进 normalize_notes。
            _note(notes, "steps", raw, default_steps)
        params["steps"] = steps

    # ---- CFG ----
    default_cfg = _as_float(model.get("cfg"), 1.0)
    if "cfg" in params and params.get("cfg") is not None:
        raw = params.get("cfg")
        cfg = _as_float(raw, default_cfg)
        if cfg < cfg_range["min"] or cfg > cfg_range["max"]:
            _note(notes, "cfg", raw, default_cfg)
            cfg = default_cfg
        elif not _is_float_like(raw):
            _note(notes, "cfg", raw, default_cfg)
        params["cfg"] = cfg

    # ---- shift ----
    default_shift = _as_float(model.get("shift"), 3.0)
    if "shift" in params and params.get("shift") is not None:
        raw = params.get("shift")
        shift = _as_float(raw, default_shift)
        if shift < shift_range["min"] or shift > shift_range["max"]:
            _note(notes, "shift", raw, default_shift)
            shift = default_shift
        elif not _is_float_like(raw):
            _note(notes, "shift", raw, default_shift)
        params["shift"] = shift

    # ---- 采样器 / 调度器（白名单来自模型表，见 R3）----
    default_sampler = model.get("sampler") or ""
    allowed_samplers = model.get("samplers")
    if "sampler" in params and params.get("sampler"):
        raw = str(params.get("sampler")).strip()
        if (isinstance(allowed_samplers, (list, tuple))
                and allowed_samplers and raw not in allowed_samplers):
            _note(notes, "sampler", raw, default_sampler)
            params["sampler"] = default_sampler
        else:
            params["sampler"] = raw

    default_scheduler = model.get("scheduler") or ""
    allowed_schedulers = model.get("schedulers")
    if "scheduler" in params and params.get("scheduler"):
        raw = str(params.get("scheduler")).strip()
        if (isinstance(allowed_schedulers, (list, tuple))
                and allowed_schedulers and raw not in allowed_schedulers):
            _note(notes, "scheduler", raw, default_scheduler)
            params["scheduler"] = default_scheduler
        else:
            params["scheduler"] = raw


def normalize_video_size_fallback(params: dict, mode: str,
                                 notes: list[str]) -> None:
    """给**视频族**补上缺失的 width / height（老任务重放路径的兜底）。

    【为什么需要 —— C-10 的根因】
    ``h3studio/video_workflow.py::build_t2v_panel`` 直接读
    ``params["width"] / params["height"]``，缺失时
    ``vmodels.normalize_video_size(None, None)`` 返回 ``(0, 0, [...])``
    -> 产出的 MiniMaxH3ImageToVideo 节点 width=0 / height=0
    -> **0×0 的 latent**（实测确认）。

    【为什么不用 h3_video_size.apply_size】
    ``apply_size`` 是 flf / r2v 的语义（档位 + 比例 -> 宽高），
    且它对「宽高都缺失」会回落到面板默认值（768p / 16:9）——
    那是**编造尺寸**，用户没要求过。

    这里用**同一套公式**（video_models.video_dimensions），
    但**只在「档位或比例明确给出」时**才补；两者都没有时**不编造**，
    而是把错误**显式抛出去**（由调用方转成可读文案）。

    【为什么抛错而不是编造】
    0×0 latent 提交给 ComfyUI 会得到一句英文的底层报错，
    用户既不知道是哪个字段、也不知道怎么改。
    明确说「缺宽高」比编一个尺寸、或崩在 ComfyUI 里，都更可执行。
    """
    from . import video_models as vmodels

    if not isinstance(params, dict):
        return

    raw_w = params.get("width")
    raw_h = params.get("height")

    def _as_positive(value):
        try:
            out = int(float(value))
        except (TypeError, ValueError):
            return None
        return out if out > 0 else None

    width = _as_positive(raw_w)
    height = _as_positive(raw_h)
    if width and height:
        return

    # 宽高齐了 -> 不用补
    if width and height:
        return

    # 档位 / 比例至少给了一个 -> 用真实公式补齐
    resolution = params.get("resolution")
    aspect = params.get("aspect")
    if not resolution and not aspect:
        raise ValueError(
            "缺少画面尺寸：width/height 为空，resolution 与 aspect 也没给 —— "
            "请在面板上选一个分辨率档位与画面比例，或直接填宽高。")

    from . import h3_video_size
    # 【2026-11-16 第5轮代码审查 C-10 修 —— 不能用 resolve_size 的面板默认回落】
    # ``h3_video_size.resolve_size`` 在「档位与比例都没有」时会回落到
    # **面板默认值**（768p / 16:9）—— 那是**编造尺寸**：
    # 老任务重放出一个用户从没选过的分辨率，正是本项目反复修的
    # 「界面说谎」类缺陷。
    #
    # 所以这里**先自己算**，算不出（0×0）就抛错，绝不编造。
    # 算式与 resolve_size 完全一致（同一份公式）。
    try:
        width, height = h3_video_size.dimensions_for(resolution, aspect, mode=mode)
    except Exception:
        width = height = 0
    if width <= 0 or height <= 0:
        raise ValueError(
            "画面尺寸无效：按 resolution="
            f"{resolution!r} / aspect={aspect!r} 算不出合法的宽高。"
            "请在面板上选一个分辨率档位与画面比例，或直接填宽高。")
    params["width"] = width
    params["height"] = height
    notes.append(
        f"面板档位 {resolution or '（无）'} + 比例 {aspect or '（无）'}"
        f" -> {width}×{height}")
