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

# ------------------------------------------------------------------
# 模型清单（与 d:\cfup\ComfyUI\models 实际文件一致）
# ------------------------------------------------------------------
UNET = {
    "fl2va": {"int8": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
              "nvfp4": "MiniMax_H3_FL2VA_pruned_nvfp4.safetensors"},
    "ref2va": {"int8": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
               "nvfp4": "MiniMax_H3_Ref2VA_pruned_nvfp4.safetensors"},
}
CLIP = {"int8": "qwen3vl_32b_minimax_h3_int4_convrot.safetensors",
        "nvfp4": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"}
VAE_VIDEO = "minimax_h3_video_vae_fp16.safetensors"
VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"

LORAS = {
    "ckpt850": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
    # 注意：源文件名末尾带空格，原样保留
    "v4step600": "minimax_h3_turbo_v4_step600_ema .safetensors",
}

Z_IMAGE_UNET = "z_image_turbo_bf16.safetensors"
Z_IMAGE_CLIP = "qwen_3_4b.safetensors"
Z_IMAGE_VAE = "ae.safetensors"

ASPECT_MAP = {
    "16:9": "16:9 (Widescreen)",
    "9:16": "9:16 (Portrait Widescreen)",
    "4:3": "4:3 (Standard)",
    "3:4": "3:4 (Portrait Standard)",
    "1:1": "1:1 (Square)",
}
ASPECT_NUM = {"16:9": (16, 9), "9:16": (9, 16), "4:3": (4, 3),
              "3:4": (3, 4), "1:1": (1, 1)}

# 分辨率档位 -> megapixels（ResolutionSelector 用）
RES_MP = {"544p": 0.4, "768p": 0.8}


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
            img_node_inputs["first_frame"] = [image_inputs["first_frame"], 0]
        if image_inputs.get("last_frame"):
            img_node_inputs["last_frame"] = [image_inputs["last_frame"], 0]

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

    ref_inputs = {"ref_image_0": params["ref_images"][0]}
    if len(params["ref_images"]) > 1:
        ref_inputs["ref_image_1"] = params["ref_images"][1]
    if len(params["ref_images"]) > 2:
        ref_inputs["ref_image_2"] = params["ref_images"][2]

    p["136"] = {
        "inputs": {
            "clip": ["128", 0], "vae": ["119", 0], "audio_vae": ["120", 0],
            "prompt": params.get("prompt", ""),
            "width": ["115", 0], "height": ["115", 1],
            "ref_image_size": "match",
            **{f"ref_images.{k}": [v, 0] for k, v in ref_inputs.items()},
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


def build_t2i(params):
    """Z-IMAGE-turbo 文生图"""
    job_id = params["job_id"]
    seed = params.get("seed") or random.randint(0, 2**48)
    aspect = params.get("aspect", "16:9")
    w_ratio, h_ratio = ASPECT_NUM.get(aspect, (16, 9))
    mp = {"544p": 1.0, "768p": 2.0}.get(params.get("resolution", "768p"), 2.0)
    total = mp * 1024 * 1024
    scale = math.sqrt(total / (w_ratio * h_ratio))
    width = round(w_ratio * scale / 16) * 16
    height = round(h_ratio * scale / 16) * 16

    return {
        "9": {"inputs": {"filename_prefix": f"h3studio/t2i_{job_id}", "images": ["57:8", 0]},
              "class_type": "SaveImage"},
        "57:30": {"inputs": {"clip_name": Z_IMAGE_CLIP, "type": "lumina2", "device": "default"},
                  "class_type": "CLIPLoader"},
        "57:29": {"inputs": {"vae_name": Z_IMAGE_VAE}, "class_type": "VAELoader"},
        "57:33": {"inputs": {"conditioning": ["57:27", 0]}, "class_type": "ConditioningZeroOut"},
        "57:8": {"inputs": {"samples": ["57:3", 0], "vae": ["57:29", 0]}, "class_type": "VAEDecode"},
        "57:28": {"inputs": {"unet_name": Z_IMAGE_UNET, "weight_dtype": "default"},
                  "class_type": "UNETLoader"},
        "57:27": {"inputs": {"text": params.get("prompt", ""), "clip": ["57:30", 0]},
                  "class_type": "CLIPTextEncode"},
        "57:13": {"inputs": {"width": width, "height": height, "batch_size": 1},
                  "class_type": "EmptySD3LatentImage"},
        "57:11": {"inputs": {"shift": 3, "model": ["57:28", 0]}, "class_type": "ModelSamplingAuraFlow"},
        "57:3": {"inputs": {"seed": int(seed), "steps": 8, "cfg": 1, "sampler_name": "res_multistep",
                            "scheduler": "simple", "denoise": 1, "model": ["57:11", 0],
                            "positive": ["57:27", 0], "negative": ["57:33", 0],
                            "latent_image": ["57:13", 0]},
                 "class_type": "KSampler"},
    }


BUILDERS = {"t2v": build_t2v, "flf": build_flf, "r2v": build_r2v, "t2i": build_t2i}


def build_workflow(mode, params):
    if mode not in BUILDERS:
        raise ValueError(f"未知模式: {mode}")
    return BUILDERS[mode](params)
