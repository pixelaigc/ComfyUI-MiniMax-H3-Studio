# -*- coding: utf-8 -*-
"""MiniMax H3 的**首尾帧**与**参考生视频**工作流（含音轨三选一）。

【为什么单独一个模块】
B1 / B2 的需求在旧的 ``h3studio/workflows.py`` 里被硬塞进
``_h3_video_workflow`` / ``build_r2v``：
  * 首帧 / 尾帧只有两个槽，且必须首帧必填；
  * 参考图最多 3 张（节点上限其实是 9）；
  * 参考视频最多 3 个、参考音频最多 3 个；
  * **音轨只有一种**：用 H3 自己生成的音频，没有「用参考音乐当音轨」
    这回事。

而且旧路径用的是 ``MiniMaxH3ImageToVideo`` / ``MiniMaxH3ReferenceToVideo``
（ComfyUI 核心节点）—— 它们**不接受音频输入**：
    MiniMaxH3ImageToVideo  required: clip/vae/prompt/width/height/length
                           optional: first_frame / last_frame
    MiniMaxH3ReferenceToVideo optional: ref_images / ref_videos /
                                      ref_video_audios / ref_audios
两者都**没有 drive_audio**。

【节点能力实测结论 —— B1「首尾帧模式是否支持音乐输入」】
**支持**，但必须换一个节点：
    MiniMaxH3AudioConditioningT8（comfyui-minimax-h3-audio-T8 插件）
      optional: drive_audio / final_audio / first_frame / last_frame /
                ref_images / ref_videos / ref_video_audios / ref_audios
      outputs:  CONDITIONING / LATENT(av_latent) / AUDIO(mux_audio) / ...
它一个节点就同时覆盖了首尾帧、参考图、参考视频、参考音频、驱动音频，
并且额外输出 ``mux_audio``（成片该贴哪条音轨）。

所以本模块统一用这个节点，而不是两个核心节点。

【音轨三选一】
    (a) generated —— H3 与画面一同采样出的音频（音画同源）。
    (b) reference —— 参考音乐**贴**到成片上（不参与采样）。
    (c) video_bg —— 参考视频的背景音乐**贴**到成片上（不参与采样）。
可见性判据见 ``h3studio/h3_audio_track.py``（没有对应素材就不出现）。
"""

from __future__ import annotations

import random

from . import h3_audio_track
from . import video_models as vmodels
from . import video_workflow as vworkflow

#: T8 条件节点的类名（comfyui-minimax-h3-audio-T8 插件）
T8_CONDITIONING = "MiniMaxH3AudioConditioningT8"

#: 参考图上限（节点 schema：ref_images max=9）
MAX_REF_IMAGES = 9
#: 参考视频上限（节点 schema：ref_videos max=3）
MAX_REF_VIDEOS = 3
#: 独立参考音频上限（节点 schema：ref_audios max=3）
MAX_REF_AUDIOS = 3

#: 视频 VAE / 音频 VAE（与旧路径同一套权重）
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"
#: 文本编码器
CLIP_H3 = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"


class H3VideoRequestError(ValueError):
    """请求本身不合法（缺首帧 / 参考素材为空 / 超过上限）。"""


def _frame_expr_node(length_node_id: str, dur_node_id: str, duration: float) -> dict:
    """秒数 -> 帧数（24fps，H3 的 17k+5 网格）。"""
    return {
        length_node_id: {
            "inputs": {
                "expression": "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17",
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


def _model_chain(p: dict, params: dict, model: dict) -> list:
    """底模 + 加速 LoRA 链 -> 模型引用。

    与文生视频面板**完全同一套**逻辑（统一槽位 + 按权重形态选加载节点），
    否则「同一个 LoRA 在文生视频里生效、在首尾帧里不生效」。
    """
    p["6"] = {"inputs": {"unet_name": model["unet"], "weight_dtype": "default"},
              "class_type": "UNETLoader", "_meta": {"title": "加载DiT"}}
    model_ref = ["6", 0]
    chain = vworkflow._resolve_accel_chain(params, model)
    if chain:
        model_ref = vworkflow.apply_accel_lora_nodes(p, model_ref, chain)
    return model_ref


def _sampling_defaults(params: dict, accel_chain: list) -> tuple[int, str, str]:
    """采样档位默认值（跟着**实际挂的那枚**加速 LoRA 走）。"""
    if accel_chain:
        meta = vmodels.accel_lora_meta(accel_chain[0].get("key") or "") or {}
        default_steps = int(meta.get("recommended_steps") or 4)
        default_scheduler = meta.get("recommended_scheduler") or "beta"
        default_sampler = meta.get("recommended_sampler") or "euler"
    else:
        default_steps = vmodels.VIDEO_STEPS_WITHOUT_ACCEL
        default_scheduler = vmodels.VIDEO_SCHEDULER_WITHOUT_ACCEL
        default_sampler = vmodels.VIDEO_SAMPLER_WITHOUT_ACCEL
    steps = vmodels.normalize_steps(params.get("steps"), default_steps)
    scheduler = vmodels.normalize_scheduler(params.get("scheduler"), default_scheduler)
    sampler = vmodels.normalize_sampler(params.get("sampler"), default_sampler)
    return steps, scheduler, sampler


def _ref_image_nodes(p: dict, filenames: list) -> dict:
    """参考图 -> 节点引用（最多 9 张）。"""
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_IMAGES]):
        node_id = f"136:{201 + index}"
        p[node_id] = {
            "inputs": {"image": filename},
            "class_type": "LoadImage",
            "_meta": {"title": f"加载参考图 {index + 1}"},
        }
        # 【关键】autogrow 槽位的键名格式是 "<容器名>.<前缀><序号>"，
        # 例如 "ref_images.ref_image_0"。写成 "ref_image_0" 会被节点
        # 当成未知输入 -> 参考图**静默不生效**（画面里看不到参考内容）。
        # 格式依据：nonebot 参考工作流 minimax_h3_v1.json 的 node 136。
        out[f"ref_images.ref_image_{index}"] = [node_id, 0]
    return out


def _ref_video_nodes(p: dict, filenames: list) -> dict:
    """参考视频 -> 节点引用（最多 3 个）。"""
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_VIDEOS]):
        node_id = f"136:{301 + index}"
        p[node_id] = {
            "inputs": {"video": filename, "format": "None"},
            "class_type": "VHS_LoadVideo",
            "_meta": {"title": f"加载参考视频 {index + 1}"},
        }
        out[f"ref_videos.ref_video_{index}"] = [node_id, 0]
    return out


def _ref_audio_nodes(p: dict, filenames: list) -> dict:
    """独立参考音频 -> 节点引用（最多 3 个）。"""
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_AUDIOS]):
        node_id = f"136:{401 + index}"
        p[node_id] = {
            "inputs": {"audio": filename},
            "class_type": "VHS_LoadAudioUpload",
            "_meta": {"title": f"加载参考音频 {index + 1}"},
        }
        out[f"ref_audios.ref_audio_{index}"] = [node_id, 0]
    return out


def _ref_video_audio_nodes(p: dict, filenames: list) -> dict:
    """参考视频的背景音 -> 节点引用（最多 3 个，与视频同序号配对）。"""
    out = {}
    for index, filename in enumerate(filenames[:MAX_REF_VIDEOS]):
        node_id = f"136:{501 + index}"
        p[node_id] = {
            "inputs": {"audio": filename},
            "class_type": "VHS_LoadAudioUpload",
            "_meta": {"title": f"加载参考视频背景音 {index + 1}"},
        }
        out[f"ref_video_audios.ref_video_audio_{index}"] = [node_id, 0]
    return out


def _build_common(p: dict, params: dict, *, condition_inputs: dict,
                  duration: float, width: int, height: int,
                  unet_name: str, task_type: str) -> dict:
    """首尾帧 / 参考生视频共用的骨架。

    返回工作流 dict。
    """
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)

    # ---- 底模（参考生视频用 ref2va 权重；首尾帧用 fl2va）----
    model = {"unet": unet_name}
    model_ref = _model_chain(p, params, model)

    # ---- 条件节点（T8：同时管首尾帧 / 参考素材 / 驱动音频）----
    p["105:11"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader",
                   "_meta": {"title": "视频 VAE"}}
    p["13"] = {"inputs": {"clip_name": CLIP_H3, "type": "minimax", "device": "default"},
               "class_type": "CLIPLoader", "_meta": {"title": "加载文本编码器"}}
    p["24"] = {"inputs": {"vae_name": VAE_AUDIO}, "class_type": "VAELoader",
               "_meta": {"title": "音频 VAE"}}

    expr = _frame_expr_node("105:107", "105:111", duration)
    p.update(expr)

    cond = {
        "clip": ["13", 0],
        "video_vae": ["105:11", 0],
        "audio_vae": ["24", 0],
        "prompt": params.get("prompt", ""),
        "width": width,
        "height": height,
        "length": ["105:107", 1],
        "task_type": task_type,
        # 音画同源生成（默认档）。用户若要「用参考音乐当音轨」，
        # 那条音轨是**贴上去**的，不进 DiT 采样。
        "audio_mode": "native",
        "ref_image_size": "match",
        # 【以下四项必须显式给】T8 节点的 required 输入 —— 即使有默认值，
        # 通过 API 提交工作流时**不会**套用 schema 默认值（ComfyUI 只对
        # 提交 payload 里实际存在的键做校验/取值）。
        # 漏掉任何一个 -> 提交被拒（"Required input is missing"）。
        "audio_denoise_strength": 0.35,
        "add_source_as_reference": True,
        "prompt_primary_audio_ordinal": 1,
        "strict_prompt_tags": True,
        "reference_video_policy": "official_2_to_15s",
    }
    cond.update(condition_inputs)
    p["136"] = {"inputs": cond, "class_type": T8_CONDITIONING,
                "_meta": {"title": "H3 条件（首尾帧 / 参考 / 音轨）"}}

    # ---- 采样 ----
    accel_chain = vworkflow._resolve_accel_chain(params, model)
    steps, scheduler, sampler = _sampling_defaults(params, accel_chain)

    # shift（MiniMaxH3SigmaShift）
    model_ref = _shift_node(p, model_ref, params)

    p["9"] = {"inputs": {"scheduler": scheduler, "steps": steps, "denoise": 1,
                          "model": model_ref},
              "class_type": "BasicScheduler", "_meta": {"title": "调度器"}}
    p["17"] = {"inputs": {"sampler_name": sampler}, "class_type": "KSamplerSelect",
               "_meta": {"title": "采样器"}}
    p["15"] = {"inputs": {"noise_seed": int(seed)}, "class_type": "RandomNoise",
               "_meta": {"title": "噪波"}}
    p["16"] = {"inputs": {"model": model_ref, "conditioning": ["136", 0]},
               "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
    p["14"] = {"inputs": {"noise": ["15", 0], "guider": ["16", 0],
                           "sampler": ["17", 0], "sigmas": ["9", 0],
                           "latent_image": ["136", 1]},
               "class_type": "SamplerCustomAdvanced", "_meta": {"title": "采样"}}

    # ---- 解码 + 合成 ----
    p["10"] = {"inputs": {"samples": ["14", 0], "vae": ["105:11", 0]},
               "class_type": "VAEDecode", "_meta": {"title": "解码视频"}}
    p["23"] = {"inputs": {"samples": ["14", 0], "vae": ["24", 0]},
               "class_type": "VAEDecodeAudio", "_meta": {"title": "解码音频"}}

    # ---- 音轨三选一 ----
    audio_ref = _pick_audio_track(p, params, duration)

    p["91"] = {"inputs": {"fps": 24, "bit_depth": 8, "images": ["10", 0],
                          "audio": audio_ref},
               "class_type": "CreateVideo", "_meta": {"title": "合成视频"}}
    p["92"] = {"inputs": {"filename_prefix": f"h3studio/{params.get('mode', 'video')}_{job_id}",
                           "format": "auto", "codec": "auto", "video": ["91", 0]},
               "class_type": "SaveVideo", "_meta": {"title": "保存视频"}}
    return p


def _shift_node(p: dict, model_ref: list, params: dict) -> list:
    """插入 MiniMaxH3SigmaShift（shift 视频/音频）。"""
    if not params.get("use_sigma_shift", True):
        return model_ref
    shift_video = vmodels.normalize_shift(
        params.get("shift_video"), vmodels.VIDEO_SHIFT_DEFAULT_VIDEO)
    shift_audio = vmodels.normalize_shift(
        params.get("shift_audio"), vmodels.VIDEO_SHIFT_DEFAULT_AUDIO)
    p["139"] = {"inputs": {"model": model_ref, "shift_video": shift_video,
                           "shift_audio": shift_audio},
                "class_type": "MiniMaxH3SigmaShift",
                "_meta": {"title": "sigma shift（视频/音频）"}}
    return ["139", 0]


def _pick_audio_track(p: dict, params: dict, duration: float) -> list:
    """按「音轨三选一」选出成片该贴哪条音轨，返回 CreateVideo 的 audio 引用。

    (a) generated —— 用 H3 生成的音频（VAEDecodeAudio 的输出）。
    (b) reference —— 用参考音乐（裁到画面时长）。
    (c) video_bg  —— 用参考视频的背景音乐（裁到画面时长）。

    【为什么 reference / video_bg 要裁时长】
    外部音轨长度与画面**不一定相等**。直接接上去，CreateVideo 会按
    音轨长度截断画面（或补静音）—— 用户会看到「视频短了一截」。
    所以这里用 TrimAudioDuration 显式裁到画面时长。
    """
    ref_audios = params.get("ref_audios") or []
    ref_video_audios = params.get("ref_video_audios") or []
    source = h3_audio_track.normalize_track_source(
        params.get("audio_track_source"),
        has_reference_audio=bool(ref_audios),
        has_reference_video_audio=bool(ref_video_audios))

    if source == h3_audio_track.TRACK_REFERENCE and ref_audios:
        node_id = "150:track"
        p[node_id] = {
            "inputs": {
                "audio": ["136:401", 0],
                "start_index": 0.0,
                "duration": float(duration),
            },
            "class_type": "TrimAudioDuration",
            "_meta": {"title": "音轨·参考音乐（裁到画面时长）"},
        }
        return [node_id, 0]

    if source == h3_audio_track.TRACK_VIDEO_BG and ref_video_audios:
        node_id = "150:track"
        p[node_id] = {
            "inputs": {
                "audio": ["136:501", 0],
                "start_index": 0.0,
                "duration": float(duration),
            },
            "class_type": "TrimAudioDuration",
            "_meta": {"title": "音轨·参考视频背景音（裁到画面时长）"},
        }
        return [node_id, 0]

    # (a) 生成的音乐 —— H3 与画面一同采样出来的那条
    return ["23", 0]


def build_flf(params: dict) -> dict:
    """首尾帧生视频（首帧 / 尾帧 / 首帧+尾帧）。

    【与旧实现的关键差别】
      * 尾帧**可以单独给**（旧实现要求首帧必填，尾帧可省 —— 这条一致，
        但现在两帧都可以从素材库选，且都不传时给出明确报错）。
      * 支持**音轨三选一**（旧实现没有）。
      * 用 T8 条件节点（支持音频输入）。

    params:
      first_frame / last_frame —— 首帧 / 尾帧的 ComfyUI input 文件名
      audio_track_source      —— 音轨三选一（见 h3_audio_track）
      ref_audios              —— 参考音乐（文件名列表）
      ref_video_audios        —— 参考视频背景音（文件名列表）
    """
    from . import video_models as _vm
    first = params.get("first_frame")
    last = params.get("last_frame")
    if not first and not last:
        raise H3VideoRequestError(
            "首尾帧模式需要至少提供首帧或尾帧图片。"
            "请从素材库选择首帧 / 尾帧，或直接上传图片。")

    # 首尾帧用 FL2VA 权重（它有 I2VA / FL2VA / L2VA 任务类型）
    unet_name = _vm.model_for_mode("flf", params.get("model_key"))["unet"]

    width = int(params.get("width") or 1344)
    height = int(params.get("height") or 768)
    duration = float(params.get("duration") or 5)

    p: dict = {}
    condition_inputs: dict = {}
    if first:
        p["105:201"] = {"inputs": {"image": first}, "class_type": "LoadImage",
                        "_meta": {"title": "加载首帧"}}
        condition_inputs["first_frame"] = ["105:201", 0]
    if last:
        p["105:202"] = {"inputs": {"image": last}, "class_type": "LoadImage",
                        "_meta": {"title": "加载尾帧"}}
        condition_inputs["last_frame"] = ["105:202", 0]

    # 参考音乐 / 参考视频背景音（音轨三选一的素材）
    ref_audios = list(params.get("ref_audios") or [])[:MAX_REF_AUDIOS]
    ref_video_audios = list(params.get("ref_video_audios") or [])[:MAX_REF_VIDEOS]
    if ref_audios:
        condition_inputs.update(_ref_audio_nodes(p, ref_audios))
    if ref_video_audios:
        condition_inputs.update(_ref_video_audio_nodes(p, ref_video_audios))

    task_type = "FL2VA" if (first and last) else ("I2VA" if first else "L2VA")
    p.update(_build_common(p, params, condition_inputs=condition_inputs,
                            duration=duration, width=width, height=height,
                            unet_name=unet_name, task_type=task_type))
    return p


def build_r2v(params: dict) -> dict:
    """参考生视频：最多 9 张参考图 + N 个参考视频 + 1 段参考音乐 + 音轨三选一。

    params:
      ref_images        —— 参考图文件名列表（≤9）
      ref_videos        —— 参考视频文件名列表（≤3）
      ref_audios        —— 独立参考音乐文件名列表（≤3）
      ref_video_audios  —— 参考视频背景音文件名列表（≤3）
      audio_track_source —— 音轨三选一
    """
    from . import video_models as _vm
    ref_images = list(params.get("ref_images") or [])[:MAX_REF_IMAGES]
    ref_videos = list(params.get("ref_videos") or [])[:MAX_REF_VIDEOS]
    ref_audios = list(params.get("ref_audios") or [])[:MAX_REF_AUDIOS]
    ref_video_audios = list(params.get("ref_video_audios") or [])[:MAX_REF_VIDEOS]

    if not (ref_images or ref_videos or ref_audios or ref_video_audios):
        raise H3VideoRequestError(
            "参考生视频需要至少 1 个参考素材（图片 / 视频 / 音乐）。"
            "请从素材库选择参考图（最多 9 张）、参考视频（最多 3 个）"
            "或参考音乐（最多 3 段）。")

    # 参考生视频必须用 **Ref2VA 权重**（它才有 Ref2VA 通道）——
    # FL2VA 权重加载后参考通道不可用，等于「参考没生效」。
    # model_for_mode() 会按模式挑（用户选了 FL2VA 权重时自动换 Ref2VA）。
    unet_name = _vm.model_for_mode("r2v", params.get("model_key"))["unet"]

    width = int(params.get("width") or 1344)
    height = int(params.get("height") or 768)
    duration = float(params.get("duration") or 5)

    p: dict = {}
    condition_inputs: dict = {}
    if ref_images:
        condition_inputs.update(_ref_image_nodes(p, ref_images))
    if ref_videos:
        condition_inputs.update(_ref_video_nodes(p, ref_videos))
    if ref_audios:
        condition_inputs.update(_ref_audio_nodes(p, ref_audios))
    if ref_video_audios:
        condition_inputs.update(_ref_video_audio_nodes(p, ref_video_audios))

    p.update(_build_common(p, params, condition_inputs=condition_inputs,
                            duration=duration, width=width, height=height,
                            unet_name=unet_name, task_type="Ref2VA"))
    return p
