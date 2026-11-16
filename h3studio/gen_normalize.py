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

【数值区间从哪来】
  steps / cfg / shift 的区间取自**前端真实控件的取值域**
  （web/index.html 的 range 标签 min/max），因为那是用户能拖出来的范围。
  越界值（Agent 传 9999 步、直接调 API 传 -1）回落到模型推荐值。
"""

from __future__ import annotations


#: 前端「采样步数」滑杆区间（web/index.html#imgSteps min=1 max=60）
#: 与视频族 video_models.VIDEO_STEPS_* 并列但**独立** ——
#: 图像族有自己的推荐步数档（8 / 25 / 15 步），不该被视频族的 50 步上限管。
IMAGE_STEPS_MIN = 1
IMAGE_STEPS_MAX = 60

#: 前端「CFG」滑杆区间（web/index.html#imgCfg min=1 max=8）
IMAGE_CFG_MIN = 1.0
IMAGE_CFG_MAX = 8.0

#: 前端「shift」滑杆区间（web/index.html#imgShift min=1 max=7）
IMAGE_SHIFT_MIN = 1.0
IMAGE_SHIFT_MAX = 7.0


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


def normalize_image_sampling(params: dict, model: dict, notes: list[str]) -> None:
    """就地归一**图像模式**的采样参数。

    参数
      params  要归一的 params（就地修改）
      model   该模式的模型定义（必须含 steps / cfg / shift / sampler /
              scheduler 字段；``samplers`` / ``schedulers`` 是白名单，
              缺失时视为「该模型不限制」）
      notes   归一说明收集列表（本函数 append，不清空）

    【为什么放在 builder 之外、归一层之内】
    builder（build_t2i / build_qwen_t2i / build_moss_t2i）读的是
    ``params.get("sampler") or model["sampler"]`` 这种写法 ——
    它**信任** params 里的值。若归一在这里做，builder 拿到的就是干净值；
    若在 builder 里做，三份 builder 就要各改一遍（又是一次漂移）。

    【注意】本函数**不改** model 自己的推荐值 ——
    它只保证 params 里若存在「非法值」就被换掉。
    """
    if not isinstance(params, dict) or not isinstance(model, dict):
        return

    # ---- 步数 ----
    default_steps = _as_int(model.get("steps"), 8)
    if "steps" in params and params.get("steps") is not None:
        raw = params.get("steps")
        steps = _as_int(raw, default_steps)
        if steps < IMAGE_STEPS_MIN or steps > IMAGE_STEPS_MAX:
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
        if cfg < IMAGE_CFG_MIN or cfg > IMAGE_CFG_MAX:
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
        if shift < IMAGE_SHIFT_MIN or shift > IMAGE_SHIFT_MAX:
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
