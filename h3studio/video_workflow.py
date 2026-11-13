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

from . import continuation as cont
from . import continuation_service as cont_service
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


def _resolve_effect_chain(params: dict) -> list[dict]:
    """效果 LoRA 链：用户勾选的**多枚**效果 LoRA，各自带强度。

    返回 [{"name": ComfyUI 里的 lora_name, "strength": float, "trigger": str}]，
    顺序 = 用户在界面上的排列顺序（先加的在前）。

    【与加速 LoRA 的关系 —— 两者并列共存】
    加速 LoRA（Turbo / LightX2V / DMAD）改的是**采样语义**（把 20 步压到
    N 步），效果 LoRA 改的是**画面风格**。两者作用在不同层面，可以共存：
    链路 = 底模 -> 加速 LoRA 链 -> 效果 LoRA 链 -> 注意力 -> sigma shift。
    """
    from . import h3_effect_loras as effect_registry

    chain: list[dict] = []
    for item in params.get("effect_loras") or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 1.0
        if strength == 0:
            # 强度 0 = 关闭该 LoRA（但仍要校验文件存在，避免静默吞掉错名）
            resolved, path = effect_registry.lora_registry.resolve_lora(name)
            if not resolved or path is None:
                raise ValueError(f"本机不存在该效果 LoRA 文件：{name}")
            continue
        # 文件必须真实存在 —— 找不到就报错，不静默忽略
        resolved, path = effect_registry.lora_registry.resolve_lora(name)
        if not resolved or path is None:
            raise ValueError(f"本机不存在该效果 LoRA 文件：{name}")
        chain.append({
            "name": resolved,
            "strength": strength,
            "trigger": (item.get("trigger") or "").strip(),
        })
    return chain


def _apply_effect_loras(p: dict, model_ref: list, chain: list[dict],
                        next_id: int) -> tuple[list, int]:
    """把效果 LoRA 链插到模型链上。

    返回 (新的模型引用, 下一个可用节点 id)。

    【为什么用 `LoraLoaderModelOnly` 而不是 `MiniMaxH3TurboLoRA`】
    加速 LoRA 用 Turbo 节点是因为它的键名是裸 `blocks.*`；效果 LoRA 的键名
    带 `diffusion_model.` 前缀，Turbo 节点会 **静默失效**（实测日志：
    "416 backbone modules, 0 weights patched"）。
    核心 `LoraLoaderModelOnly` 走 `comfy.lora.model_lora_keys_unet()`，
    同时认 `diffusion_model.blocks.*` / `lora_unet_blocks_*` / 裸 `blocks.*`
    三种写法 —— 实测本机 27 枚效果 LoRA 全部命中 200~258 层、0 形状不匹配。

    【为什么是 ModelOnly 而不是 LoraLoader】
    H3 的文本编码器是独立的 CLIP 对象（qwen3vl_32b），H3 的效果 LoRA
    全部只 patch DiT 的 blocks / token_refiner，**不 patch 文本编码器**。
    用 `LoraLoader`（带 clip 输入）会让 ComfyUI 对 clip 侧记一堆
    "NOT LOADED" 警告；而 `LoraLoaderModelOnly` 语义上就是
    "只改扩散模型" —— 与实际权重内容完全一致。

    【strength 真的生效吗】
    `LoraLoaderModelOnly` 的 `strength_model` 直接进
    `comfy.sd.load_lora_for_models(model, None, lora, strength_model, 0)`
    -> `model_patcher.add_patches(loaded, strength_model)`
    -> `LoRAAdapter.calculate_weight(...)` 里
    `weight += function(((strength * alpha) * lora_diff))`。
    即：强度是**乘在 LoRA 增量上**的线性系数，0 = 不加任何增量。
    """
    for index, item in enumerate(chain):
        node_id = f"320:{index}"
        p[node_id] = {
            "inputs": {
                "model": model_ref,
                "lora_name": item["name"],
                "strength_model": float(item["strength"]),
            },
            "class_type": "LoraLoaderModelOnly",
            "_meta": {"title": f"效果 LoRA·{item['name']}"},
        }
        model_ref = [node_id, 0]
    return model_ref, next_id


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

    # ---- 效果 LoRA 链（用户可多选，各自带强度）----
    #
    # 【插在加速链之后、注意力之前】
    # 加速 LoRA 改的是采样语义（20 步 -> 4 步），效果 LoRA 改的是画面风格；
    # 顺序上「先把底模语义改对，再叠风格」与图像面板
    # （加速在前、用户效果 LoRA 在后）保持一致。
    effect_chain = _resolve_effect_chain(params)
    if effect_chain:
        model_ref, next_id = _apply_effect_loras(
            p, model_ref, effect_chain, next_id)
        # 触发词自动追加到提示词末尾（与图像面板同一套规则：
        # 已存在的词不重复追加，多枚 LoRA 按顺序追加）
        from . import workflows as wf
        merged = wf.merge_lora_triggers(params.get("prompt", ""), effect_chain)
        if merged != (params.get("prompt") or ""):
            p["105:104"]["inputs"]["prompt"] = merged

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

    # ---- 接续（continuation）----
    #
    # 【插在哪】拷贝桥改的是**采样器的 latent 输入**，所以它必须接在
    # `latent_ref` 与采样器**之间**；裁重叠（TrimAV）改的是 **decode 之后
    # 的像素帧**，所以它必须接在 VAEDecode 之后。两头都不能接错。
    #
    # 两种接续形态：
    #   * 首段（无上游）：latent 直通采样器；采样器的输出**落盘**成接力棒。
    #   * 续接段（有上游）：读回上游潜空间 -> 拷贝桥（逐位拷贝尾段 + 噪声掩码）
    #     -> 采样器；采样器的输出**落盘**成下一段的接力棒；
    #     decode 之后经裁重叠（裁掉钉住区）再合成视频。
    #
    # 【为什么二采不走这条路】见 h3studio/continuation.py 的模块说明：
    # 二采的潜空间与标准采样路径不同源，接续会位移。服务端在
    # continuation.continuation_allowed() 里已经拦掉了，这里再兜一次底。
    cont_state = _continuation_state(params)
    latent_in_ref = latent_ref
    cond_ref = ["105:104", 0]
    trim_ref = None

    if cont_state["enabled"] and not use_twopass and cont_state["has_source"]:
        # ---- 续接段：读回上游潜空间 -> 拷贝桥 ----
        #
        # 【读回哪一份 —— 这是本段最关键的一根线】
        # 读回的是**上游自己那一份**潜空间，路径由服务端从上游任务记录
        # 权威算出（`source_latent_path`），以 `explicit_path` 交给节点。
        #
        # 【为什么不用「按链段号推坐标」】用户需求第 7 条允许对同一段
        # 反复接续 -> 同一条链会**分叉**成兄弟分支 B1 / B2。若按
        # 「链目录 + 链段号」推坐标，B1 与 B2 的坐标完全相同 ->
        # 写同一个文件、互相覆盖 -> 再接续时读到兄弟分支的潜空间，
        # 产出**静默的错误内容**（实测 C1 首帧 vs B1末帧 MAE 38.65、
        # vs B2末帧 2.08，偏离 18.6 倍）。
        # 每段一个专属目录（run_id 由该段自己的任务 id 派生），
        # 结构上就不可能互相覆盖 —— 详见 continuation.segment_run_id。
        p["300:load"] = {
            "inputs": {
                "run_id": cont_state["source_run_id"],
                # 有 explicit_path 时节点直接读那个文件（不再按段号推）。
                # 段号给「上游的段号 + 1」只为让节点内部的报错信息可读。
                "stage_index": cont_state["source_stage"] + 1,
                "explicit_path": cont_state["source_path"],
            },
            "class_type": "H3RelayLatentLoad",
            "_meta": {"title": "接续·读回上游潜空间"},
        }
        # 外观锚：链首段那一份（防长程漂移）。
        # 每段一个目录之后，链首段的潜空间在**另一个目录** —— 拷贝桥
        # 靠 run_id 自动读锚的能力用不上了，必须显式读一份接进来。
        # 锚是**可选**的：找不到就不接（缺锚只是漂移风险，不是接错段）。
        anchor_latent_ref = None
        if cont_state["anchor_path"]:
            p["300:anchor_load"] = {
                "inputs": {
                    "run_id": cont_state["anchor_run_id"],
                    "stage_index": cont_state["anchor_stage"] + 1,
                    "explicit_path": cont_state["anchor_path"],
                },
                "class_type": "H3RelayLatentLoad",
                "_meta": {"title": "接续·读回链首段潜空间（外观锚）"},
            }
            anchor_latent_ref = ["300:anchor_load", 0]
        p["300:bridge"] = {
            "inputs": {
                "latent": latent_ref,
                "context_frames": cont_state["context_frames"],
                "context_latent": ["300:load", 0],
                # hard = 钉住区零重绘（真续接唯一推荐档）。
                "mask_mode": "hard",
                "pin_audio": True,
                # 段号 >= 1 且没有 context_latent 时拷贝桥会 raise ——
                # 我们永远接了 context_latent，所以段号给真实值即可。
                "stage_index": cont_state["source_stage"],
                "run_id": cont_state["source_run_id"],
                # 外观锚：显式接进来（每段一个目录后不能再靠自动读）。
                "ref_anchor_latent": anchor_latent_ref,
                "ref_anchor_stage": -1,
                "ref_anchor_frames": cont_state["anchor_frames"],
                # 第 4 路：conditioning 钉帧（管取景/构图）。
                "conditioning": cond_ref,
            },
            "class_type": "H3RelayCopyBridge",
            "_meta": {"title": "接续·拷贝桥（零重编码潜空间桥）"},
        }
        latent_in_ref = ["300:bridge", 0]
        cond_ref = ["300:bridge", 3]
        trim_ref = ["300:bridge", 2]

    if use_twopass:
        # 二采路径的 CFG 来自 twopass.cfg（面板上二采区块里的真控件），
        # 没给才回落到模型默认值。
        samples_ref = _twopass_block(p, model_ref, steps, scheduler, sampler, seed,
                                     cfg, twopass, width, height, duration,
                                     latent_in_ref)
    else:
        p["15"] = {"inputs": {"noise_seed": int(seed)}, "class_type": "RandomNoise",
                   "_meta": {"title": "噪波"}}
        p["16"] = {"inputs": {"model": model_ref, "conditioning": cond_ref},
                   "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
        p["14"] = {"inputs": {"noise": ["15", 0], "guider": ["16", 0],
                               "sampler": ["17", 0], "sigmas": ["9", 0],
                               "latent_image": latent_in_ref},
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

    # ---- 裁重叠：去掉钉住区（音画同裁，保住 A/V 同步）----
    if trim_ref is not None:
        p["300:trim"] = {
            "inputs": {
                "images": ["10", 0],
                "audio": ["23", 0],
                "trim_frames": trim_ref,
                "fps": 24,
                # 沉降帧保持 0：只裁钉住区。Relay-Kit 实测「裁沉降」才是
                # 缝处跳帧的源头（裁 0 帧跳 0.020 几乎无感 / 裁 16 帧 0.055）。
                "settle_frames": 0,
                "seam_ghost": 0,
            },
            "class_type": "H3RelayTrimAV",
            "_meta": {"title": "接续·裁重叠（裁掉钉住区，音画同裁）"},
        }
        video_images_ref = ["300:trim", 0]
        video_audio_ref = ["300:trim", 1]
    else:
        video_images_ref = ["10", 0]
        video_audio_ref = ["23", 0]

    # ---- 落盘接力棒 ----
    # 采样器的输出（latent）落盘，供下一段读回。
    # 节点名 H3RelayLatentSave：把本段 AV latent 存成文件，它是下一段的「接力棒」。
    #
    # 【写在哪】写进**本段自己的专属目录**（run_id 由本段任务 id 派生，
    # 段号恒 0）。这样兄弟分支各写各的目录 —— 结构上不可能互相覆盖。
    # 详见 continuation.segment_run_id 的说明。
    if cont_state["enabled"]:
        p["300:save"] = {
            "inputs": {
                "latent": samples_ref,
                "run_id": cont_state["own_run_id"],
                "stage_index": 0,
                "note": f"h3studio job {job_id}",
            },
            "class_type": "H3RelayLatentSave",
            "_meta": {"title": "接续·落盘本段潜空间（下一段的接力棒）"},
        }

    p["91"] = {"inputs": {"fps": 24, "bit_depth": 8, "images": video_images_ref,
                          "audio": video_audio_ref},
               "class_type": "CreateVideo", "_meta": {"title": "合成视频"}}
    p["92"] = {"inputs": {"filename_prefix": prefix, "format": "auto", "codec": "auto",
                          "video": ["91", 0]},
               "class_type": "SaveVideo", "_meta": {"title": "保存视频"}}
    return p


def _continuation_state(params: dict) -> dict:
    """从任务参数算出「这次要不要接续、接谁的第几段」。

    返回：
      {
        "enabled":        本次任务是否参与接续（会落盘接力棒）,
        "has_source":     本次是否是「续接段」（有上游潜空间）,
        "own_run_id":     **本段自己**的落盘目录名（接力棒写这里）,
        "source_run_id":  上游的落盘目录名（从那里读）,
        "source_stage":   上游在那个目录里的段号,
        "source_path":    上游潜空间文件的**绝对路径**（有上游时必填）,
        "anchor_path":    链首段潜空间文件的绝对路径（外观锚用）,
        "context_frames": 钉住窗口（像素帧）,
        "anchor_frames":  外观锚帧数,
      }

    【参数来源】前端回填的接续上下文会带 `continuation` 字段，
    服务端在提交前会**权威重算**并补齐其中的落盘坐标：
      {
        "chain_id":    链 id（同一链的所有段共用）,
        "stage_index": 本段段号,
        "source_job":  上游任务 id,
        "source_latent_run_id": 上游的落盘目录名,
        "source_latent_stage":  上游在那个目录里的段号,
        "source_latent_path":   上游潜空间文件的绝对路径,
        "anchor_latent_path":   链首段潜空间文件的绝对路径,
        "context_frames": 钉住窗口,
      }
    没有这个字段 = 不接续（普通生成）。

    【为什么「自己的目录」与「上游的目录」要分开】
    本段要**读**上游那份、**写**自己那份。若两者共用一个目录 +
    段号，兄弟分支（B1/B2）会撞同一个文件互相覆盖 ——
    详见 continuation.segment_run_id 的说明。
    """
    info = (params or {}).get("continuation")
    blank = {"enabled": False, "has_source": False, "own_run_id": "",
             "source_run_id": "", "source_stage": 0, "source_path": "",
             "anchor_path": "", "context_frames": 0,
             "anchor_frames": cont.DEFAULT_CONTEXT_FRAMES}
    if not isinstance(info, dict) or not info:
        return dict(blank)

    chain_id = str(info.get("chain_id") or "").strip()
    if not chain_id:
        return dict(blank)

    # 本段自己的落盘目录：服务端写死 `latent_run_id`（由本段任务 id 派生）。
    # 兜底：没有这个字段时用本段任务 id 现算（协议层单一真源）。
    own_run_id = str(info.get("latent_run_id") or "").strip()
    if not own_run_id:
        job_id = str((params or {}).get("job_id") or "").strip()
        own_run_id = cont.segment_run_id(job_id) if job_id else ""

    # 上游的落盘坐标：服务端从上游任务记录**权威算出**后写在这里。
    source_run_id = str(info.get("source_latent_run_id") or "").strip()
    try:
        source_stage = int(info.get("source_latent_stage", 0))
    except (TypeError, ValueError):
        source_stage = 0
    source_stage = max(0, source_stage)
    if not source_run_id:
        # 兼容旧任务：没有权威坐标时按老规则推（链目录 + 链段号）。
        source_run_id, source_stage = cont.latent_coords(info)

    source_job = str(info.get("source_job") or "").strip()
    has_source = bool(source_job and source_run_id)

    # 上游潜空间文件的**绝对路径** —— 读回必须精确到这一个文件。
    source_path = str(info.get("source_latent_path") or "").strip()
    if not source_path and source_run_id:
        source_path = str(cont.stage_file_path(
            cont_service.comfy_output_dir(), source_run_id, source_stage))

    # 外观锚：链首段那份（防长程漂移）。
    anchor_path = str(info.get("anchor_latent_path") or "").strip()
    anchor_run_id = str(info.get("anchor_latent_run_id") or "").strip()
    try:
        anchor_stage = int(info.get("anchor_latent_stage", 0))
    except (TypeError, ValueError):
        anchor_stage = 0
    anchor_stage = max(0, anchor_stage)

    # 钉住窗口：必须落在 5+17k 网格上，否则拷贝桥切不出来。
    raw_window = info.get("context_frames") or cont.DEFAULT_CONTEXT_FRAMES
    try:
        window = int(raw_window)
    except (TypeError, ValueError):
        window = cont.DEFAULT_CONTEXT_FRAMES
    if not cont.valid_context_frames(window):
        window = cont.snap_context_frames(window) or cont.DEFAULT_CONTEXT_FRAMES

    try:
        source_segment = int(info.get("source_segment", 0))
    except (TypeError, ValueError):
        source_segment = 0

    anchor_frames = info.get("anchor_frames")
    try:
        anchor_frames = int(anchor_frames)
    except (TypeError, ValueError):
        anchor_frames = cont.DEFAULT_CONTEXT_FRAMES
    if not cont.valid_context_frames(anchor_frames):
        anchor_frames = cont.snap_context_frames(anchor_frames) or cont.DEFAULT_CONTEXT_FRAMES

    return {
        "enabled": True,
        "has_source": has_source,
        "own_run_id": own_run_id,
        "source_run_id": source_run_id,
        "source_stage": source_stage,
        "source_path": source_path,
        "anchor_path": anchor_path,
        "anchor_run_id": anchor_run_id,
        "anchor_stage": anchor_stage,
        "context_frames": window,
        "anchor_frames": anchor_frames,
        "source_job": source_job,
        "source_segment": source_segment,
    }


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

    # 【2026-11-13】transition_step 上限按**精修后**的 sigma 表长度算。
    # 精修参数在下面算（sigma_refine），但上限公式在这里就要用到它 ——
    # 所以这里先读出 extra_steps（不插节点，只算长度）。
    # 与 server.normalize_t2v_params 的算法保持一致（同一份公式）。
    _refine_cfg = twopass.get("sigma_refine") or {}
    _extra_steps = 0
    if _refine_cfg.get("enabled"):
        try:
            _extra_steps = max(0, min(15, int(_refine_cfg.get(
                "extra_steps",
                vmodels.VIDEO_TWOPASS["sigma_refine_extra_steps_default"]))))
        except (TypeError, ValueError):
            _extra_steps = 0
    _refined_len = steps + 1 + _extra_steps
    max_transition = max(1, _refined_len - 2)
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

    # ---- 【2026-11-13 新增】sigma 精修节点（借鉴 nonebot H3 工作流）----
    #
    # 借鉴来源：/home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/
    #   minimax_h3_v1.json 的 node 346（class_type=H3SigmaRefiner）
    # 节点源码：/home/ubuntu/comfyui/custom_nodes/ComfyUI-YCNodes-MiniMax-H3/
    #   py/h3_sigma_refiner.py:52-98
    #
    # 它在 sigma 表的**低噪段局部加密采样步**（多花 NFE 换低噪阶段细节），
    # 针对「高速运动边缘像素颗粒 / 闪烁」。
    #
    # 【本项目默认关闭】理由（实测，别照抄 nonebot 的参数）：
    #   * nonebot 用 start_at_sigma=0.7，但本项目默认档 steps=4 / beta 下
    #     sigma 尾段只有 0.7207 一档 > 0.7，之后直接跳到 0.0 ->
    #     节点判定 idx == len-1 -> **原样返回（完全 no-op）**。
    #   * 节点自己的 tooltip 写「H3 推荐设在 2.0 ~ 3.5 之间」，
    #     所以本项目默认取 2.5（落在推荐区间内，真的会生效）。
    #
    # 【transition_step 必须重标定】
    # SelfLift 把 sigma 表切成两段：
    #     低分辨率一采 = sigmas[:transition_step + 1]
    #     高分辨率二采 = sigmas[transition_step:]
    # transition_step 是**索引**。精修后表长 +extra_steps，索引语义会变 ——
    # 所以这里按「精修后的实际表长」重新夹一次范围，并保证
    # transition_step <= len(sigmas) - 2（SelfLift 的硬约束）。
    sigma_ref = twopass.get("sigma_refine") or {}
    refine_enabled = bool(sigma_ref.get("enabled"))
    sigmas_ref = ["9", 0]
    extra_steps = int(sigma_ref.get(
        "extra_steps", vmodels.VIDEO_TWOPASS["sigma_refine_extra_steps_default"]))
    extra_steps = max(0, min(15, extra_steps))
    if refine_enabled and extra_steps > 0:
        start_at_sigma = float(sigma_ref.get(
            "start_at_sigma",
            vmodels.VIDEO_TWOPASS["sigma_refine_start_at_sigma_default"]))
        end_at_sigma = float(sigma_ref.get(
            "end_at_sigma",
            vmodels.VIDEO_TWOPASS["sigma_refine_end_at_sigma_default"]))
        spacing = str(sigma_ref.get(
            "spacing",
            vmodels.VIDEO_TWOPASS["sigma_refine_spacing_default"]))
        if spacing not in ("cosine", "linear", "exponential"):
            spacing = vmodels.VIDEO_TWOPASS["sigma_refine_spacing_default"]
        p["346"] = {
            "inputs": {
                "sigmas": ["9", 0],
                "extra_steps": extra_steps,
                "start_at_sigma": max(0.0, min(20.0, start_at_sigma)),
                "end_at_sigma": max(0.0, min(5.0, end_at_sigma)),
                "spacing": spacing,
            },
            "class_type": "H3SigmaRefiner",
            "_meta": {"title": "sigma 精修（低噪段加密采样）"},
        }
        sigmas_ref = ["346", 0]
        # 精修后 sigma 表长度 = (steps + 1) + extra_steps
        # （节点把尾段长度从 (len - idx) 扩成 (len - idx) + extra_steps）
        refined_len = steps + 1 + extra_steps
        max_transition = max(1, refined_len - 2)
        transition_step = max(1, min(transition_step, max_transition))

    p["171"] = {
        "inputs": {
            "model": model_ref,
            "positive": ["105:104", 0],
            "negative": ["320", 0],
            "vae": ["105:11", 0],
            "latent_image": latent_ref,
            "sampler": ["17", 0],
            "sigmas": sigmas_ref,
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
