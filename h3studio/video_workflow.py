# -*- coding: utf-8 -*-
"""
文生视频（MiniMax-H3）的工作流拼装。

与旧版 `_h3_video_workflow()` 的区别（2026-11-10 改造）：

  * **分辨率**：不再走 ResolutionSelector 的「比例 + 总像素」快捷档，
    而是直接吃用户输入的宽高（已按 32 的倍数吸附）。
    旧路径保留在 `_h3_video_workflow()` 里给 flf / r2v 用。
  * **模型可选**：底模从 video_models.VIDEO_MODELS 里选（实测本机 5 个）。
  * **加速 LoRA 组合**：固定 Turbo LoRA + 用户可勾选的组合，串成 LoRA 链。
  * **注意力机制**：按用户选择插入对应的 patch 节点。
  * **二采**：用 SelfLiftH3Sampler 替换标准采样块（低分辨率一采 + 高分辨率二采）。

所有「节点是否本机存在」的判断都在服务端做（video_models），
前端拿到的是已经过滤好的清单 —— 不让用户选完才在跑图时炸掉。
"""

from __future__ import annotations

import random

from . import video_models as vmodels

# 文本编码器 / VAE（实测本机存在，路径见 video_models 的同类说明）
CLIP_H3 = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"

# 帧数 -> 帧数的表达式（与官方工作流一致：24fps，17k+5 网格）
_FRAME_EXPR = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"


def _frame_expr_node(length_node_id: str, dur_node_id: str, duration: float):
    return {
        length_node_id: {
            "inputs": {
                "expression": _FRAME_EXPR,
                "values.a": [dur_node_id, 0],
            },
            "class_type": "ComfyMathExpression",
            "_meta": {"title": "时长->帧数"},
        },
        dur_node_id: {
            "inputs": {"value": float(duration)},
            "class_type": "PrimitiveFloat",
            "_meta": {"title": "时长(秒)"},
        },
    }


def _resolve_model(params: dict) -> dict:
    """用户选的模型 key -> 模型定义（带可用性兜底）。"""
    key = params.get("model_key") or vmodels.VIDEO_DEFAULT_MODEL
    model = vmodels.VIDEO_MODELS.get(key)
    if model is None:
        model = vmodels.VIDEO_MODELS[vmodels.VIDEO_DEFAULT_MODEL]
    return model


def _resolve_accel_chain(params: dict, model: dict) -> list[dict]:
    """加速 LoRA 链：固定 Turbo LoRA + 用户勾选的组合。

    返回 [{"name": ComfyUI 里的 lora_name, "strength": float}]，
    顺序 = 固定加速 LoRA 在前、用户组合在后。
    """
    chain: list[dict] = []

    # 固定加速 LoRA（工作流硬性必需，不出现在用户可勾选列表里）
    fixed_key = params.get("fixed_accel_lora") or vmodels.VIDEO_FIXED_ACCEL_LORA_DEFAULT
    fixed = vmodels.VIDEO_ACCEL_LORAS.get(fixed_key)
    if fixed:
        strength = params.get("fixed_accel_strength")
        if strength is None:
            strength = fixed.get("recommended_strength", 1.0)
        chain.append({"name": fixed["name"], "strength": float(strength),
                      "key": fixed_key, "fixed": True})

    # 用户勾选的加速 LoRA 组合
    for item in params.get("accel_loras") or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        key = item.get("key") or ""
        meta = vmodels.VIDEO_ACCEL_LORA_COMBOS.get(key)
        if meta is None:
            continue
        try:
            strength = float(item.get("strength", meta.get("recommended_strength", 1.0)))
        except (TypeError, ValueError):
            strength = float(meta.get("recommended_strength", 1.0))
        if strength <= 0:
            continue
        chain.append({"name": meta["name"], "strength": strength,
                      "key": key, "fixed": False})
        # 【2026-11-11】单选：用户选的加速 LoRA 只能有一个 ——
        # 拿到第一个就停，后面的忽略（两枚 4 步蒸馏 LoRA 叠加没有意义）。
        break
    return chain


def _apply_attention(p: dict, node_id: str, model_ref: list,
                     params: dict, next_id: int) -> tuple[list, int]:
    """按用户选择插入注意力 patch 节点。

    返回 (新的模型引用, 下一个可用节点 id)。
    """
    mode_key = params.get("attention") or vmodels.VIDEO_DEFAULT_ATTENTION
    mode = vmodels.VIDEO_ATTENTION_MODES.get(mode_key)
    if not mode or not mode.get("node"):
        return model_ref, next_id

    inputs = {"model": model_ref}
    inputs.update(mode.get("inputs") or {})
    p[str(next_id)] = {
        "inputs": inputs,
        "class_type": mode["node"],
        "_meta": {"title": f"注意力·{mode['label']}"},
    }
    return [str(next_id), 0], next_id + 1


def build_t2v_panel(params: dict) -> dict:
    """文生视频面板的工作流。

    params 里的关键字段（都由服务端校验过）：
      model_key        底模 key
      width / height   吸附后的实际像素宽高
      duration         秒
      steps            采样步数
      seed             种子
      fixed_accel_lora 固定加速 LoRA 的 key
      accel_loras      用户勾选的加速 LoRA 组合 [{key, strength, enabled}]
      attention        注意力机制 key
      twopass          二采开关与参数（enabled / transition_step / lowres_scale / rho /
                       w_min / w_max / upscaler_model）
      prompt           提示词（已含导演提示词的产物）
    """
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)
    duration = float(params.get("duration", 5))
    model = _resolve_model(params)
    accel_chain = _resolve_accel_chain(params, model)

    # 步数：用户没给就用模型推荐的；有加速 LoRA 时按 4 步档
    steps = params.get("steps")
    if not steps:
        steps = model.get("steps", 4)
    steps = vmodels.normalize_steps(steps, int(model.get("steps", 4)))
    scheduler = vmodels.normalize_scheduler(
        params.get("scheduler"), model.get("scheduler", "beta"))
    sampler = vmodels.normalize_sampler(
        params.get("sampler"), model.get("sampler", "euler"))
    # CFG：H3 标准采样路径固定 1.0（BasicGuider 没有 cfg 槽位），
    # 只有二采（SelfLiftH3Sampler）那条路径才真的吃 cfg —— 见下方
    # _twopass_block() 的调用。
    #
    # 【2026-11-11】标准路径的 cfg **不会**写进任何节点（BasicGuider
    # 压根没有这个输入），这里只是保留变量形状，让 _twopass_block()
    # 能拿到二采专用的 cfg。标准路径的 CFG 由 vmodels.VIDEO_CFG_FIXED
    # 固定并由面板做只读说明。
    cfg = params.get("cfg")
    if cfg is None:
        cfg = model.get("cfg", 1.0)
    cfg = float(cfg)

    # 宽高：服务端已经吸附过，这里再兜一次（防止绕过服务端的直接调用）
    width, height, _reasons = vmodels.normalize_video_size(
        params.get("width"), params.get("height"))

    p: dict = {}
    next_id = 100

    # ---- 条件与 latent ----
    # 时长 -> 帧数（17k+5 网格）
    expr = _frame_expr_node("105:107", "105:111", duration)
    p.update(expr)
    p["105:11"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader",
                   "_meta": {"title": "视频 VAE"}}
    p["13"] = {"inputs": {"clip_name": CLIP_H3, "type": "minimax", "device": "default"},
               "class_type": "CLIPLoader", "_meta": {"title": "加载文本编码器"}}

    cond_inputs = {
        "clip": ["13", 0],
        "vae": ["105:11", 0],
        "prompt": params.get("prompt", ""),
        "width": width,
        "height": height,
        "length": ["105:107", 1],
    }
    p["105:104"] = {"inputs": cond_inputs, "class_type": "MiniMaxH3ImageToVideo",
                    "_meta": {"title": "H3 条件"}}
    # 条件节点的第 2 个输出就是「目标分辨率 + 时长」定义的 AV latent
    # （MiniMaxH3ImageToVideo.outputs = [CONDITIONING, LATENT]），
    # 标准采样与二采都用它 —— 与旧路径完全一致，不引入第二份 latent。
    latent_ref = ["105:104", 1]

    # ---- 底模 ----
    p["6"] = {"inputs": {"unet_name": model["unet"], "weight_dtype": "default"},
              "class_type": "UNETLoader", "_meta": {"title": "加载DiT"}}
    model_ref: list = ["6", 0]

    # ---- 加速 LoRA 链（固定 Turbo LoRA + 用户组合）----
    for index, item in enumerate(accel_chain):
        node_id = f"138:{index}"
        p[node_id] = {
            "inputs": {
                "model": model_ref,
                "lora_name": item["name"],
                "strength": item["strength"],
                "low_vram": True,
            },
            "class_type": "MiniMaxH3TurboLoRA",
            "_meta": {"title": ("Turbo LoRA（固定）" if item.get("fixed")
                                else f"加速 LoRA·{item.get('key')}")},
        }
        model_ref = [node_id, 0]

    # ---- 注意力机制 ----
    model_ref, next_id = _apply_attention(p, "300", model_ref, params, next_id)

    # ---- shift（MiniMaxH3SigmaShift）----
    #
    # 【2026-11-11 新增】这个节点是 **ComfyUI 核心节点**
    # （comfy_extras/nodes_minimax_h3.py:367），display_name
    # 「ModelSamplingMiniMaxH3」，它做两件事：
    #   1. model_sampling.set_parameters(shift=shift_video, audio_shift=shift_audio)
    #      —— 改采样器的 sigma 时间表；
    #   2. 写 transformer_options["minimax_h3_sigma_shift_video/_audio"]
    #      —— 让 DiT 内部把音视频两条流映射到同一个基准网格。
    #
    # 默认值 12.0 / 3.0 **就是 H3 模型的内生值**：
    #   comfy/supported_models.py:964  sampling_settings = {"shift": 12.0, "audio_shift": 3.0}
    #   comfy/ldm/minimax/model.py:492  sigma_shift_video=12.0, sigma_shift_audio=3.0
    # 所以「不接这个节点」和「接了但用默认值」是**完全等价**的 ——
    # 面板给默认值时不会改变任何出图结果。
    #
    # 节点不存在时**不接**（保持原样），不要硬塞一个 ComfyUI 不认的节点。
    shift_video = vmodels.normalize_shift(
        params.get("shift_video"), vmodels.VIDEO_SHIFT_DEFAULT_VIDEO)
    shift_audio = vmodels.normalize_shift(
        params.get("shift_audio"), vmodels.VIDEO_SHIFT_DEFAULT_AUDIO)
    if params.get("use_sigma_shift", True):
        p[str(next_id)] = {
            "inputs": {
                "model": model_ref,
                "shift_video": shift_video,
                "shift_audio": shift_audio,
            },
            "class_type": "MiniMaxH3SigmaShift",
            "_meta": {"title": "sigma shift（视频/音频）"},
        }
        model_ref = [str(next_id), 0]
        next_id += 1

    # ---- sigma ----
    p["9"] = {"inputs": {"scheduler": scheduler, "steps": steps, "denoise": 1,
                          "model": model_ref},
              "class_type": "BasicScheduler", "_meta": {"title": "调度器"}}
    p["17"] = {"inputs": {"sampler_name": sampler}, "class_type": "KSamplerSelect",
               "_meta": {"title": "采样器"}}

    twopass = params.get("twopass") or {}
    use_twopass = bool(twopass.get("enabled"))

    if use_twopass:
        # 二采路径的 CFG 来自 twopass.cfg（面板上二采区块里的真控件），
        # 没给才回落到模型默认值。
        samples_ref = _twopass_block(p, model_ref, steps, scheduler, sampler, seed,
                                     cfg, twopass, width, height, duration,
                                     latent_ref)
    else:
        p["15"] = {"inputs": {"noise_seed": int(seed)}, "class_type": "RandomNoise",
                   "_meta": {"title": "噪波"}}
        p["16"] = {"inputs": {"model": model_ref, "conditioning": ["105:104", 0]},
                   "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
        p["14"] = {"inputs": {"noise": ["15", 0], "guider": ["16", 0],
                               "sampler": ["17", 0], "sigmas": ["9", 0],
                               "latent_image": latent_ref},
                   "class_type": "SamplerCustomAdvanced", "_meta": {"title": "采样"}}
        samples_ref = ["14", 0]

    # ---- 解码 + 合成 + 保存 ----
    prefix = f"h3studio/t2v_{job_id}"
    p["10"] = {"inputs": {"samples": samples_ref, "vae": ["105:11", 0]},
               "class_type": "VAEDecode", "_meta": {"title": "解码视频"}}
    p["24"] = {"inputs": {"vae_name": VAE_AUDIO}, "class_type": "VAELoader",
               "_meta": {"title": "音频 VAE"}}
    p["23"] = {"inputs": {"samples": samples_ref, "vae": ["24", 0]},
               "class_type": "VAEDecodeAudio", "_meta": {"title": "解码音频"}}
    p["91"] = {"inputs": {"fps": 24, "bit_depth": 8, "images": ["10", 0],
                          "audio": ["23", 0]},
               "class_type": "CreateVideo", "_meta": {"title": "合成视频"}}
    p["92"] = {"inputs": {"filename_prefix": prefix, "format": "auto", "codec": "auto",
                          "video": ["91", 0]},
               "class_type": "SaveVideo", "_meta": {"title": "保存视频"}}
    return p


def _twopass_block(p: dict, model_ref: list, steps: int, scheduler: str,
                   sampler: str, seed: int, cfg: float, twopass: dict,
                   width: int, height: int, duration: float,
                   latent_ref: list) -> list:
    """二采（SelfLift）采样块。

    **硬约束**（实测自 comfyui-SelfLift/nodes.py）：
      * 只支持标准 Euler 采样器（s_churn=0）—— Turbo 的自定义采样器不能用，
        所以这里强制 euler；
      * transition_step 必须落在 [1, len(sigmas)-2]，
        len(sigmas) = steps + 1 -> 上限是 steps - 1；
      * rho=0 且 upscaler_model=none 会被节点直接拒绝 -> 这时给一个非零 rho。
    """
    forced_sampler = "euler"
    p["17"] = {"inputs": {"sampler_name": forced_sampler}, "class_type": "KSamplerSelect",
               "_meta": {"title": "采样器（二采固定 Euler）"}}

    # sigmas 也要重算（节点要拿同一份 sigma 表做两段）
    p["9"] = {"inputs": {"scheduler": scheduler, "steps": steps, "denoise": 1,
                          "model": model_ref},
              "class_type": "BasicScheduler", "_meta": {"title": "调度器"}}

    max_transition = max(1, steps - 1)
    transition_step = int(twopass.get("transition_step")
                          or vmodels.VIDEO_TWOPASS["transition_step_default"])
    transition_step = max(1, min(transition_step, max_transition))

    lowres_scale = float(twopass.get("lowres_scale",
                                     vmodels.VIDEO_TWOPASS["lowres_scale_default"]))
    lowres_scale = max(0.25, min(1.0, lowres_scale))

    rho = float(twopass.get("rho", vmodels.VIDEO_TWOPASS["rho_default"]))
    upscaler = twopass.get("upscaler_model") or vmodels.VIDEO_TWOPASS["upscaler_default"]
    if rho <= 0 and upscaler == "none":
        # 节点会直接 raise：两条路都关掉了
        rho = 0.6
    w_min = float(twopass.get("w_min", vmodels.VIDEO_TWOPASS["w_min_default"]))
    w_max = float(twopass.get("w_max", vmodels.VIDEO_TWOPASS["w_max_default"]))
    if w_min > w_max:
        w_min, w_max = w_max, w_min

    # CFG：SelfLiftH3Sampler 自带 cfg 输入（节点 schema: 0~100, default 5.0），
    # 这是 H3 链路里**唯一**真正可调 CFG 的地方。
    # 优先取 twopass.cfg（面板二采区块的控件），回落顺序：
    #   twopass.cfg -> 外部传入的 cfg -> VIDEO_TWOPASS["cfg_default"]
    try:
        sampler_cfg = float(twopass.get("cfg", cfg))
    except (TypeError, ValueError):
        sampler_cfg = float(cfg)
    sampler_cfg = max(0.0, min(100.0, sampler_cfg))

    # 负面条件：H3 的引导器要 positive + negative
    p["16"] = {"inputs": {"model": model_ref, "conditioning": ["105:104", 0]},
               "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
    p["320"] = {"inputs": {"conditioning": ["105:104", 0]},
                "class_type": "ConditioningZeroOut", "_meta": {"title": "负面条件"}}

    p["171"] = {
        "inputs": {
            "model": model_ref,
            "positive": ["105:104", 0],
            "negative": ["320", 0],
            "vae": ["105:11", 0],
            "latent_image": latent_ref,
            "sampler": ["17", 0],
            "sigmas": ["9", 0],
            "seed": int(seed),
            "cfg": sampler_cfg,
            "transition_step": transition_step,
            "lowres_scale": lowres_scale,
            "rho": rho,
            "w_min": w_min,
            "w_max": w_max,
            "upscaler_model": upscaler,
            "upscaler_unload": True,
            "return_lowres_latent": False,
        },
        "class_type": "SelfLiftH3Sampler",
        "_meta": {"title": "二采（SelfLift 低分辨率一采 + 高分辨率二采）"},
    }
    return ["171", 0]
