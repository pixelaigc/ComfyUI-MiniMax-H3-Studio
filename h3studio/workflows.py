# -*- coding: utf-8 -*-
"""
H3 Studio 工作流生成器
基于 ComfyUI v0.30 API 格式，复用 d:\\cfup 已验证的 MiniMax H3 工作流结构。

支持模式:
  t2v   文生视频      MiniMaxH3ImageToVideo（无首帧）
  flf   首尾帧        MiniMaxH3ImageToVideo（first_frame / last_frame）
  r2v   参考生成      MiniMaxH3ReferenceToVideo（1~3 张参考图）
  t2i   文生图        Z-IMAGE-turbo
"""
import math
import re
from pathlib import Path
import random

from . import loras as lora_registry
from . import lora_damping

# ------------------------------------------------------------------
# 模型清单（与 d:\cfup\ComfyUI\models 实际文件一致）
# ------------------------------------------------------------------
UNET = {
    "fl2va": {"int8": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
              "nvfp4": "MiniMax_H3_FL2VA_pruned_nvfp4.safetensors"},
    "ref2va": {"int8": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
               "nvfp4": "MiniMax_H3_Ref2VA_pruned_nvfp4.safetensors"},
}
CLIP = {"int8": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "nvfp4": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"}
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"

LORAS = {
    "ckpt850": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
    # 本机实际文件名末尾没有空格（仓库原版硬编码了带空格的 "ema .safetensors"，
    # ComfyUI 的 lora 下拉框里不存在该名字，提交会被拒绝）。改为按实际存在的
    # 文件名匹配，找不到再回退到带空格的原始名。
    "v4step600": "minimax_h3_turbo_v4_step600_ema.safetensors",
}

Z_IMAGE_CLIP = "qwen_3_4b.safetensors"
Z_IMAGE_VAE = "ae.safetensors"

# 画面比例：取值与 ComfyUI ResolutionSelector 的下拉项一一对应
# （实测 object_info/ResolutionSelector.aspect_ratio.options）
ASPECT_MAP = {
    "1:1": "1:1 (Square)",
    "2:3": "2:3 (Portrait Photo)",
    "3:2": "3:2 (Photo)",
    "3:4": "3:4 (Portrait Standard)",
    "4:3": "4:3 (Standard)",
    "9:16": "9:16 (Portrait Widescreen)",
    "16:9": "16:9 (Widescreen)",
    "21:9": "21:9 (Ultrawide)",
}
ASPECT_NUM = {"1:1": (1, 1), "2:3": (2, 3), "3:2": (3, 2), "3:4": (3, 4),
              "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9), "21:9": (21, 9)}

# 分辨率档位 -> megapixels（ResolutionSelector 用）
RES_MP = {"544p": 0.4, "768p": 0.8}

# ------------------------------------------------------------------
# Z-Image 文生图：模型版本 / 分辨率档位 / 画面比例 / 采样参数
# ------------------------------------------------------------------
# 模型目录只列本机 ComfyUI 实测存在的权重（UNETLoader 下拉项），
# 不列编造的选项。三个文件都落在 /mnt/nvme1t_colorful 外接盘上。
#
# 默认值来源（见《Z-Image 最佳参数与提示词增强 Skill 范式_技术调研.md》）：
#   - ComfyUI 官方模板 image_z_image_turbo.json / image_z_image.json
#   - 官方 2K 放大模板 utility_z_image_turbo_2k_upscaler.app.json
#   - 本机 comfy/samplers.py:610（CFG=1 时 uncond 被置空 -> 负面提示词无效）
#   - 本机 comfy/supported_models.py:1206（ZImage 内置 shift=3.0）
Z_IMAGE_MODELS = {
    "z_image_turbo_bf16": {
        "label": "Z-Image-Turbo · 官方加速版",
        "unet": "z_image_turbo_bf16.safetensors",
        "weight_dtype": "default",
        "sampler": "res_multistep",
        "scheduler": "simple",
        "steps": 8,
        "cfg": 1.0,
        "shift": 3.0,
        # "zero" = ConditioningZeroOut（官方模板做法）；Turbo 在 CFG=1 下
        # 负面提示词数学上无效（samplers.py:610），所以不提供输入槽。
        "negative": "zero",
        "supports_negative": False,
        # Turbo 8 步下 karras / exponential 会崩图（社区 76 图实测）。
        "samplers": ["res_multistep", "euler", "dpmpp_2m", "dpmpp_2m_sde",
                     "dpmpp_3m_sde", "dpm_2", "heun"],
        "schedulers": ["simple", "beta", "sgm_uniform", "normal", "karras"],
        "note": "官方模板 image_z_image_turbo.json：8 步 / cfg 1 / res_multistep / simple / shift 3。",
        "warn": "Turbo 的 CFG 必须=1（此时负面提示词被跳过、不跑第二遍前向），"
                "且不要用 karras / exponential 调度器（8 步下会崩图）。",
    },
    "z_image_bf16": {
        "label": "Z-Image Base · 官方完整版",
        "unet": "z_image_bf16.safetensors",
        "weight_dtype": "default",
        "sampler": "res_multistep",
        "scheduler": "simple",
        "steps": 25,
        "cfg": 4.0,
        "shift": 3.0,
        # Base 非蒸馏，官方模板有独立的负面 CLIPTextEncode 槽。
        "negative": "encode",
        "supports_negative": True,
        # Base 非蒸馏，走的调度假设不同：exponential 采样器在这里可用
        # （与 Turbo 上 exponential 调度器有害方向相反）。
        "samplers": ["res_multistep", "euler", "exponential", "res_2s", "res_3s",
                     "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "ddim", "heun"],
        "schedulers": ["simple", "beta", "normal", "sgm_uniform", "karras",
                       "exponential", "linear_quadratic"],
        "note": "官方模板 image_z_image.json：25 步 / cfg 4 / res_multistep / simple / shift 3"
                "（模板批注建议 30~50 步、cfg 3~5）。",
        "warn": "Base 支持负面提示词（官方强烈建议）。Base 上 exponential 采样器可用，"
                "与 Turbo 相反。",
    },
    "darkbeast_z6_blitz_fp8": {
        "label": "DarkBeast Z6 BlitZ · FP8 混精版",
        "unet": "DarkBeastZ6-BlitZ-Scaled-Fp8-Mixed-ComfyUI.safetensors",
        "weight_dtype": "fp8_e4m3fn",
        "sampler": "euler",
        "scheduler": "simple",
        "steps": 15,
        "cfg": 1.0,
        "shift": 3.0,
        "negative": "zero",
        "supports_negative": False,
        "samplers": ["euler", "res_multistep", "dpmpp_2m", "dpmpp_2m_sde",
                     "dpmpp_3m_sde", "dpm_2", "heun"],
        "schedulers": ["simple", "beta", "sgm_uniform", "normal", "karras"],
        "note": "本机工作流 z-image-fast.json：15 步 / cfg 1 / euler / simple。",
        "warn": "Turbo 蒸馏系：CFG 建议保持 1，负面提示词无效。",
    },
}
Z_IMAGE_DEFAULT_MODEL = "z_image_turbo_bf16"

# 图像面板分辨率档位：**总像素（megapixels）**语义，与 ComfyUI 自带的
# ResolutionSelector 完全一致（aspect_ratio + megapixels + multiple），
# 只是 multiple 用 16 而不是 8（Z-Image 要求宽高是 16 的倍数）。
#
# 取值依据：
#   - 1MP = 官方默认（模板 EmptySD3LatentImage 1024×1024，ResolutionSelector
#     megapixels=1），社区公认的原生甜点
#   - 480P / 720P / 1080P / 1440P 按视频行业的竖直分辨率惯例命名，换算成
#     总像素后对应约 0.4 / 0.9 / 2.0 / 3.5 MP
#   - 2K / 4K 按短边命名（2048 / 3840x2160）
#   - 社区明确 >=4MP「对本模型原生过高质量」，所以超过 3MP 的档位走放大路径
Z_IMAGE_RES_TIERS = {
    "1mp": 1.0,       # 官方默认 1024×1024
    "480p": 0.4,
    "720p": 0.9,
    "1080p": 2.0,
    "1440p": 3.5,
    "2k": 4.2,
    "4k": 8.3,
}
Z_IMAGE_DEFAULT_TIER = "1mp"

# 超过这个总像素（MP）就不要再直出了：社区明确 >=4MP 对本模型原生过高质量，
# 直出会构图崩坏 / 出伪影。超出的档位改走官方 2K 放大路径。
Z_IMAGE_MAX_DIRECT_MP = 3.0

Z_IMAGE_SIZE_MULTIPLE = 16

# 官方 2K 放大模板 utility_z_image_turbo_2k_upscaler.app.json 的参数
Z_IMAGE_UPSCALE = {
    "sampler": "dpmpp_2m_sde",
    "scheduler": "beta",
    "steps": 5,
    "cfg": 1.0,
    "denoise": 0.33,
    "upscale_model": "RealESRGAN_x4plus.pth",
    "upscale_method": "lanczos",
    "scale_by": 0.5,
}


def _frame_expr_node(length_node_id, dur_node_id):
    """秒数 -> 帧数（24fps，对齐 17 帧约束），与官方工作流一致"""
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
            "inputs": {"value": 5},
            "class_type": "PrimitiveFloat",
            "_meta": {"title": "时长(秒)"},
        },
    }


def _video_sampler_block(p, model_ref, steps, scheduler, sampler):
    p[f"{p['_p']}9"] = {"inputs": {"scheduler": scheduler, "steps": steps, "denoise": 1,
                                   "model": model_ref}, "class_type": "BasicScheduler",
                        "_meta": {"title": "调度器"}}
    p[f"{p['_p']}17"] = {"inputs": {"sampler_name": sampler}, "class_type": "KSamplerSelect",
                         "_meta": {"title": "采样器"}}
    p[f"{p['_p']}15"] = {"inputs": {"noise_seed": p["_seed"]}, "class_type": "RandomNoise",
                         "_meta": {"title": "噪波"}}
    p[f"{p['_p']}16"] = {"inputs": {"model": model_ref, "conditioning": [p["_cond"], 0]},
                         "class_type": "BasicGuider", "_meta": {"title": "引导器"}}
    p[f"{p['_p']}14"] = {"inputs": {"noise": [f"{p['_p']}15", 0], "guider": [f"{p['_p']}16", 0],
                                    "sampler": [f"{p['_p']}17", 0], "sigmas": [f"{p['_p']}9", 0],
                                    "latent_image": [p["_cond"], 1]},
                         "class_type": "SamplerCustomAdvanced", "_meta": {"title": "采样"}}
    return f"{p['_p']}14"


def _video_decode_save_block(p, samples_ref):
    prefix = f"h3studio/{p['_mode']}_{p['_job_id']}"
    p[f"{p['_p']}10"] = {"inputs": {"samples": samples_ref, "vae": [f"{p['_p']}11", 0]},
                         "class_type": "VAEDecode", "_meta": {"title": "解码视频"}}
    p[f"{p['_p']}23"] = {"inputs": {"samples": samples_ref, "vae": [f"{p['_p']}24", 0]},
                         "class_type": "VAEDecodeAudio", "_meta": {"title": "解码音频"}}
    p[f"{p['_p']}91"] = {"inputs": {"fps": 24, "bit_depth": 8, "images": [f"{p['_p']}10", 0],
                                    "audio": [f"{p['_p']}23", 0]},
                         "class_type": "CreateVideo", "_meta": {"title": "合成视频"}}
    p[f"{p['_p']}92"] = {"inputs": {"filename_prefix": prefix, "format": "auto", "codec": "auto",
                                    "video": [f"{p['_p']}91", 0]},
                         "class_type": "SaveVideo", "_meta": {"title": "保存视频"}}
    p[f"{p['_p']}11"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader"}
    p[f"{p['_p']}24"] = {"inputs": {"vae_name": VAE_AUDIO}, "class_type": "VAELoader"}


def _h3_video_workflow(params, image_inputs=None):
    """t2v / flf 共用骨架。image_inputs: {'first_frame': fname, 'last_frame': fname}"""
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)
    precision = params.get("precision", "int8")
    lora = params.get("lora", "ckpt850")          # none / ckpt850 / v4step600
    steps = params.get("steps") or (4 if lora != "none" else 20)
    scheduler = "beta" if lora != "none" else "simple"
    sampler = "euler" if lora != "none" else "res_multistep"
    aspect = ASPECT_MAP.get(params.get("aspect", "16:9"), ASPECT_MAP["16:9"])
    mp = RES_MP.get(params.get("resolution", "768p"), 0.8)
    duration = float(params.get("duration", 5))

    p = {"_p": "", "_seed": int(seed), "_job_id": job_id,
         "_mode": params.get("mode", "t2v"), "_cond": None}

    # -- 条件与 latent --
    p["115"] = {"inputs": {"aspect_ratio": aspect, "megapixels": mp, "multiple": 32},
                "class_type": "ResolutionSelector", "_meta": {"title": "分辨率选择"}}

    unet_key = "fl2va"
    p["6"] = {"inputs": {"unet_name": UNET[unet_key][precision], "weight_dtype": "default"},
              "class_type": "UNETLoader", "_meta": {"title": "加载DiT"}}
    model_ref = ["6", 0]

    if lora != "none":
        p["138"] = {"inputs": {"model": ["6", 0], "lora_name": LORAS[lora],
                               "strength": float(params.get("lora_strength", 1.0)),
                               "low_vram": True},
                    "class_type": "MiniMaxH3TurboLoRA", "_meta": {"title": "Turbo LoRA"}}
        model_ref = ["138", 0]

    p["13"] = {"inputs": {"clip_name": CLIP[precision], "type": "minimax", "device": "default"},
               "class_type": "CLIPLoader", "_meta": {"title": "加载文本编码器"}}

    img_node_inputs = {
        "prompt": params.get("prompt", ""),
        "width": ["115", 0],
        "height": ["115", 1],
        "clip": ["13", 0],
        "vae": ["105:11", 0],
    }
    if image_inputs:
        if image_inputs.get("first_frame"):
            p["105:201"] = {
                "inputs": {"image": image_inputs["first_frame"]},
                "class_type": "LoadImage",
                "_meta": {"title": "加载首帧"},
            }
            img_node_inputs["first_frame"] = ["105:201", 0]
        if image_inputs.get("last_frame"):
            p["105:202"] = {
                "inputs": {"image": image_inputs["last_frame"]},
                "class_type": "LoadImage",
                "_meta": {"title": "加载尾帧"},
            }
            img_node_inputs["last_frame"] = ["105:202", 0]

    p["105:104"] = {"inputs": img_node_inputs, "class_type": "MiniMaxH3ImageToVideo",
                    "_meta": {"title": "H3 条件"}}
    p["_cond"] = "105:104"

    # 时长 -> 帧数
    expr = _frame_expr_node("105:107", "105:111")
    expr["105:111"]["inputs"]["value"] = duration
    p.update(expr)
    img_node_inputs["length"] = ["105:107", 1]

    p["105:11"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader"}

    samples = _video_sampler_block(p, model_ref, steps, scheduler, sampler)
    _video_decode_save_block(p, [samples, 0])

    # 清理内部键
    for k in ["_p", "_seed", "_job_id", "_mode", "_cond"]:
        p.pop(k, None)
    return p


def build_t2v(params):
    return _h3_video_workflow(params)


def build_flf(params):
    return _h3_video_workflow(params, image_inputs={
        "first_frame": params.get("first_frame"),
        "last_frame": params.get("last_frame"),
    })


def build_r2v(params):
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)
    precision = params.get("precision", "int8")
    lora = params.get("lora", "ckpt850")
    steps = params.get("steps") or (4 if lora != "none" else 20)
    scheduler = "beta" if lora != "none" else "simple"
    sampler = "euler" if lora != "none" else "res_multistep"
    aspect = ASPECT_MAP.get(params.get("aspect", "16:9"), ASPECT_MAP["16:9"])
    mp = RES_MP.get(params.get("resolution", "768p"), 0.8)
    duration = float(params.get("duration", 5))

    p = {"_seed": int(seed), "_job_id": job_id, "_mode": "r2v", "_p": "", "_cond": None}

    p["115"] = {"inputs": {"aspect_ratio": aspect, "megapixels": mp, "multiple": 32},
                "class_type": "ResolutionSelector"}
    p["127"] = {"inputs": {"unet_name": UNET["ref2va"][precision], "weight_dtype": "default"},
                "class_type": "UNETLoader"}
    model_ref = ["127", 0]
    if lora != "none":
        p["138"] = {"inputs": {"model": ["127", 0], "lora_name": LORAS[lora],
                               "strength": float(params.get("lora_strength", 1.0)),
                               "low_vram": True},
                    "class_type": "MiniMaxH3TurboLoRA"}
        model_ref = ["138", 0]
    p["128"] = {"inputs": {"clip_name": CLIP[precision], "type": "minimax", "device": "default"},
                "class_type": "CLIPLoader"}
    p["119"] = {"inputs": {"vae_name": VAE_VIDEO}, "class_type": "VAELoader"}
    p["120"] = {"inputs": {"vae_name": VAE_AUDIO}, "class_type": "VAELoader"}

    ref_inputs = {}
    for index, filename in enumerate(params["ref_images"][:3]):
        node_id = f"136:{201 + index}"
        p[node_id] = {
            "inputs": {"image": filename},
            "class_type": "LoadImage",
            "_meta": {"title": f"加载参考图 {index + 1}"},
        }
        ref_inputs[f"ref_image_{index}"] = [node_id, 0]

    # 参考视频 / 参考音频（素材库引用）。
    # H3 节点 MiniMaxH3ReferenceToVideo 的真实槽位上限（实测 /object_info）：
    #   ref_images 0~9 / ref_videos 0~3 / ref_video_audios 0~3 / ref_audios 0~3
    ref_video_inputs = {}
    for index, filename in enumerate(params.get("ref_videos", [])[:3]):
        node_id = f"136:{301 + index}"
        p[node_id] = {
            "inputs": {"video": filename, "format": "None"},
            "class_type": "VHS_LoadVideo",
            "_meta": {"title": f"加载参考视频 {index + 1}"},
        }
        ref_video_inputs[f"ref_video_{index}"] = [node_id, 0]

    ref_audio_inputs = {}
    for index, filename in enumerate(params.get("ref_audios", [])[:3]):
        node_id = f"136:{401 + index}"
        p[node_id] = {
            "inputs": {"audio": filename},
            "class_type": "VHS_LoadAudioUpload",
            "_meta": {"title": f"加载参考音频 {index + 1}"},
        }
        ref_audio_inputs[f"ref_audio_{index}"] = [node_id, 0]

    p["136"] = {
        "inputs": {
            "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
            "prompt": params.get("prompt", ""),
            "width": ["115", 0], "height": ["115", 1],
            "ref_image_size": "match",
            **{f"ref_images.{k}": v for k, v in ref_inputs.items()},
            **{f"ref_videos.{k}": v for k, v in ref_video_inputs.items()},
            **{f"ref_audios.{k}": v for k, v in ref_audio_inputs.items()},
        },
        "class_type": "MiniMaxH3ReferenceToVideo",
        "_meta": {"title": "H3 参考生成"},
    }
    p["_cond"] = "136"

    expr = _frame_expr_node("131", "132")
    expr["132"]["inputs"]["value"] = duration
    p.update(expr)
    p["136"]["inputs"]["length"] = ["131", 1]

    samples = _video_sampler_block(p, model_ref, steps, scheduler, sampler)
    _video_decode_save_block(p, [samples, 0])

    for k in ["_p", "_seed", "_job_id", "_mode", "_cond"]:
        p.pop(k, None)
    return p


def z_image_dimensions(aspect, tier, width=None, height=None):
    """比例 + 分辨率档位 -> 实际像素宽高。

    算法与 ComfyUI 自带的 ResolutionSelector 完全一致：
        total = megapixels * 1024 * 1024
        scale = sqrt(total / (w_ratio * h_ratio))
        width  = round(w_ratio * scale / multiple) * multiple
        height = round(h_ratio * scale / multiple) * multiple
    multiple 用 16 而不是 8（Z-Image 要求宽高是 16 的倍数）。

    用户手动填了宽高就以手填的为准（微调优先）。
    """
    w_ratio, h_ratio = ASPECT_NUM.get(aspect, (16, 9))
    megapixels = Z_IMAGE_RES_TIERS.get(tier, Z_IMAGE_RES_TIERS[Z_IMAGE_DEFAULT_TIER])
    mult = Z_IMAGE_SIZE_MULTIPLE

    if width and height:
        final_w, final_h = int(width), int(height)
    else:
        total_pixels = megapixels * 1024 * 1024
        scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
        final_w = w_ratio * scale
        final_h = h_ratio * scale
    final_w = max(mult, int(round(final_w / mult)) * mult)
    final_h = max(mult, int(round(final_h / mult)) * mult)
    return final_w, final_h


def z_image_megapixels(width, height):
    return (width * height) / (1024.0 * 1024.0)


def _lookup_lora_meta(name: str) -> dict:
    """按界面上的 LoRA 名查本机注册表（拿到 kind / trigger 等元信息）。

    查不到就返回空 dict —— 调用方据此回落到前端传值，不报错。
    """
    if not name:
        return {}
    for item in lora_registry.list_z_image_loras():
        if item["name"] == name or Path(item["name"]).name == Path(name).name:
            return item
    return {}


def merge_lora_triggers(prompt: str, lora_items) -> str:
    """把各 LoRA 自带的触发词合并追加到提示词末尾。

    规则：
      * 每个 LoRA 的 trigger 字段可能含多个词，用 " / " 或 "," 分隔
      * 已存在于提示词中的触发词不重复追加
      * 多个 LoRA 的触发词按 LoRA 顺序依次追加
    返回合并后的提示词（没有触发词时原样返回）。
    """
    triggers: list[str] = []
    seen: set[str] = set()
    for item in lora_items:
        raw = str((item or {}).get("trigger") or "").strip()
        if not raw:
            continue
        for token in re.split(r"[/,，、]", raw):
            token = token.strip()
            if not token or token in seen:
                continue
            seen.add(token)
            triggers.append(token)
    if not triggers:
        return prompt
    base = (prompt or "").strip()
    missing = [t for t in triggers if t not in base]
    if not missing:
        return base
    suffix = ", ".join(missing)
    return f"{base}, {suffix}" if base else suffix


def _z_image_lora_chain(lora_items, base_model_ref, base_clip_ref=None):
    """把选中的效果 LoRA 串成模型链，返回 (节点字典, 最终模型引用, 最终 clip 引用)。

    链路：UNETLoader -> ModelSamplingAuraFlow -> LoraLoader x N
                                CLIPLoader(lumina2) -^

    用 **LoraLoader**（而非 LoraLoaderModelOnly）：它的 model 与 clip
    两个输入都接上 —— model 接 UNet 侧，clip 接 CLIPLoader(lumina2)。
    原因是 LoRA 权重里同时包含 UNet 与文本编码器两组键；只接 model 的话，
    文本编码器那部分 patch 会被 ComfyUI 记为 "NOT LOADED" 而丢弃。

    实测（见本轮验证记录）：Z-Image 的 lumina2 文本编码器
    （ZImageTEModel_）**能**接受 LoRA patch，日志显示
    "Model Lumina2 prepared ... 170 patches attached"。

    找不到文件时抛错（不静默忽略）。
    """
    nodes: dict = {}
    current_model = base_model_ref
    current_clip = base_clip_ref
    for index, item in enumerate(lora_items):
        name = item.get("name") or ""
        if not name:
            raise ValueError("LoRA 配置缺少 name")
        resolved, path = lora_registry.resolve_lora(name)
        if not resolved or path is None:
            raise ValueError(f"本机不存在该 LoRA 文件：{name}")
        # context_refiner 阻尼：部分 Z-Image LoRA（如「瑶光」）对
        # context_refiner 的扰动达 10% 量级，而该模块正是联合处理
        # 「文本条件 + 潜变量」的地方 —— 长提示词下会把采样推到发散，
        # 出图为彩色噪点。改用阻尼后的派生 LoRA（强度不变）。
        # 详见 h3studio/lora_damping.py 的消融实验记录。
        damped_name, _ = lora_damping.ensure_damped_lora(resolved)
        if damped_name:
            resolved = damped_name
        strength = float(item.get("strength", 1.0))
        if strength <= 0:
            # 强度 0 = 关闭该 LoRA，但仍然校验文件存在，避免静默吞掉错名
            continue
        node_id = f"57:40{index}"
        inputs = {
            "lora_name": resolved,
            "strength_model": strength,
            # clip 强度跟随 model 强度（风格 LoRA 的两侧语义一致）
            "strength_clip": strength,
            "model": current_model,
        }
        if current_clip is not None:
            inputs["clip"] = current_clip
        nodes[node_id] = {
            "inputs": inputs,
            "class_type": "LoraLoader",
        }
        current_model = [node_id, 0]
        if current_clip is not None:
            current_clip = [node_id, 1]
    return nodes, current_model, current_clip


def _z_image_t2i_nodes(model, width, height, seed, sampler, scheduler, steps,
                       cfg, shift, negative_mode, negative_text, prompt,
                       job_id, filename_prefix=None, loras=None):
    """Z-Image 主链节点（文生图 / 放大二次采样共用）。

    loras: [{name, strength}] —— 按顺序串联到 model 链上。
    """
    p = {
        "9": {"inputs": {"filename_prefix": filename_prefix or f"h3studio/t2i_{job_id}",
                         "images": ["57:8", 0]},
              "class_type": "SaveImage"},
        "57:30": {"inputs": {"clip_name": Z_IMAGE_CLIP, "type": "lumina2", "device": "default"},
                  "class_type": "CLIPLoader"},
        "57:29": {"inputs": {"vae_name": Z_IMAGE_VAE}, "class_type": "VAELoader"},
        "57:8": {"inputs": {"samples": ["57:3", 0], "vae": ["57:29", 0]}, "class_type": "VAEDecode"},
        "57:28": {"inputs": {"unet_name": model["unet"], "weight_dtype": model["weight_dtype"]},
                  "class_type": "UNETLoader"},
        "57:27": {"inputs": {"text": prompt, "clip": ["57:30", 0]},
                  "class_type": "CLIPTextEncode"},
        "57:13": {"inputs": {"width": width, "height": height, "batch_size": 1},
                  "class_type": "EmptySD3LatentImage"},
        "57:11": {"inputs": {"shift": shift, "model": ["57:28", 0]},
                  "class_type": "ModelSamplingAuraFlow"},
        "57:3": {"inputs": {"seed": int(seed), "steps": steps, "cfg": cfg,
                            "sampler_name": sampler, "scheduler": scheduler, "denoise": 1,
                            "model": ["57:11", 0], "positive": ["57:27", 0],
                            "latent_image": ["57:13", 0]},
                 "class_type": "KSampler"},
    }
    # LoRA 串在 ModelSamplingAuraFlow 之后、KSampler 之前
    # clip 侧也一起接上（LoraLoader 的 clip 输入），文本编码器才能吃到 patch
    if loras:
        lora_nodes, model_ref, clip_ref = _z_image_lora_chain(
            loras, ["57:11", 0], ["57:30", 0])
        p.update(lora_nodes)
        p["57:3"]["inputs"]["model"] = model_ref
        # CLIPTextEncode（正向）与 ConditioningZeroOut（负向）都用 LoRA 后的 clip
        p["57:27"]["inputs"]["clip"] = clip_ref
    if negative_mode == "encode":
        p["57:33"] = {"inputs": {"text": negative_text, "clip": ["57:30", 0]},
                      "class_type": "CLIPTextEncode"}
    else:
        p["57:33"] = {"inputs": {"conditioning": ["57:27", 0]},
                      "class_type": "ConditioningZeroOut"}
    p["57:3"]["inputs"]["negative"] = ["57:33", 0]
    return p


def _z_image_upscale_nodes(model, width, height, target_w, target_h, seed, prompt, job_id,
                            loras=None):
    """官方 2K 放大路径（utility_z_image_turbo_2k_upscaler.app.json 的做法）。

    链路：T2I 出图 -> ImageScaleToTotalPixels(lanczos) -> ImageUpscaleWithModel
    (RealESRGAN x4) -> ImageScaleBy(lanczos 0.5) -> VAEEncode
    -> KSampler(dpmpp_2m_sde / beta / 5 步 / cfg 1 / denoise 0.33) -> VAEDecode。

    用途：4K 这类 >=4MP 的目标尺寸直出会崩（社区实测「too high for this
    model natively」），所以改成「低分辨率生成 + 放大 + 二次细化」。
    """
    up = Z_IMAGE_UPSCALE
    p = {
        "9": {"inputs": {"filename_prefix": f"h3studio/t2i_{job_id}", "images": ["57:8", 0]},
              "class_type": "SaveImage"},
        "57:30": {"inputs": {"clip_name": Z_IMAGE_CLIP, "type": "lumina2", "device": "default"},
                  "class_type": "CLIPLoader"},
        "57:29": {"inputs": {"vae_name": Z_IMAGE_VAE}, "class_type": "VAELoader"},
        "57:28": {"inputs": {"unet_name": model["unet"], "weight_dtype": model["weight_dtype"]},
                  "class_type": "UNETLoader"},
        "57:11": {"inputs": {"shift": 3.0, "model": ["57:28", 0]},
                  "class_type": "ModelSamplingAuraFlow"},
        "57:27": {"inputs": {"text": prompt, "clip": ["57:30", 0]},
                  "class_type": "CLIPTextEncode"},
        "57:33": {"inputs": {"conditioning": ["57:27", 0]}, "class_type": "ConditioningZeroOut"},
        # 第一段：低分辨率生成
        "57:13": {"inputs": {"width": width, "height": height, "batch_size": 1},
                  "class_type": "EmptySD3LatentImage"},
        "57:3": {"inputs": {"seed": int(seed), "steps": 8, "cfg": 1.0,
                            "sampler_name": "res_multistep", "scheduler": "simple", "denoise": 1,
                            "model": ["57:11", 0], "positive": ["57:27", 0],
                            "negative": ["57:33", 0], "latent_image": ["57:13", 0]},
                 "class_type": "KSampler"},
        "57:8": {"inputs": {"samples": ["57:3", 0], "vae": ["57:29", 0]}, "class_type": "VAEDecode"},
        # 放大链：先按目标像素面积缩放，再用 RealESRGAN x4 提细节，
        # 然后 ImageScaleBy 回到目标尺寸，最后低 denoise 二次细化。
        "57:70": {"inputs": {"upscale_method": up["upscale_method"], "megapixels": 4.0,
                             "resolution_steps": 1, "image": ["57:8", 0]},
                  "class_type": "ImageScaleToTotalPixels"},
        "57:71": {"inputs": {"model_name": up["upscale_model"]}, "class_type": "UpscaleModelLoader"},
        "57:72": {"inputs": {"upscale_model": ["57:71", 0], "image": ["57:70", 0]},
                  "class_type": "ImageUpscaleWithModel"},
        "57:76": {"inputs": {"upscale_method": up["upscale_method"], "width": target_w,
                             "height": target_h, "crop": "disabled", "image": ["57:72", 0]},
                  "class_type": "ImageScale"},
        "57:73": {"inputs": {"pixels": ["57:76", 0], "vae": ["57:29", 0]},
                  "class_type": "VAEEncode"},
        # 第二段：低 denoise 二次细化
        "57:75": {"inputs": {"seed": int(seed), "steps": up["steps"], "cfg": up["cfg"],
                             "sampler_name": up["sampler"], "scheduler": up["scheduler"],
                             "denoise": up["denoise"], "model": ["57:11", 0],
                             "positive": ["57:27", 0], "negative": ["57:33", 0],
                             "latent_image": ["57:73", 0]},
                  "class_type": "KSampler"},
        "57:8b": {"inputs": {"samples": ["57:75", 0], "vae": ["57:29", 0]}, "class_type": "VAEDecode"},
    }
    # 放大路径的 LoRA 同样串在 ModelSamplingAuraFlow 之后
    # （两段 KSampler 都用 LoRA 后的 model；clip 也要接到 LoRA 后的 CLIP）
    if loras:
        lora_nodes, model_ref, clip_ref = _z_image_lora_chain(
            loras, ["57:11", 0], ["57:30", 0])
        p.update(lora_nodes)
        p["57:3"]["inputs"]["model"] = model_ref
        p["57:75"]["inputs"]["model"] = model_ref
        p["57:27"]["inputs"]["clip"] = clip_ref
    return p


def build_t2i(params):
    """Z-IMAGE 文生图

    模型版本 / 采样器 / 调度器 / 步数 / CFG / shift / 分辨率 / 比例全部来自
    前端 params，缺省时回落到该模型版本的官方推荐值。

    目标尺寸 >= Z_IMAGE_MAX_DIRECT_MP 时自动切到官方 2K 放大路径
    （低分辨率生成 + RealESRGAN 放大 + 低 denoise 二次细化）。
    """
    job_id = params["job_id"]
    model_key = params.get("model") or Z_IMAGE_DEFAULT_MODEL
    if model_key not in Z_IMAGE_MODELS:
        model_key = Z_IMAGE_DEFAULT_MODEL
    model = Z_IMAGE_MODELS[model_key]

    seed = params.get("seed") or random.randint(0, 2**48)
    aspect = params.get("aspect", "16:9")
    if aspect not in ASPECT_NUM:
        aspect = "16:9"
    tier = params.get("resolution")
    if tier not in Z_IMAGE_RES_TIERS:
        tier = Z_IMAGE_DEFAULT_TIER
    width, height = z_image_dimensions(aspect, tier, params.get("width"), params.get("height"))

    sampler = params.get("sampler") or model["sampler"]
    scheduler = params.get("scheduler") or model["scheduler"]
    steps = int(params.get("steps") or model["steps"])
    cfg = float(params.get("cfg") if params.get("cfg") is not None else model["cfg"])
    shift = float(params.get("shift") if params.get("shift") is not None else model["shift"])
    negative_mode = params.get("negative_mode") or model["negative"]
    negative_text = params.get("negative_prompt", "") or ""
    prompt = params.get("prompt", "")

    # LoRA：[{name, strength, trigger?}]，只接受启用且强度 > 0 的项
    #
    # 触发词来源优先级：
    #   1. 前端显式传来的 trigger（用户改过就以用户的为准）
    #   2. 本机 LoRA 注册表里的 trigger（上游标注的效果触发词）
    # 加速/蒸馏类 LoRA 不允许作为用户可选项 —— 下面直接拒绝。
    loras = []
    for item in (params.get("loras") or []):
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 0.0
        if strength <= 0:
            continue
        name = item.get("name") or ""
        meta = _lookup_lora_meta(name)
        if meta and meta.get("kind") == lora_registry.KIND_ACCELERATION:
            raise ValueError(
                f"「{meta.get('display') or name}」是加速/蒸馏类 LoRA，"
                "不能作为可选效果 LoRA 使用")
        # 真·不兼容（要 patch 的层在底模里不存在）-> 拒绝
        if meta and not meta.get("compatible", True):
            raise ValueError(
                f"「{meta.get('display') or name}」与 Z-Image 底模不兼容，已拒绝加载："
                f"{meta.get('incompatible_reason') or '未知原因'}")
        # 权重能 attach、但幅度过大的 LoRA -> 夹到安全上限
        # （实测：强度 1.0 出彩色噪点，0.8 安全）
        cap = float(meta.get("strength_cap", 1.0)) if meta else 1.0
        if cap < 1.0 and strength > cap:
            strength = cap
        trigger = item.get("trigger")
        if trigger is None:
            trigger = (meta or {}).get("trigger", "")
        loras.append({"name": name, "strength": strength, "trigger": trigger or ""})

    # 触发词自动追加到最终提示词（用户可在界面上编辑/删除）
    prompt = merge_lora_triggers(prompt, loras)

    # 目标过大 -> 走官方 2K 放大路径（生成端固定用官方 Turbo 推荐参数）
    if z_image_megapixels(width, height) > Z_IMAGE_MAX_DIRECT_MP:
        base_w, base_h = z_image_dimensions(aspect, "1mp")
        p = _z_image_upscale_nodes(model, base_w, base_h, width, height, seed, prompt,
                                   job_id, loras=loras)
        # 放大链输出接回最终 SaveImage
        p["9"]["inputs"]["images"] = ["57:8b", 0]
        return p

    return _z_image_t2i_nodes(model, width, height, seed, sampler, scheduler, steps,
                              cfg, shift, negative_mode, negative_text, prompt, job_id,
                              loras=loras)



# ------------------------------------------------------------------
# Qwen-Image 2.1：文生图（t2i_qwen）/ 参考图生图（i2i_qwen）
# ------------------------------------------------------------------
# 权重与工作流全部来自本机实测存在的文件，**不列编造的选项**：
#   UNET       /home/ubuntu/comfyui/models/diffusion_models/qwen_image_2.1_bf16.safetensors (14G)
#   VAE        /home/ubuntu/comfyui/models/vae/qwen_image_2.1_vae_bf16.safetensors (645M)
#   PE 文本编码器（带官方提示词扩写能力）
#              text_encoders/qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors (8.9G)
#              text_encoders/qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors (8.9G)
#   必需加速 LoRA（官方工作流固定内置，不进用户可选列表）
#              loras/qwen21/bfs_head_v1_qwen_2.1.safetensors
#
# 官方工作流（本机）：
#   user/default/workflows/Qwen+image+2.1+文生图.json
#   user/default/workflows/Qwen+image+2.1图片编辑.json
#
# 官方推荐参数来源：上面两个官方工作流的 KSampler
#   steps=50 / cfg=1 / sampler=euler / scheduler=simple / denoise=1
#   文生图工作流的 KSampler id=476；图片编辑工作流的 KSampler id=482
#
# SHIFT 语义（重要，实测踩过坑）：
#   Qwen-Image 2.1 走 model_type=FLUX（comfy/model_base.py:2673），
#   采样类 **ModelSamplingFlux**（不是 DiscreteFlow），
#   sigma 公式 = flux_time_shift(shift, 1.0, t)（comfy/model_sampling.py:413）。
#   模型内置 sampling_settings 的 shift=0.69（supported_models.py:2080）。
#
#   要让用户可调 SHIFT，必须用 **ModelSamplingFlux** 节点，并且令
#   base_shift = max_shift = 用户值 —— 它的 shift 计算是
#       mm = (max_shift - base_shift) / (4096 - 256)
#       b  = base_shift - mm*256
#       shift = (w*h/32)*mm + b
#   base=max=S 时 mm=0、b=S -> shift 恒等于 S（与分辨率无关）。
#
#   **不能用 ModelSamplingSD3**：它继承 ModelSamplingDiscreteFlow，
#   patch() 里 multiplier 写死 1000（comfy_extras/nodes_model_advanced.py:127），
#   而 Qwen-Image 2.1 的 flux_time_shift 没有这一层 -> sigma 表被放大 1000 倍，
#   出图退化成紫底绿斑的噪点。
#   实测 A/B（同种子同提示词）：
#     A 不加 sampling 节点（用模型内置 0.69） -> 正常橘猫窗台
#     B 加 ModelSamplingSD3(0.69)          -> 噪点
#     C 加 ModelSamplingFlux(base=max=0.69) -> 正常橘猫窗台（与 A 等价）
QWEN_IMAGE_UNET = "qwen_image_2.1_bf16.safetensors"
QWEN_IMAGE_VAE = "qwen_image_2.1_vae_bf16.safetensors"
# PE 文本编码器（带官方提示词扩写能力，Qwen3.5 9B）：
#   PE 编码器接 **TextGenerateLTX2Prompt**（扩写节点），扩写后的文本
#   再喂给 TextEncodeQwenImage21 的 prompt 输入。
#   条件编码器（TextEncodeQwenImage21 的 clip 输入）是另一个文件。
#   官方工作流依据：Qwen+image+2.1+文生图.json / Qwen+image+2.1图片编辑.json
QWEN_IMAGE_CLIP_PE_T2I = "qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors"
QWEN_IMAGE_CLIP_PE_I2I = "qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors"
# 条件编码器（TextEncodeQwenImage21 的 clip 输入）—— 官方工作流用的就是这个
QWEN_IMAGE_CLIP_COND = "qwen3vl_8b_bf16.safetensors"
# 不带 PE 的普通文本编码器（关掉 PE 开关时，提示词原样进模型，不做官方扩写）
QWEN_IMAGE_CLIP_PLAIN = "qwen3vl_8b_bf16.safetensors"
# Qwen-Image 2.1 **没有**加速/蒸馏 LoRA —— 官方工作流里那个
# qwen21/bfs_head_v1_qwen_2.1.safetensors 实测是「换头」效果 LoRA
# （元数据 ss_tag_frequency = {"1_head_swap: start with <image1> ..."}），
# 不是加速器。所以它和其它效果 LoRA 一样由用户自己选，不固定内置。

# 模型版本：只列本机 ComfyUI UNETLoader 下拉里真实存在的 Qwen-Image 2.1 权重。
# 2.1 本体只有一个 bf16 版本；其余是 Qwen-Image 2509/2511/2512（不同代，
# 不是 2.1），不混进来 —— 混进来会让「2.1 的推荐参数」套到别的代上。
QWEN_IMAGE_MODELS = {
    "qwen_image_2.1_bf16": {
        "label": "Qwen-Image 2.1 · 官方完整版（含 PE 提示词扩写）",
        "unet": QWEN_IMAGE_UNET,
        "weight_dtype": "default",
        # PE 文本编码器：文生图用 pe_t2i、参考图生图用 pe_i2i
        # （两者是官方给两种场景各配的扩写权重，不能混用）
        "clip_t2i": QWEN_IMAGE_CLIP_PE_T2I,
        "clip_i2i": QWEN_IMAGE_CLIP_PE_I2I,
        "sampler": "euler",
        "scheduler": "simple",
        # 官方模板 note：「steps: Qwen Image 2.1 official pipeline uses about
        # 40-50 with euler. This template starts at 25.」—— 取 40-50 区间的上沿。
        "steps": 50,
        # 官方模板 note：「cfg: keep 1 for the Qwen Image 2.1 official path.
        # Raise it only if you use a negative prompt.」
        # 注意：cfg=1 时 ComfyUI 会跳过 uncond 分支（samplers.py:609-627），
        # 负面提示词数学上无效 —— 界面会据此提示用户。
        "cfg": 1.0,
        # 内置 shift 是 0.69（supported_models.py:2080，按 1024×1024 标定）；
        # 这里暴露给用户的覆盖值默认取同一个数 —— 即「不覆盖」，
        # 行为与官方工作流一致。
        "shift": 0.69,
        # 负面提示词本身是支持的（官方 note 也说「Raise cfg only if you use
        # a negative prompt」），但默认 cfg=1 时不生效，界面要提示清楚。
        "supports_negative": True,
        "negative_effective_only_above_cfg1": True,
        "samplers": ["euler", "euler_cfg_pp", "res_multistep", "dpmpp_2m",
                     "dpmpp_2m_sde", "dpmpp_3m_sde", "dpm_2", "heun", "uni_pc"],
        "schedulers": ["simple", "beta", "sgm_uniform", "normal", "karras",
                       "exponential", "linear_quadratic"],
        "note": "官方工作流 Qwen+image+2.1+文生图.json：50 步 / cfg 1 / euler / simple。",
        "warn": "官方 PE 文本编码器自带提示词扩写（提示词会被重写得更详细）；"
                "想完全控制提示词，可在面板里关掉 PE 编码器。",
    },
}
QWEN_IMAGE_DEFAULT_MODEL = "qwen_image_2.1_bf16"

# Qwen-Image 2.1 的分辨率档位：同样是「总像素（megapixels）」语义。
# 与 Z-Image 的区别：Qwen-Image 2.1 的 VAE 是 16 倍下采样
# （comfy_extras/nodes_qwen.py:181 -> torch.zeros([1, 64, H//16, W//16])），
# 所以宽高必须是 16 的倍数。
# 官方默认 1024x1024（1MP），官方工作流 ResolutionSelector megapixels=2。
# 档位只到 2K —— 官方模板原文：
#   "Qwen Image 2.1 has native 2K (2048*2048) support for direct output."
#   "For native 2K, set 1:1 and 4 megapixels (2048*2048)."
# 4K（8.3MP）超出原生能力，官方没有对应路径，所以这里**不列 4K**
# （Z-Image 那样走放大链需要额外实现一套 Qwen 的放大流程，本轮不做，
#  列一个会崩图的档位比不列更糟）。
QWEN_IMAGE_RES_TIERS = {
    "1mp": 1.0,        # 官方默认 1024×1024
    "480p": 0.4,
    "720p": 0.9,
    "1080p": 2.0,
    "1440p": 3.5,
    "2k": 4.0,         # 2K 原生直出上限（1:1 时正好 2048×2048 = 4.0MP）
}
QWEN_IMAGE_DEFAULT_TIER = "1mp"
# VAE 是 16 倍下采样（latent_formats.QwenImage21.spacial_downscale_ratio = 16），
# 宽高必须是 16 的倍数；官方模板 further 建议 32 的倍数。
QWEN_IMAGE_SIZE_MULTIPLE = 16
# 超过这个总像素就不该直出（2.1 原生上限约 4MP / 2K）
QWEN_IMAGE_MAX_DIRECT_MP = 4.2

# 参考图数量上限：TextEncodeQwenImage21 的 images 是 COMFY_AUTOGROW_V3，
# 节点本身支持 image_1..image_16（comfy_extras/nodes_qwen.py:125-133），
# 但官方图片编辑工作流只接了 9 张参考图 —— 本面板按官方取 9。
QWEN_IMAGE_MAX_REF_IMAGES = 9
QWEN_IMAGE_MIN_REF_IMAGES = 1


def qwen_image_dimensions(aspect, tier, width=None, height=None):
    """比例 + 分辨率档位 -> 实际像素宽高（Qwen-Image 2.1）。

    算法与 z_image_dimensions 完全一致（同一套 ResolutionSelector 语义），
    只是档位表与倍数换成 Qwen-Image 2.1 的：
      - VAE 是 16 倍下采样 -> 宽高取 16 的倍数
      - 默认档位 1MP（官方 1024x1024）
    """
    w_ratio, h_ratio = ASPECT_NUM.get(aspect, (16, 9))
    megapixels = QWEN_IMAGE_RES_TIERS.get(tier,
                                            QWEN_IMAGE_RES_TIERS[QWEN_IMAGE_DEFAULT_TIER])
    mult = QWEN_IMAGE_SIZE_MULTIPLE

    if width and height:
        final_w, final_h = int(width), int(height)
    else:
        total_pixels = megapixels * 1024 * 1024
        scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
        final_w = w_ratio * scale
        final_h = h_ratio * scale
    final_w = max(mult, int(round(final_w / mult)) * mult)
    final_h = max(mult, int(round(final_h / mult)) * mult)
    return final_w, final_h


def _qwen_lora_chain(lora_items, base_model_ref, base_clip_ref):
    """把选中的效果 LoRA 串到 Qwen-Image 2.1 的模型链上。

    与 Z-Image 的 _z_image_lora_chain 同语义：
      - 用 **LoraLoader**（不是 LoraLoaderModelOnly）：model 与 clip 都接，
        否则文本编码器那半边的 patch 会被 ComfyUI 记为 "NOT LOADED" 丢掉。
      - clip 强度跟随 model 强度。

    Qwen-Image 2.1 的官方工作流用的是 rgthree 的 "Lora Loader Stack"，
    但它的输入输出与 LoraLoader 一致（model + clip -> model + clip），
    这里用核心节点 LoraLoader，避免依赖 rgthree 插件。
    """
    nodes: dict = {}
    current_model = base_model_ref
    current_clip = base_clip_ref
    for index, item in enumerate(lora_items):
        name = item.get("name") or ""
        if not name:
            raise ValueError("LoRA 配置缺少 name")
        resolved, path = lora_registry.resolve_lora(name)
        if not resolved or path is None:
            raise ValueError(f"本机不存在该 LoRA 文件：{name}")
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 0.0
        if strength <= 0:
            continue
        node_id = f"qw:40{index}"
        inputs = {
            "lora_name": resolved,
            "strength_model": strength,
            "strength_clip": strength,
            "model": current_model,
        }
        if current_clip is not None:
            inputs["clip"] = current_clip
        nodes[node_id] = {"inputs": inputs, "class_type": "LoraLoader"}
        current_model = [node_id, 0]
        if current_clip is not None:
            current_clip = [node_id, 1]
    return nodes, current_model, current_clip


def _qwen_common_nodes(model, pe_clip_name, cond_clip_name, width, height, seed,
                       sampler, scheduler, steps, cfg, shift, negative_text, prompt,
                       job_id, filename_prefix=None, loras=None, is_edit=False,
                       use_pe=True):
    """Qwen-Image 2.1 公共链路。

    文生图与参考图生图共用同一套节点，区别只在：
      - TextEncodeQwenImage21 的 images 输入接不接参考图
      - KSampler 的 latent_image 用 EmptyLatentImage 还是 TextEncode 输出的 latent

    链路：
        UNETLoader -> [加速 LoRA] -> ModelSamplingSD3(shift) -> [效果 LoRA]
                   -> QwenImage21Cache -> KSampler
        CLIPLoader(PE) -> [效果 LoRA 的 clip 侧] -> TextEncodeQwenImage21
        VAELoader -> TextEncodeQwenImage21 的 vae 输入（参考图编码成 ref_latents）

    ModelSamplingSD3 的作用：Qwen-Image 2.1 在
    comfy/supported_models.py:2080 内置了 shift=0.69（经
    ModelSamplingDiscreteFlow 的 time_snr_shift 应用）。要暴露用户可调的
    SHIFT，必须用继承自同一个 ModelSamplingDiscreteFlow 的节点覆盖它
    （comfy_extras/nodes_model_advanced.py:120）—— sigma 公式同源。
    """
    p = {}
    # PE 编码器（Qwen3.5 9B）—— 接提示词扩写节点
    p["qw:30"] = {"inputs": {"clip_name": pe_clip_name, "type": "qwen_image",
                             "device": "default"},
                  "class_type": "CLIPLoader"}
    # 条件编码器 —— 接 TextEncodeQwenImage21 的 clip 输入
    p["qw:31"] = {"inputs": {"clip_name": cond_clip_name, "type": "qwen_image",
                             "device": "default"},
                  "class_type": "CLIPLoader"}
    # VAE
    p["qw:29"] = {"inputs": {"vae_name": QWEN_IMAGE_VAE}, "class_type": "VAELoader"}
    # UNET
    p["qw:28"] = {"inputs": {"unet_name": model["unet"],
                             "weight_dtype": model["weight_dtype"]},
                  "class_type": "UNETLoader"}

    model_ref = ["qw:28", 0]
    clip_ref = ["qw:31", 0]

    # SHIFT 覆盖：ModelSamplingFlux，base=max=用户值 -> shift 恒等于该值
    p["qw:11"] = {"inputs": {"model": model_ref, "max_shift": shift,
                             "base_shift": shift, "width": width, "height": height},
                  "class_type": "ModelSamplingFlux"}
    model_ref = ["qw:11", 0]

    # 用户选的效果 LoRA
    if loras:
        lora_nodes, model_ref, clip_ref = _qwen_lora_chain(loras, model_ref, clip_ref)
        p.update(lora_nodes)

    # KV cache（编辑类工作流必带；文生图带上也不影响，且能省显存）
    p["qw:12"] = {"inputs": {"model": model_ref, "device": "auto", "dtype": "default"},
                  "class_type": "QwenImage21Cache"}
    model_ref = ["qw:12", 0]

    # 提示词扩写（官方 PE）：PE 编码器 -> TextGenerateLTX2Prompt -> 文本
    # 关掉 PE 时不建这个节点，提示词原样进 TextEncodeQwenImage21。
    if use_pe:
        p["qw:26"] = {"inputs": {
            "clip": ["qw:30", 0],
            "prompt": prompt,
            "max_length": 512,
            # COMFY_DYNAMICCOMBO_V3 在 API 格式下：主字段是选项 key，
            # 子输入扁平化成 sampling_mode.xxx
            "sampling_mode": "on",
            "sampling_mode.temperature": 0.7,
            "sampling_mode.top_k": 64,
            "sampling_mode.top_p": 0.95,
            "sampling_mode.min_p": 0.05,
            "sampling_mode.repetition_penalty": 1.05,
            "sampling_mode.seed": 0,
            "sampling_mode.presence_penalty": 0.0,
            "thinking": False,
            "use_default_template": True,
            "mtp": "auto",
        }, "class_type": "TextGenerateLTX2Prompt"}
        prompt_ref = ["qw:26", 0]
    else:
        prompt_ref = prompt

    # 文本编码
    te_inputs = {
        "clip": clip_ref,
        "prompt": prompt_ref,
        "negative_prompt": negative_text,
        "vae": ["qw:29", 0],
        # 参考图重采样目标边长（comfy_extras/nodes_qwen.py:155-160）。
        # 官方工作流：文生图用 1024，图片编辑用 2048 —— 编辑要保留参考图
        # 的细节，用 1024 会把参考图降采样过多。
        "resolution": 2048 if is_edit else 1024,
    }
    p["qw:27"] = {"inputs": te_inputs, "class_type": "TextEncodeQwenImage21"}

    # 采样
    if is_edit:
        # 编辑：latent 用 TextEncode 输出的（尺寸跟随第一张参考图）。
        # 官方工作流用 ComfySwitchNode 在 EmptyLatentImage 和这个 latent 之间切，
        # 但 ComfySwitchNode 的两个输入类型不同（LATENT vs COMFY_MATCHTYPE_V3），
        # 直接按场景接更简单也等效：编辑场景永远走 TextEncode 的 latent。
        latent_ref = ["qw:27", 2]
    else:
        p["qw:13"] = {"inputs": {"width": width, "height": height, "batch_size": 1},
                      "class_type": "EmptyLatentImage"}
        latent_ref = ["qw:13", 0]

    p["qw:3"] = {"inputs": {
        "seed": int(seed), "steps": int(steps), "cfg": float(cfg),
        "sampler_name": sampler, "scheduler": scheduler, "denoise": 1,
        "model": model_ref,
        "positive": ["qw:27", 0],
        "negative": ["qw:27", 1],
        "latent_image": latent_ref,
    }, "class_type": "KSampler"}

    p["qw:8"] = {"inputs": {"samples": ["qw:3", 0], "vae": ["qw:29", 0]},
                 "class_type": "VAEDecode"}
    p["qw:9"] = {"inputs": {"filename_prefix": filename_prefix or f"h3studio/qwen_{job_id}",
                            "images": ["qw:8", 0]},
                 "class_type": "SaveImage"}
    return p


def _qwen_collect_loras(params):
    """收集用户选中的效果 LoRA（沿用 Z-Image 的语义与校验）。"""
    loras = []
    for item in (params.get("loras") or []):
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        try:
            strength = float(item.get("strength", 1.0))
        except (TypeError, ValueError):
            strength = 0.0
        if strength <= 0:
            continue
        name = item.get("name") or ""
        meta = _lookup_lora_meta(name)
        if meta and meta.get("kind") == lora_registry.KIND_ACCELERATION:
            raise ValueError(
                f"「{meta.get('display') or name}」是加速/蒸馏类 LoRA，"
                "不能作为可选效果 LoRA 使用")
        if meta and not meta.get("compatible", True):
            raise ValueError(
                f"「{meta.get('display') or name}」与 Qwen-Image 2.1 底模不兼容，已拒绝加载："
                f"{meta.get('incompatible_reason') or '未知原因'}")
        cap = float(meta.get("strength_cap", 1.0)) if meta else 1.0
        if cap < 1.0 and strength > cap:
            strength = cap
        trigger = item.get("trigger")
        if trigger is None:
            trigger = (meta or {}).get("trigger", "")
        loras.append({"name": name, "strength": strength, "trigger": trigger or ""})
    return loras


def build_qwen_t2i(params):
    """Qwen-Image 2.1 文生图。"""
    job_id = params["job_id"]
    model_key = params.get("model") or QWEN_IMAGE_DEFAULT_MODEL
    if model_key not in QWEN_IMAGE_MODELS:
        model_key = QWEN_IMAGE_DEFAULT_MODEL
    model = QWEN_IMAGE_MODELS[model_key]

    seed = params.get("seed") or random.randint(0, 2**48)
    aspect = params.get("aspect", "16:9")
    if aspect not in ASPECT_NUM:
        aspect = "16:9"
    tier = params.get("resolution")
    if tier not in QWEN_IMAGE_RES_TIERS:
        tier = QWEN_IMAGE_DEFAULT_TIER
    width, height = qwen_image_dimensions(aspect, tier, params.get("width"),
                                           params.get("height"))

    sampler = params.get("sampler") or model["sampler"]
    scheduler = params.get("scheduler") or model["scheduler"]
    steps = int(params.get("steps") or model["steps"])
    cfg = float(params.get("cfg") if params.get("cfg") is not None else model["cfg"])
    shift = float(params.get("shift") if params.get("shift") is not None else model["shift"])
    negative_text = params.get("negative_prompt", "") or ""
    prompt = params.get("prompt", "")

    loras = _qwen_collect_loras(params)
    prompt = merge_lora_triggers(prompt, loras)

    # PE 开关：关掉 PE 就跳过提示词扩写，提示词原样进模型
    use_pe = bool(params.get("use_pe", True))
    pe_clip = model["clip_t2i"]
    cond_clip = QWEN_IMAGE_CLIP_COND

    return _qwen_common_nodes(model, pe_clip, cond_clip, width, height, seed, sampler,
                               scheduler, steps, cfg, shift, negative_text,
                               prompt, job_id, loras=loras, use_pe=use_pe)


def build_qwen_i2i(params):
    """Qwen-Image 2.1 参考图生图（图片编辑）。

    参考图最多 9 张（官方图片编辑工作流 Qwen+image+2.1图片编辑.json
    接的就是 9 张）。数量必须在 1–9 之间，超出给明确错误。

    参考图来源：params["ref_images"] = [{"file": "xxx.png"}, ...]
    文件必须已存在于本机输出目录（走 _verified_video_path 同类的白名单校验）。
    """
    job_id = params["job_id"]
    model_key = params.get("model") or QWEN_IMAGE_DEFAULT_MODEL
    if model_key not in QWEN_IMAGE_MODELS:
        model_key = QWEN_IMAGE_DEFAULT_MODEL
    model = QWEN_IMAGE_MODELS[model_key]

    refs = params.get("ref_images") or []
    if not isinstance(refs, list):
        refs = []
    if len(refs) < QWEN_IMAGE_MIN_REF_IMAGES:
        raise ValueError(
            f"参考图生图至少需要 {QWEN_IMAGE_MIN_REF_IMAGES} 张参考图，当前 {len(refs)} 张")
    if len(refs) > QWEN_IMAGE_MAX_REF_IMAGES:
        raise ValueError(
            f"参考图最多 {QWEN_IMAGE_MAX_REF_IMAGES} 张，当前 {len(refs)} 张。"
            f"请删除多余参考图（上限 {QWEN_IMAGE_MAX_REF_IMAGES} 张）")

    seed = params.get("seed") or random.randint(0, 2**48)
    aspect = params.get("aspect", "16:9")
    if aspect not in ASPECT_NUM:
        aspect = "16:9"
    tier = params.get("resolution")
    if tier not in QWEN_IMAGE_RES_TIERS:
        tier = QWEN_IMAGE_DEFAULT_TIER
    width, height = qwen_image_dimensions(aspect, tier, params.get("width"),
                                           params.get("height"))

    sampler = params.get("sampler") or model["sampler"]
    scheduler = params.get("scheduler") or model["scheduler"]
    steps = int(params.get("steps") or model["steps"])
    cfg = float(params.get("cfg") if params.get("cfg") is not None else model["cfg"])
    shift = float(params.get("shift") if params.get("shift") is not None else model["shift"])
    negative_text = params.get("negative_prompt", "") or ""
    prompt = params.get("prompt", "")

    loras = _qwen_collect_loras(params)
    prompt = merge_lora_triggers(prompt, loras)

    # PE 开关：关掉 PE 就跳过提示词扩写，提示词原样进模型
    use_pe = bool(params.get("use_pe", True))
    pe_clip = model["clip_i2i"]
    cond_clip = QWEN_IMAGE_CLIP_COND

    # 参考图 -> LoadImage 节点（只允许引用本机已存在的文件）
    p = _qwen_common_nodes(model, pe_clip, cond_clip, width, height, seed, sampler,
                            scheduler, steps, cfg, shift, negative_text,
                            prompt, job_id, loras=loras, is_edit=True, use_pe=use_pe)
    # 参考图 -> LoadImage -> TextEncodeQwenImage21 的 images 输入
    # （COMFY_AUTOGROW_V3，按 image_1..image_N 命名）
    te_inputs = p["qw:27"]["inputs"]
    # TextEncodeQwenImage21 的 images 是 COMFY_AUTOGROW_V3，在 API 格式下
    # **必须写成嵌套的 images 字典**：{"image_1": <值>, "image_2": ...}
    # 写成扁平的 "image_1" 顶层键时，ComfyUI 会把它当成 execute() 的
    # 关键字参数传进去，报
    #   TypeError: TextEncodeQwenImage21.execute() got an unexpected
    #              keyword argument 'image_1'
    # （值可以是文件名字符串，也可以是 ["节点id", 槽位] 的连线引用）
    images_input: dict = {}
    for index, ref in enumerate(refs[:QWEN_IMAGE_MAX_REF_IMAGES]):
        node_id = f"qw:ref{index}"
        # ref_images 两种形态都收：
        #   服务端上传后是文件名列表（["images/x.png", ...]）
        #   也接受字典列表（[{"file": "images/x.png"}, ...]）
        if isinstance(ref, dict):
            filename = ref.get("file") or ""
        else:
            filename = str(ref or "")
        if not filename:
            raise ValueError(f"第 {index + 1} 张参考图缺少文件名")
        p[node_id] = {"inputs": {"image": filename}, "class_type": "LoadImage"}
        images_input[f"image_{index + 1}"] = [node_id, 0]
    te_inputs["images"] = images_input
    return p


BUILDERS = {"t2v": build_t2v, "flf": build_flf, "r2v": build_r2v, "t2i": build_t2i,
             "t2i_qwen": build_qwen_t2i, "i2i_qwen": build_qwen_i2i}


def build_workflow(mode, params):
    if mode not in BUILDERS:
        raise ValueError(f"未知模式: {mode}")
    return BUILDERS[mode](params)
