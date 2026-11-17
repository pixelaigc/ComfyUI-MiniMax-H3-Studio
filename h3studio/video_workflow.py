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
from . import frame_expr as frame_expr_mod

# 文本编码器 / VAE（实测本机存在，路径见 video_models 的同类说明）
CLIP_H3 = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"

# 帧数 -> 帧数的表达式（与官方工作流一致：24fps，17k+5 网格）
# 【2026-11-16 第5轮代码审查 C-13 修 —— 三份重复实现合一】
# 真正的实现见 h3studio/frame_expr.py（零依赖模块，避免
# workflows <-> video_workflow 的循环 import）。
def _frame_expr_node(length_node_id: str, dur_node_id: str, duration: float):
    """秒数 -> 帧数节点（**唯一真源**在 h3studio/frame_expr.py）。

    【2026-11-16 第5轮代码审查 C-13 修 —— 三份重复实现合一】
    原来这里有第三份 _frame_expr_node 实现，与 workflows.py / h3_video_modes.py
    的两份重复（公式相同、签名分叉）。现在统一走 frame_expr.frame_expr_node。
    """
    return frame_expr_mod.frame_expr_node(length_node_id, dur_node_id, duration)


def _resolve_model(params: dict) -> dict:
    """用户选的模型 key -> 模型定义（带可用性兜底）。"""
    key = params.get("model_key") or vmodels.VIDEO_DEFAULT_MODEL
    model = vmodels.VIDEO_MODELS.get(key)
    if model is None:
        model = vmodels.VIDEO_MODELS[vmodels.VIDEO_DEFAULT_MODEL]
    return model


def _resolve_accel_chain(params: dict, model: dict) -> list[dict]:
    """加速 LoRA 链 —— **一个槽位**，前端选哪枚就只挂哪枚。

    返回 [{"name": ComfyUI 里的 lora_name, "strength": float,
            "key": key, "loader": 节点名}]，最多一项。

    【2026-11-14 重构 —— 这段是「选了不生效」的根因所在】
    旧实现返回的是「固定枚 + 用户选的组合枚」**两枚叠加**：
        chain = [fixed_accel_lora（恒为 ckpt850）] + [用户选的那枚]
    于是前端选 LightX2V，工作流里仍然是 ckpt850 在前面挡着。

    现在只解析**一个**槽位（vmodels.resolve_accel_lora_slot 负责
    三套历史字段的换算），前端选什么就是什么。

    【loader 字段为什么必须有】
    每枚 LoRA 的权重键名形态不同，必须用对应的加载节点，否则
    ComfyUI 会**静默丢弃**（Turbo 节点拿到 diffusion_model. 前缀权重时
    一个都 patch 不上，出片照跑但 LoRA 没生效）。
    路由判据是权重的实测键名形态，见 vmodels.detect_accel_lora_loader。
    """
    chain: list[dict] = []

    key, strength = vmodels.resolve_accel_lora_slot(params)
    if not key:
        # 显式「不挂加速 LoRA」-> 空链（工作流走标准多步采样）
        return chain

    meta = vmodels.accel_lora_meta(key)
    if not meta:
        # 未知 key -> 不挂（服务端归一层会提前拦掉并报错，
        # 这里只是兜底，避免工作流构建阶段抛异常）
        return chain

    try:
        strength = float(strength)
    except (TypeError, ValueError):
        strength = float(meta.get("recommended_strength", 1.0))
    if strength <= 0:
        return chain

    name = meta["name"]
    chain.append({
        "name": name,
        "strength": strength,
        "key": key,
        "loader": vmodels.detect_accel_lora_loader(name),
    })
    return chain


def apply_accel_lora_nodes(p: dict, model_ref: list, chain: list[dict],
                           node_prefix: str = "138:") -> list:
    """把加速 LoRA 链插到模型链上，按**权重形态**选加载节点。

    返回新的模型引用。

    【为什么必须分流到两个节点】
    加速 LoRA 的权重键名形态不同，而两个加载节点各自只认一种：
      * 裸键名（Turbo 系 ckpt850 / v4step600）
        -> MiniMaxH3TurboLoRA
           它额外做剪枝底模的 AdaLN 运行期重注入（_inject_adaln_egrid），
           核心节点没这个能力 —— 用核心节点挂 ckpt850 会丢掉 51 个 AdaLN 层。
      * diffusion_model. 前缀（LightX2V / DMAD / better_motion）
        -> 核心 LoraLoaderModelOnly
           Turbo 节点会把模块名当成 diffusion_model.diffusion_model.xxx，
           **一个权重都 patch 不上**（静默失效，出片照跑但 LoRA 没生效）。

    用错节点的后果不是报错，而是「看起来正常但没生效」——
    这是「前端选了加速 LoRA 却不生效」的根因之一。
    """
    for index, item in enumerate(chain):
        node_id = f"{node_prefix}{index}"
        loader = item.get("loader") or vmodels.ACCEL_LOADER_TURBO
        if loader == vmodels.ACCEL_LOADER_CORE:
            p[node_id] = {
                "inputs": {
                    "model": model_ref,
                    "lora_name": item["name"],
                    "strength_model": item["strength"],
                },
                "class_type": vmodels.ACCEL_LOADER_CORE,
                "_meta": {"title": f"加速 LoRA·{item.get('key')}"},
            }
        else:
            p[node_id] = {
                "inputs": {
                    "model": model_ref,
                    "lora_name": item["name"],
                    "strength": item["strength"],
                    "low_vram": True,
                },
                "class_type": vmodels.ACCEL_LOADER_TURBO,
                "_meta": {"title": f"加速 LoRA·{item.get('key')}"},
            }
        model_ref = [node_id, 0]
    return model_ref


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
        # 【2026-11-14】默认步数要看**实际挂了哪枚加速 LoRA**：
        #   挂了加速 LoRA -> 该 LoRA 推荐的步数（4 步蒸馏 / 8 步蒸馏各不同）
        #   没挂           -> 20 步标准采样
        #
        # 旧实现恒取 model["steps"]（= 4），于是「不挂加速 LoRA」时
        # 也会跑 4 步 —— 那是蒸馏档的步数，配非蒸馏采样必然糊。
        # 旧实现之所以「碰巧」没错，是因为加速 LoRA 恒定存在（写死的 ckpt850）。
        # 现在加速 LoRA 由用户选，步数默认值必须跟着变。
        if accel_chain:
            item = accel_chain[0]
            meta = vmodels.accel_lora_meta(item.get("key") or "") or {}
            default_steps = int(meta.get("recommended_steps") or 4) or 4
        else:
            default_steps = vmodels.VIDEO_STEPS_WITHOUT_ACCEL
        steps = default_steps
    steps = vmodels.normalize_steps(steps, int(model.get("steps", 4)))
    # 调度器 / 采样器同理：加速 LoRA 档用 beta/euler（蒸馏档的配套），
    # 标准多步采样用 simple/res_multistep（官方非蒸馏模板的配套）。
    # 加速 LoRA 的 recommended_scheduler / recommended_sampler 优先。
    if accel_chain:
        meta = vmodels.accel_lora_meta(accel_chain[0].get("key") or "") or {}
        default_scheduler = meta.get("recommended_scheduler") or model.get("scheduler", "beta")
        default_sampler = meta.get("recommended_sampler") or model.get("sampler", "euler")
    else:
        default_scheduler = vmodels.VIDEO_SCHEDULER_WITHOUT_ACCEL
        default_sampler = vmodels.VIDEO_SAMPLER_WITHOUT_ACCEL
    scheduler = vmodels.normalize_scheduler(
        params.get("scheduler"), default_scheduler)
    sampler = vmodels.normalize_sampler(
        params.get("sampler"), default_sampler)
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
    #
    # 【2026-11-16 第5轮代码审查 C-10 修 —— 0×0 latent】
    #
    # 【原来错在哪】
    # ``vmodels.normalize_video_size(None, None)`` 返回 ``(0, 0, ["宽高必须是数字"])``，
    # 而这个函数的返回值**只用了前两个**（``_reasons`` 被丢弃）——
    # 于是 width=height=0 一路写进 MiniMaxH3ImageToVideo 的
    # width / height 槽位，产出 **0×0 的 latent**。
    #
    # 实测（可复现）：
    #   params = {"job_id":"x","duration":5,"model_key":"fl2va_pruned_int8","prompt":"p"}
    #   build_t2v_panel(params) -> 节点 105:104 的 width=0 / height=0
    #
    # 触发路径：**老任务重放**（worker 里 build_workflow 直接拿落库的 params）——
    # 面板提交与 Agent 建任务都会先过归一层（补上宽高），
    # 只有「历史任务 params 里压根没有 width/height」这一条会漏过来。
    #
    # 【为什么这里抛错而不是编一个尺寸】
    # 编尺寸 = 悄悄跑出一个用户没要求的分辨率（正是本项目反复修的
    # 「界面说谎」类缺陷）。而 0×0 提交给 ComfyUI 会得到一句英文底层报错，
    # 用户既不知道是哪个字段、也不知道怎么改。
    # 明确抛错（由调用方转成可读文案）比编造或崩在 ComfyUI 都更可执行。
    #
    # 【为什么不用 h3_video_size.resolve_size 的「面板默认 768p/16:9」回落】
    # 那个回落是给 flf/r2v 用的（它们的前端**一定**会提交档位或比例）。
    # t2v 老任务的 params 里可能两个都没有 -> 此时没有任何依据推断尺寸，
    # 只能如实说「缺尺寸」。
    width, height, _reasons = vmodels.normalize_video_size(
        params.get("width"), params.get("height"))
    if width <= 0 or height <= 0:
        raise ValueError(
            "缺少画面尺寸：width/height 均为空，且 resolution / aspect 也没给 —— "
            "请在面板上选一个分辨率档位与画面比例，或直接填宽高。"
            "（这是历史任务的兼容保护：宁可明确报错，也不编一个尺寸。）")

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

    # ---- 加速 LoRA 链（**一个槽位**：前端选哪枚就只挂哪枚）----
    #
    # 节点选择逻辑见 apply_accel_lora_nodes 的说明：
    # Turbo 系（裸键名）走 Turbo 节点（要 AdaLN 运行期重注入），
    # 前缀族（diffusion_model.*）走核心 LoraLoaderModelOnly。
    # 用错节点 = 静默失效（出片照跑但 LoRA 没生效）。
    if accel_chain:
        model_ref = apply_accel_lora_nodes(p, model_ref, accel_chain)

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
    # 【二采也走这条路】二采（SelfLift）原来被 `not use_twopass` 排除在外
    # （旧结论：「二采潜空间与标准路径不同源，接续会位移」）。
    # 2026-11-17 实测修正：网格对齐没问题、SelfLift 原生支持拷贝桥的
    # noise_mask —— 二采现在**也**走接续链路。详见下方注释。
    cont_state = _continuation_state(params)
    latent_in_ref = latent_ref
    cond_ref = ["105:104", 0]
    trim_ref = None

    # 【为什么二采现在也走这条路】原来这里是 `not use_twopass` ——
    # 二采路径下接续分支被整体跳过（不读回、不接桥）。
    # 2026-11-17 实测修正：二采**可以**接续，因为
    #   (a) 二采落盘的 latent 时间步与 17k+5 网格天然对齐
    #       （独立复核 4/4 真实落盘样本，tail_aligned 全 True）；
    #   (b) SelfLiftH3Sampler 原生读 latent 的 noise_mask
    #       （nodes.py:116-155），且 masked_model() 对**低分辨率段与
    #       高分辨率段都**装 post-CFG 钉住钩子（nodes.py:280-297）。
    # 接线姿势照 nonebot minimax_h3_v1.json 的 171 节点：
    #   SelfLift 消费**拷贝桥的 latent** + **桥第 4 路 conditioning**。
    # → 二采段现在既落盘接力棒、也能消费接力棒。
    if cont_state["enabled"] and cont_state["has_source"]:
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
        #
        # 【2026-11-17 接续】cond_ref 在接续段是**拷贝桥第 4 路**
        # （["300:bridge", 3]）—— 桥第 4 路是把上一段的钉帧
        # （minimax_keyframes）写进 conditioning 的唯一出口。
        # 二采采样器必须拿这份，否则二采段就看不到「上一段的画面钉帧」，
        # 接缝处的取景/构图会漂。
        # 姿势与 nonebot minimax_h3_v1.json 的 171 节点一致：
        #   positive = ["322", 3]（桥第 4 路）、
        #   latent_image = ["322", 0]（桥第 0 路）。
        samples_ref = _twopass_block(p, model_ref, steps, scheduler, sampler, seed,
                                     cfg, twopass, width, height, duration,
                                     latent_in_ref,
                                     cond_node_id="105:104",
                                     positive_ref=cond_ref)
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
        # ---- 画质域：跨段统计匹配（2026-11-17 修：接续点闪烁）----
        #
        # 【为什么需要】用户实测反馈「接续点有一次明显闪烁」。
        # 取证结论（线上真实产物，帧级证据）：
        #   · 钉住区**逐位保住**（段2 钉住区 vs 段1 尾 7 步：
        #     max 差 4.8e-7 = float32 舍入）→ 拷贝桥没问题；
        #   · 但接续点的**色档跳变**：二采 ON 时是段内量级的 **29.52x**，
        #     同参数二采 OFF 时只有 **5.48x**；
        #   · 机理：SelfLift 的低分辨率段把钉住区内容**降采样**后当锚点
        #     （nodes.py:299-306 low_video = interpolate(...)），
        #     抬升再回高分辨率 -> 新生成区继承了低分辨率段的色档，
        #     于是缝头几帧是「低色档过渡区」。
        #
        # 【为什么用 H3RelayPost.match_prev】节点自己的 tooltip 写着：
        #   「目标：压『成片缝上的亮度阶跃』」+「真正的用武之地是**拷贝桥**
        #   （那里 prev_tail 才是真·上段末帧）」—— 我们的路径**就是**拷贝桥。
        #   它做的是逐通道 Reinhard 式一阶+二阶矩对齐，**帧数守恒、不动音频、
        #   零采样开销**（relay_core.py:1820 match_prev_stats）。
        #
        # 【guide 接哪】300:trim 的**第 4 路** prev_tail = 本段钉住区末帧
        #   （裁重叠节点把它交接出来）—— 拷贝桥路线下它**就是**上段末帧，
        #   不是近似。
        #
        # 【为什么 stats_frames=1】节点的 tooltip 明确说：
        #   「1（默认）= 统计量取紧贴缝的那一帧，与 guide（单帧）口径一致
        #   ⇒ offs 恰是『缝上的阶跃』，首帧按权重被拉向 guide，**不会越过它**；
        #   0 = 旧口径…首帧被推过 guide，缝上凭空多出一个阶跃
        #   （2026-09-17 真渲染实测 ×12.6）。**仅作对照档，勿用于产线。**」
        # → 这里必须显式给 1，不能依赖节点默认值以外的任何取值。
        p["300:post"] = {
            "inputs": {
                "images": ["300:trim", 0],
                "guide": ["300:trim", 3],
                # 跨段统计匹配强度：只对齐统计量，不复制姿态 ⇒ 无重影。
                "match_prev": 0.5,
                "match_prev_frames": 12,
                # 护栏：逐通道增益/偏移上限（防把已通过的内容改坏）。
                "match_prev_gain_max": 1.15,
                "match_prev_offset_max": 0.06,
                "match_prev_stats_frames": 1,
                # 【跨段两项的总闸】确认 guide 是**真参照**才打勾。
                # 节点 tooltip：「不打勾（默认）⇒ match_prev 与 lowfreq_pull
                # **自动弃权**」+「**真参照只在拷贝桥下成立**」。
                # 我们的路径**就是拷贝桥**（300:bridge → 300:trim），
                # prev_tail 就是上段末帧（不是 cond 桥的近似）→ 必须打勾。
                # 不打勾的话节点会自己弃权，日志会写：
                #   「自动弃权 —— guide 未确认是真参照」
                # （2026-11-17 实测踩到：不打勾 -> match_prev 完全没生效）
                "cross_seg_ack": True,
                # 其余画质域项保持 0（不动时间轴、不引入额外处理）。
                "lowfreq_pull": 0,
                "hist_match": 0,
                "wb_match": 0,
                "settle_sharpen": 0,
                "settle_auto": 0,
            },
            "class_type": "H3RelayPost",
            "_meta": {"title": "接续·跨段统计匹配（压缝上的亮度阶跃）"},
        }
        video_images_ref = ["300:post", 0]
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
                   latent_ref: list,
                   cond_node_id: str = "105:104",
                   positive_ref: list | None = None) -> list:
    """二采（SelfLift）采样块。

    **硬约束**（实测自 comfyui-SelfLift/nodes.py）：
      * 只支持标准 Euler 采样器（s_churn=0）—— Turbo 的自定义采样器不能用，
        所以这里强制 euler；
      * transition_step 必须落在 [1, len(sigmas)-2]，
        len(sigmas) = steps + 1 -> 上限是 steps - 1；
      * rho=0 且 upscaler_model=none 会被节点直接拒绝 -> 这时给一个非零 rho。

    【2026-11-15 修复③ 交付项 3】cond_node_id 参数化：
    文生视频的条件节点是 105:104（MiniMaxH3ImageToVideo），
    首尾帧 / 参考生视频是 136（MiniMaxH3AudioConditioningT8）——
    之前这里写死 105:104，给 flf/r2v 接二采就会引用一个**不存在的节点**
    （工作流提交时报 "value not in list"）。

    【2026-11-17 接续】positive_ref 参数化：
    **positive 走哪一路**由调用方决定：
      * 非接续段 -> `[cond_node_id, 0]`（直接接条件节点）；
      * 接续段   -> `["300:bridge", 3]`（**拷贝桥第 4 路**）。
    桥第 4 路是把上一段的钉帧（minimax_keyframes）写进 conditioning 的
    唯一出口 —— 二采采样器必须拿这份，否则接缝处的取景/构图会漂。
    姿势与 nonebot `minimax_h3_v1.json` 的 171 节点完全一致：
      `positive = ["322", 3]`、`latent_image = ["322", 0]`。

    【为什么 negative 不跟着改】负面条件是 `ConditioningZeroOut`，
    作用是「把 positive 的引导反向」，零化掉正向引导即可 ——
    拷贝桥的钉帧是**内容锚**（keyframe latent），不是引导强度，
    零化它反而是对的（钉帧只该约束构图，不该当引导）。
    所以 negative 仍然接 `[cond_node_id, 0]`（与 nonebot v1 的 320 一致）。
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
    #
    # 【2026-11-17 接续】positive 走 positive_ref（接续段 = 拷贝桥第 4 路），
    # negative 仍走条件节点 —— 桥第 4 路带的是**钉帧**（内容锚），
    # 零化掉它才是对的（钉帧约束构图，不该当引导）。
    pos_ref = list(positive_ref) if positive_ref else [cond_node_id, 0]
    p["16"] = {"inputs": {"model": model_ref, "conditioning": pos_ref},
               "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
    p["320"] = {"inputs": {"conditioning": [cond_node_id, 0]},
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
            # 【2026-11-17 接续】positive 走 pos_ref（接续段 = 拷贝桥第 4 路）。
            # latent_image 走 latent_ref（接续段 = 拷贝桥第 0 路）。
            # 与 nonebot minimax_h3_v1.json 的 171 节点同姿势：
            #   positive = ["322", 3]、latent_image = ["322", 0]。
            "positive": pos_ref,
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
