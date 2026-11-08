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
import random

from . import loras as lora_registry

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

    p["136"] = {
        "inputs": {
            "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
            "prompt": params.get("prompt", ""),
            "width": ["115", 0], "height": ["115", 1],
            "ref_image_size": "match",
            **{f"ref_images.{k}": v for k, v in ref_inputs.items()},
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


def _z_image_lora_chain(lora_items, base_model_ref):
    """把选中的 LoRA 串成模型链，返回 (节点字典, 最终模型引用)。

    链路：UNETLoader -> ModelSamplingAuraFlow -> LoraLoaderModelOnly x N

    LoraLoaderModelOnly 只作用在 model 上（不动 clip），这正是 Z-Image
    需要的 —— Z-Image 的文本编码走独立的 CLIPLoader(lumina2)，
    风格 LoRA 应当只改 UNet 侧。

    找不到文件时抛错（不静默忽略）。
    """
    nodes: dict = {}
    current = base_model_ref
    for index, item in enumerate(lora_items):
        name = item.get("name") or ""
        if not name:
            raise ValueError("LoRA 配置缺少 name")
        resolved, path = lora_registry.resolve_lora(name)
        if not resolved or path is None:
            raise ValueError(f"本机不存在该 LoRA 文件：{name}")
        strength = float(item.get("strength", 1.0))
        if strength <= 0:
            # 强度 0 = 关闭该 LoRA，但仍然校验文件存在，避免静默吞掉错名
            continue
        node_id = f"57:40{index}"
        nodes[node_id] = {
            "inputs": {"lora_name": resolved, "strength_model": strength,
                       "model": current},
            "class_type": "LoraLoaderModelOnly",
        }
        current = [node_id, 0]
    return nodes, current


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
    if loras:
        lora_nodes, model_ref = _z_image_lora_chain(loras, ["57:11", 0])
        p.update(lora_nodes)
        p["57:3"]["inputs"]["model"] = model_ref
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
    return {
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
    if loras:
        lora_nodes, model_ref = _z_image_lora_chain(loras, ["57:11", 0])
        p.update(lora_nodes)
        p["57:3"]["inputs"]["model"] = model_ref
        p["57:75"]["inputs"]["model"] = model_ref


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

    # LoRA：[{name, strength}]，只接受启用且强度 > 0 的项
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
        loras.append({"name": item.get("name") or "", "strength": strength})

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


BUILDERS = {"t2v": build_t2v, "flf": build_flf, "r2v": build_r2v, "t2i": build_t2i}


def build_workflow(mode, params):
    if mode not in BUILDERS:
        raise ValueError(f"未知模式: {mode}")
    return BUILDERS[mode](params)
