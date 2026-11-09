# -*- coding: utf-8 -*-
"""
YuE2 音乐生成（文生音乐 / 参考生音乐）的工作流构造与参数表。

【为什么走 ComfyUI 原生节点，而不是 custom_nodes/ComfyUI-YuE2】
  第三方 `ComfyUI-YuE2` 的 `YuE2Loader` 节点内部会调用
  `torch.cuda.set_per_process_memory_fraction(...)`（yue2/pipeline.py:159）。
  这是**进程级**设置 —— 一旦跑过一次音乐任务，整个 ComfyUI 进程的显存上限
  被永久压到 (memory_budget_gib-2)/total，之后跑 H3 视频（需要 30GB+）就会 OOM。
  ComfyUI 0.37 **自带** YuE2 支持（comfy/supported_models.py:2318 的 `YuE2` 类、
  comfy_extras/nodes_yue2.py 的 `YuE2GenerateABC` / `YuE2GenerateMusic` /
  `EmptyYuE2LatentAudio`、comfy_extras/nodes_audio_encoder.py 的
  `SheetSage2AudioToABC`），走原生节点没有这个副作用。

【工作流骨架（对齐官方模板 audio_yue2_text2music.json / audio_yue2_music_cover.json）】
  文生音乐：
    CheckpointLoaderSimple -> MODEL / CLIP / VAE
      ├─ CLIP -> YuE2GenerateABC(style, lyrics, seed, mode) -> abc
      └─ CLIP -> YuE2GenerateMusic(style, lyrics, abc, seed, mode, max_duration)
                 ├─ CONDITIONING -> KSampler.positive
                 └─ seconds -> EmptyYuE2LatentAudio.seconds -> KSampler.latent_image
    CONDITIONING -> ConditioningZeroOut -> KSampler.negative
    KSampler -> VAEDecodeAudio(vae) -> AUDIO -> SaveAudioAdvanced

  参考生音乐（cover）：
    LoadAudio / VHS_LoadAudio -> SheetSage2AudioToABC(audio_encoder, audio, mode)
      -> abc -> YuE2GenerateMusic.abc（mode 固定 melody）
    其余同上。

【关键参数来源】
  * 官方模板 KSampler：steps=32 / cfg=1 / dpm_2 / sgm_uniform
  * 官方模板 YuE2GenerateMusic：temperature=1 / top_p=0.95 / top_k=100 /
    repetition_penalty=1.2
  * 官方模板 YuE2GenerateABC：temperature=0.7 / top_p=0.9 / top_k=30 /
    repetition_penalty=1.005 / penalty_window=100 / max_abc_tokens=8192
  * 官方模板 SeedNode：seed 默认 0
"""
from __future__ import annotations

import math
import random
from pathlib import Path

# ------------------------------------------------------------------
# 模型清单（只列本机 ComfyUI 实测存在的权重文件）
# ------------------------------------------------------------------
#: YuE2 主模型（ComfyUI 原生 CheckpointLoaderSimple 读 checkpoints/）
YUE2_CHECKPOINTS = {
    "yue2_3b_int8_convrot": {
        "label": "YuE2 3B · INT8 ConvRot（官方量化版）",
        "ckpt": "yue2_3b_int8_convrot.safetensors",
        "note": "Comfy-Org/YuE2 官方 INT8 ConvRot 量化版（官方模板 audio_yue2_text2music 默认用这个）。"
                "3B 参数的音乐生成模型：吃风格描述 + 歌词，先规划 ABC 乐谱再生成带人声与伴奏的音频。"
                "INT8 量化版显存占用与耗时都明显低于 BF16 完整版，是本机唯一可用的版本。",
        "warn": "",
    },
    "yue2_3b_bf16": {
        "label": "YuE2 3B · BF16 完整版",
        "ckpt": "yue2_3b_bf16.safetensors",
        "note": "Comfy-Org/YuE2 官方 BF16 原精度版，音质略好但显存与耗时明显更高。"
                "本机未下载该权重文件，故不可用。",
        "warn": "",
    },
}
YUE2_DEFAULT_CHECKPOINT = "yue2_3b_int8_convrot"

#: 参考生音乐用的音频编码器（SheetSage2，读 audio_encoders/）
YUE2_AUDIO_ENCODERS = {
    "sheetsage2_bf16": {
        "label": "SheetSage2 · BF16（官方音频转谱）",
        "encoder": "sheetsage2_bf16.safetensors",
        "note": "Comfy-Org/YuE2 官方音频编码器 SheetSage2（BF16）。参考生音乐时，"
                "它把参考音频转成 ABC 乐谱 —— 只提取旋律线，不含和弦符号，"
                "原曲的编曲与和声由 YuE2 依据风格描述重新生成。",
    },
}
YUE2_DEFAULT_AUDIO_ENCODER = "sheetsage2_bf16"

#: 模型目录（ComfyUI 的 checkpoints / audio_encoders）
YUE2_MODEL_ROOTS = [
    Path("/home/ubuntu/comfyui/models"),
    Path("/mnt/nvme1t_colorful/comfyui-models"),
    Path("/mnt/usb2t_toshiba/comfyui-models"),
]

#: 官方模板 audio_yue2_text2music.json 的采样参数
YUE2_SAMPLER = "dpm_2"
YUE2_SCHEDULER = "sgm_uniform"
YUE2_STEPS = 32
YUE2_CFG = 1.0

#: 官方模板 YuE2GenerateMusic 的采样参数
YUE2_MUSIC_SAMPLING = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 100,
    "repetition_penalty": 1.2,
}

#: 官方模板 YuE2GenerateABC 的采样参数
YUE2_ABC_SAMPLING = {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 30,
    "repetition_penalty": 1.005,
    "penalty_window": 100,
    "max_abc_tokens": 8192,
}

#: 采样器 / 调度器可选范围（KSampler 的合法取值）
YUE2_SAMPLERS = ["dpm_2", "euler", "res_multistep", "dpmpp_2m", "dpmpp_2m_sde",
                 "dpmpp_3m_sde", "ddim", "heun", "ddpm_uniform"]
YUE2_SCHEDULERS = ["sgm_uniform", "simple", "beta", "normal", "karras",
                    "exponential", "linear_quadratic", "ddim_uniform"]

#: 谱面规划模式（ABC planning）
#   full   = 先出「旋律 + 和弦」完整谱面（文生音乐推荐）
#   melody = 只出旋律（参考生音乐推荐 —— 保留原曲旋律线）
YUE2_MODES = ["full", "melody"]

#: 时长上限（秒）。官方模板给 360（6 分钟）；节点 schema 上限 900。
YUE2_MAX_DURATION = 360.0
YUE2_MIN_DURATION = 4.0
YUE2_DEFAULT_DURATION = 60.0

#: 音频输出格式
YUE2_AUDIO_FORMATS = ["flac", "mp3", "opus"]
YUE2_DEFAULT_FORMAT = "flac"


def _find_model(sub_dir: str, filename: str) -> Path | None:
    """在 ComfyUI 的模型目录里找权重文件（含 extra_model_paths 扩展的 6 处）。"""
    for root in YUE2_MODEL_ROOTS:
        candidate = root / sub_dir / filename
        if candidate.is_file():
            return candidate
    return None


def checkpoint_available(key: str) -> bool:
    entry = YUE2_CHECKPOINTS.get(key)
    if not entry:
        return False
    return _find_model("checkpoints", entry["ckpt"]) is not None


def audio_encoder_available(key: str) -> bool:
    entry = YUE2_AUDIO_ENCODERS.get(key)
    if not entry:
        return False
    return _find_model("audio_encoders", entry["encoder"]) is not None


def model_options() -> dict:
    """给前端的模型清单（含本机可用性）。"""
    checkpoints = {}
    for key, m in YUE2_CHECKPOINTS.items():
        checkpoints[key] = {
            "label": m["label"],
            "ckpt": m["ckpt"],
            "note": m["note"],
            "warn": m["warn"],
            "available": checkpoint_available(key),
        }
    encoders = {}
    for key, m in YUE2_AUDIO_ENCODERS.items():
        encoders[key] = {
            "label": m["label"],
            "encoder": m["encoder"],
            "note": m["note"],
            "available": audio_encoder_available(key),
        }
    return {
        "checkpoints": checkpoints,
        "audio_encoders": encoders,
        "default_checkpoint": YUE2_DEFAULT_CHECKPOINT,
        "default_audio_encoder": YUE2_DEFAULT_AUDIO_ENCODER,
        "sampler": YUE2_SAMPLER,
        "scheduler": YUE2_SCHEDULER,
        "steps": YUE2_STEPS,
        "cfg": YUE2_CFG,
        "samplers": YUE2_SAMPLERS,
        "schedulers": YUE2_SCHEDULERS,
        "modes": YUE2_MODES,
        "music_sampling": dict(YUE2_MUSIC_SAMPLING),
        "abc_sampling": dict(YUE2_ABC_SAMPLING),
        "max_duration": YUE2_MAX_DURATION,
        "min_duration": YUE2_MIN_DURATION,
        "default_duration": YUE2_DEFAULT_DURATION,
        "formats": YUE2_AUDIO_FORMATS,
        "default_format": YUE2_DEFAULT_FORMAT,
    }


def _clamp_duration(value) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return YUE2_DEFAULT_DURATION
    return max(YUE2_MIN_DURATION, min(YUE2_MAX_DURATION, seconds))


def _resolve_checkpoint(params: dict) -> str:
    key = params.get("checkpoint") or YUE2_DEFAULT_CHECKPOINT
    entry = YUE2_CHECKPOINTS.get(key)
    if not entry:
        key = YUE2_DEFAULT_CHECKPOINT
        entry = YUE2_CHECKPOINTS[key]
    if not checkpoint_available(key):
        raise ValueError(
            f"本机缺少 YuE2 模型权重：{entry['ckpt']}。"
            f"请下载到 ComfyUI 的 models/checkpoints/ 后再使用")
    return entry["ckpt"]


def _resolve_audio_encoder(params: dict) -> str:
    key = params.get("audio_encoder") or YUE2_DEFAULT_AUDIO_ENCODER
    entry = YUE2_AUDIO_ENCODERS.get(key)
    if not entry:
        key = YUE2_DEFAULT_AUDIO_ENCODER
        entry = YUE2_AUDIO_ENCODERS[key]
    if not audio_encoder_available(key):
        raise ValueError(
            f"本机缺少音频编码器权重：{entry['encoder']}。"
            f"请下载到 ComfyUI 的 models/audio_encoders/ 后再使用")
    return entry["encoder"]


def _sampling(params: dict, prefix: str, defaults: dict) -> dict:
    """取一组采样参数（params 覆盖默认值）。"""
    out = dict(defaults)
    for name in defaults:
        value = params.get(f"{prefix}_{name}")
        if value is None:
            continue
        try:
            if isinstance(defaults[name], int) and not isinstance(defaults[name], bool):
                out[name] = int(value)
            else:
                out[name] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _seed(params: dict) -> int:
    seed = params.get("seed")
    try:
        if seed is None or str(seed).strip() == "":
            return random.randint(0, 2**31)
        return int(seed)
    except (TypeError, ValueError):
        return random.randint(0, 2**31)


def build_music_workflow(params: dict) -> dict:
    """
    组装 YuE2 音乐工作流（API 格式）。

    params 字段（全部可选，有合理默认值）：
      prompt        风格描述（style）—— 必填，由调用方保证非空
      lyrics        歌词（可选，空 = 纯器乐）
      mode          谱面规划：full / melody（默认 full）
      checkpoint    主模型 key（默认 yue2_3b_int8_convrot）
      audio_encoder 参考生音乐的音频编码器 key
      ref_audio     参考音频文件名（ComfyUI input 里的 'subfolder/name'）
      max_duration  最长时长（秒），默认 60
      seed          随机种子
      steps / cfg / sampler / scheduler
      format        输出音频格式 flac / mp3 / opus
      music_temperature / music_top_p / music_top_k / music_repetition_penalty
      abc_temperature / abc_top_p / abc_top_k / abc_repetition_penalty
      abc_max_tokens / abc_penalty_window
    """
    ckpt_name = _resolve_checkpoint(params)
    style = (params.get("prompt") or "").strip()
    if not style:
        raise ValueError("音乐风格描述不能为空")
    lyrics = params.get("lyrics") or ""
    if isinstance(lyrics, str):
        lyrics = lyrics.strip()

    mode = params.get("mode") or "full"
    if mode not in YUE2_MODES:
        mode = "full"

    # 参考生音乐：谱面来自参考音频（SheetSage2 转谱），不是 YuE2 自己规划的。
    # SheetSage2AudioToABC 固定输出「仅旋律」谱面（不含和弦符号），
    # 所以 YuE2GenerateMusic 的 mode 也必须是 melody —— 否则它的指令前缀
    # 会声称「带和弦的完整谱面」，而实际喂进去的 ABC 里没有和弦，
    # 模型与谱面互相矛盾。
    # 【实测，2026-11-09】官方 cover 模板 audio_yue2_music_cover.json 里
    # SheetSage2AudioToABC 与 YuE2GenerateMusic 两处的 mode 都是 "melody"。
    ref_audio_param = (params.get("ref_audio") or "").strip()
    if ref_audio_param and mode != "melody":
        mode = "melody"

    seed = _seed(params)
    max_duration = _clamp_duration(params.get("max_duration", YUE2_DEFAULT_DURATION))
    sampler = params.get("sampler") or YUE2_SAMPLER
    if sampler not in YUE2_SAMPLERS:
        sampler = YUE2_SAMPLER
    scheduler = params.get("scheduler") or YUE2_SCHEDULER
    if scheduler not in YUE2_SCHEDULERS:
        scheduler = YUE2_SCHEDULER
    try:
        steps = int(params.get("steps") or YUE2_STEPS)
    except (TypeError, ValueError):
        steps = YUE2_STEPS
    steps = max(1, min(200, steps))
    try:
        cfg = float(params.get("cfg") if params.get("cfg") is not None else YUE2_CFG)
    except (TypeError, ValueError):
        cfg = YUE2_CFG
    cfg = max(0.0, min(20.0, cfg))

    audio_format = params.get("format") or YUE2_DEFAULT_FORMAT
    if audio_format not in YUE2_AUDIO_FORMATS:
        audio_format = YUE2_DEFAULT_FORMAT

    music_sampling = _sampling(params, "music", YUE2_MUSIC_SAMPLING)
    abc_sampling = _sampling(params, "abc", YUE2_ABC_SAMPLING)

    job_id = params.get("job_id") or "yue2"
    filename_prefix = params.get("filename_prefix") or f"h3studio/yue2_{job_id}"

    p: dict = {}

    # -- 模型 --------------------------------------------------------
    p["15"] = {
        "inputs": {"ckpt_name": ckpt_name},
        "class_type": "CheckpointLoaderSimple",
        "_meta": {"title": "加载 YuE2"},
    }

    # -- 谱面规划（仅文生音乐需要；参考生音乐的 ABC 来自参考音频） --
    ref_audio = (params.get("ref_audio") or "").strip()
    abc_source = None
    if ref_audio:
        # 参考生音乐：音频 -> LoadAudio -> SheetSage2 -> ABC
        #
        # 【关键，2026-11-09 真机踩坑，两处】
        # 1) SheetSage2AudioToABC 的 `audio` 输入类型是 **AUDIO**（对象），
        #    不是文件名字符串。直接塞 "h3studio/xxx.wav" 会报
        #    "'str' object has no attribute 'get'"（节点 1 就挂）。
        #    必须先用 LoadAudio 把文件读成 AUDIO 对象再接进去。
        # 2) 节点选 `LoadAudio` 而不是 `VHS_LoadAudio`：
        #    * LoadAudio.audio 是 COMBO，值必须命中 `input/` **顶层**清单
        #      -> 所以服务端把参考音频上传到 input 顶层（见 server.py 的
        #         comfy_upload_audio_flat），传回来的就是纯文件名。
        #    * VHS_LoadAudio.audio_file 要的是**绝对文件系统路径**
        #      （validate_path 用 os.path.isfile 判），传相对路径会报
        #      "Invalid file path"。
        encoder_name = _resolve_audio_encoder(params)
        p["43"] = {
            "inputs": {"audio_encoder_name": encoder_name},
            "class_type": "AudioEncoderLoader",
            "_meta": {"title": "加载音频编码器"},
        }
        p["45"] = {
            "inputs": {"audio": ref_audio},
            "class_type": "LoadAudio",
            "_meta": {"title": "加载参考音频"},
        }
        p["41"] = {
            "inputs": {"audio_encoder": ["43", 0], "audio": ["45", 0],
                       "mode": "melody"},
            "class_type": "SheetSage2AudioToABC",
            "_meta": {"title": "参考音频转 ABC 谱"},
        }
        abc_source = ["41", 0]
    else:
        # 文生音乐：YuE2 自己规划 ABC
        p["24"] = {
            "inputs": {
                "clip": ["15", 1],
                "style": style,
                "lyrics": lyrics,
                "seed": seed,
                "mode": mode,
                "max_abc_tokens": abc_sampling["max_abc_tokens"],
                "temperature": abc_sampling["temperature"],
                "top_p": abc_sampling["top_p"],
                "top_k": abc_sampling["top_k"],
                "repetition_penalty": abc_sampling["repetition_penalty"],
                "penalty_window": abc_sampling["penalty_window"],
            },
            "class_type": "YuE2GenerateABC",
            "_meta": {"title": "生成 ABC 谱面"},
        }
        abc_source = ["24", 0]

    # -- 条件 + 采样 --------------------------------------------------
    music_inputs = {
        "clip": ["15", 1],
        "style": style,
        "lyrics": lyrics,
        "abc": abc_source,
        "seed": seed,
        "mode": mode,
        "max_duration": max_duration,
        "temperature": music_sampling["temperature"],
        "top_p": music_sampling["top_p"],
        "top_k": music_sampling["top_k"],
        "repetition_penalty": music_sampling["repetition_penalty"],
    }
    if params.get("cfg_scale") is not None:
        try:
            music_inputs["cfg_scale"] = float(params["cfg_scale"])
        except (TypeError, ValueError):
            pass
    p["25"] = {
        "inputs": music_inputs,
        "class_type": "YuE2GenerateMusic",
        "_meta": {"title": "生成音乐"},
    }

    # 负分支：官方模板用 ConditioningZeroOut（CFG 恒为 1 时不需要负条件）
    p["18"] = {
        "inputs": {"conditioning": ["25", 0]},
        "class_type": "ConditioningZeroOut",
        "_meta": {"title": "负条件"},
    }

    # latent 长度 = 模型实际输出的秒数（不是我们请求的上限）
    p["5"] = {
        "inputs": {"seconds": ["25", 1], "batch_size": 1},
        "class_type": "EmptyYuE2LatentAudio",
        "_meta": {"title": "空音频 latent"},
    }

    p["8"] = {
        "inputs": {
            "model": ["15", 0],
            "positive": ["25", 0],
            "negative": ["18", 0],
            "latent_image": ["5", 0],
            "seed": seed,
            "steps": steps,
            "cfg": cfg,
            "sampler_name": sampler,
            "scheduler": scheduler,
            "denoise": 1,
        },
        "class_type": "KSampler",
        "_meta": {"title": "采样"},
    }

    p["9"] = {
        "inputs": {"samples": ["8", 0], "vae": ["15", 2]},
        "class_type": "VAEDecodeAudio",
        "_meta": {"title": "解码音频"},
    }

    # -- 输出 --------------------------------------------------------
    # SaveAudioAdvanced 的 format 是 COMFY_DYNAMICCOMBO_V3。
    # 【实测，2026-11-09】API 格式里必须写**扁平字符串** "format": "flac"，
    # 不能写嵌套 dict {"format": "flac"} —— 后者会让 execute() 收不到
    # format 参数，报 "SaveAudioAdvanced.execute() missing 1 required
    # positional argument: 'format'"。
    # mp3 / opus 的附加参数走**顶层点号键** "format.quality"。
    # 三种写法实测结论（probe_dyncombo.py）：
    #   A 嵌套 dict      -> ERROR（missing format）
    #   B 扁平字符串      -> OK（fmt_b_00001.flac）
    #   C 扁平 + 点号quality -> OK（fmt_c_00001.mp3）
    save_inputs = {
        "audio": ["9", 0],
        "filename_prefix": filename_prefix,
        "format": audio_format,
    }
    if audio_format == "mp3":
        save_inputs["format.quality"] = params.get("audio_quality") or "V0"
    elif audio_format == "opus":
        save_inputs["format.quality"] = params.get("audio_quality") or "128k"
    p["10"] = {
        "inputs": save_inputs,
        "class_type": "SaveAudioAdvanced",
        "_meta": {"title": "保存音频"},
    }
    return p


def build_t2a(params: dict) -> dict:
    """文生音乐（text -> music）。"""
    return build_music_workflow(params)


def build_a2a(params: dict) -> dict:
    """参考生音乐（reference audio -> music / cover）。"""
    if not (params.get("ref_audio") or "").strip():
        raise ValueError("参考生音乐需要一段参考音频")
    return build_music_workflow(params)


def estimate_seconds(params: dict) -> float:
    """粗略估计成曲时长（秒）—— 仅用于界面提示，实际以模型输出为准。"""
    try:
        return _clamp_duration(params.get("max_duration", YUE2_DEFAULT_DURATION))
    except Exception:
        return YUE2_DEFAULT_DURATION
