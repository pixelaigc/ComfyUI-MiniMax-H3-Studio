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

from . import music_style

# ------------------------------------------------------------------
# 模型清单（键 -> ComfyUI 原生 CheckpointLoaderSimple 读的本地文件名）
#
# 【只用 BF16 原精度版 —— 2026-11-09 用户拍板】
#   INT8 ConvRot 量化版已从本机删除（权重 + 代码/文案/测试引用全部清理）。
#   ComfyUI 走队列执行、不存在并发冲突，量化版没有存在价值。
#
# 【BF16 权重的来源】
#   由本机 m-a-p/YuE2-3B（HF 布局）+ m-a-p/YuE2-Vae 转换成 ComfyUI 原生
#   单文件布局而来（数值与官方 Comfy-Org 版逐字节一致）。
#   转换脚本：scripts/convert_yue2_hf_to_native.py
#
# 【models/yue2/ 不动】
#   models/yue2/YuE2-3B/ 与 models/yue2/YuE2-Vae/ 是第三方节点
#   **ComfyUI-FL-YuE2** 的原配依赖（其 downloads.py:resolve() 在
#   folder_paths.get_folder_paths("yue2") 下找 YuE2-3B/，缺失才联网下载）
#   —— 删了那条链路就断。
#   注意：**ComfyUI-YuE2 主节点走的是 HF 缓存**（yue2/storage.py:resolve_model()
#   对 "m-a-p/YuE2-3B" 调 snapshot_download），不依赖 models/yue2/。
#   两者都保留最稳。
#   面板走的是原生 CheckpointLoaderSimple，与它们都无关。
#
# 【note 字段不谎报可用性】
#   可用性描述由 `model_options()` 按**运行时实测**的 available 动态拼接，
#   权重在不在盘上，说的就是不在盘上。
#
# ------------------------------------------------------------------
#: YuE2 主模型（ComfyUI 原生 CheckpointLoaderSimple 读 checkpoints/）
YUE2_CHECKPOINTS = {
    "yue2_3b_bf16": {
        "label": "YuE2 3B · BF16 完整版",
        "ckpt": "yue2_3b_bf16.safetensors",
        "note": "官方 BF16 原精度版（3.63B 参数，628 个 BF16 张量），"
                "音质与生成稳定性都优于量化版。"
                "本机这份由 m-a-p/YuE2-3B（官方原始 BF16 权重）+ m-a-p/YuE2-Vae "
                "转换为 ComfyUI 原生单文件布局而来，数值与官方版逐字节一致。",
        "warn": "",
    },
}
YUE2_DEFAULT_CHECKPOINT = "yue2_3b_bf16"

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

#: 语义(人声/伴奏内容)阶段的采样参数。
#:
#: 【2026-11-09 对齐第三方】第三方链路（nonebot_plugin_novelai/h3_yue2.py:58-62,
#:  以及 yue2_remix_18node.json 节点 13）实际用的是：
#:     temperature=0.8  top_p=0.9  top_k=50  repetition_penalty=1.05
#: 我们之前用的是 ComfyUI 官方模板的 music 采样默认（1.0 / 0.95 / 100 / 1.2）。
#: 第三方那套是**实跑验证过出活效果**的值 —— 这里对齐它。
YUE2_MUSIC_SAMPLING = {
    "temperature": 0.8,
    "top_p": 0.9,
    "top_k": 50,
    "repetition_penalty": 1.05,
}

#: ABC 符号谱生成阶段的采样参数。
#:
#: 第三方链路的 abc_sampling 在 yue2 运行时是**硬编码**的
#: （ComfyUI-YuE2/nodes.py:327：temp .7 / top_p .9 / top_k 30 / rp 1.005 /
#:  penalty_window 100 / min_tokens 32），只有 abc_max_tokens 可调。
#: 我们的 abc_max_tokens 用第三方 remix 链路的值 8192（顶格），
#: 文生链路用 4096 —— 这里统一取 4096（第三方文生链路的值，h3_yue2.py:65）。
YUE2_ABC_SAMPLING = {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 30,
    "repetition_penalty": 1.005,
    "penalty_window": 100,
    "max_abc_tokens": 4096,
}

#: 语义 token 数上限（第三方 yue2_infer 的硬顶是 16000）。
#:
#: 【与 max_duration 的关系】原生 YuE2GenerateMusic 没有 token cap 参数，
#: 它用 max_duration 秒数换算 max_tokens = max_duration * FRAMES_PER_SECOND(25)
#: （comfy_extras/nodes_yue2.py:73）。而原生节点内部还会按 prompt 长度
#: 二次收紧（comfy/text_encoders/yue2.py:253-258）。
#: 我们用 max_duration 做用户可见控制（更直观），并按下面这个上限夹紧，
#: 保证实际 token 数不超过第三方链路的量级。
YUE2_SEMANTIC_MAX_TOKENS = 16000

#: 采样器 / 调度器可选范围（KSampler 的合法取值）
YUE2_SAMPLERS = ["dpm_2", "euler", "res_multistep", "dpmpp_2m", "dpmpp_2m_sde",
                 "dpmpp_3m_sde", "ddim", "heun", "ddpm_uniform"]
YUE2_SCHEDULERS = ["sgm_uniform", "simple", "beta", "normal", "karras",
                    "exponential", "linear_quadratic", "ddim_uniform"]

#: 谱面规划模式（ABC planning）
#   full   = 先出「旋律 + 和弦」完整谱面（文生音乐推荐）
#   melody = 只出旋律（参考生音乐推荐 —— 保留原曲旋律线）
YUE2_MODES = ["full", "melody"]

#: 生成时长上限（秒）。
#:
#: 【2026-11-09 用户要求：移除「最长时长」参数，生成始终自然完成】
#:   原先这里有「最长时长」滑杆（10~600 秒），后端按用户值夹取 max_duration
#:   —— 也就是「按时长上限截断」。现在：**不再按时长上限截断**，固定给
#:   YuE2GenerateMusic.max_duration 一个「足够大、让模型自己吐 MUSIC_END 结束」
#:   的值。
#:
#: 【为什么是 900 而不是 600】
#:   节点 schema 上限是 900 秒（comfy_extras/nodes_yue2.py:56）。
#:   真正的天花板是**模型上下文**：Qwen3_8BConfig.max_position_embeddings
#:   = 24576（comfy/ldm/yue2/model.py:18），且 encode_token_weights 里
#:   max_tokens 会被压到 `context - max(len(prefix), len(negative))`。
#:   24576 token / 25 frames-per-second ≈ 983 秒，但 prefix（风格+歌词+ABC）
#:   也要占位置，实际可用略小于 900。
#:   给 900 是「节点 schema 上限」—— 超过这个数 ComfyUI 会直接拒绝参数。
#:
#: 【「自然完成」的语义】
#:   YuE2GenerateMusic 的 max_duration 是「**最大**时长」不是目标时长：
#:   模型生成到 MUSIC_END 结束标记就停（comfy/text_encoders/yue2.py 的
#:   `_generate()`：token == MUSIC_END 就 return history, False）。
#:   给足上限 = 让它自然结束。
#:
#: 【与 semantic_max_tokens 的关系】
#:   原生节点内部还会按 prompt 长度二次收紧 token 预算
#:   （comfy/text_encoders/yue2.py:253-258），所以真实时长由
#:   「歌词段落数 + BPM + token 预算」共同决定，与用户设的上限无关。
YUE2_MAX_DURATION = 900.0
YUE2_MIN_DURATION = 4.0
YUE2_DEFAULT_DURATION = 900.0

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


async def model_options_async() -> dict:
    """异步版 model_options —— 给 aiohttp 的 async handler 用。

    【为什么要异步版】
    model_options() 里调了 music_style.style_options() 6 次，
    而 style_options() 内部是**同步阻塞**的 urllib 拉 ComfyUI
    /object_info（timeout 10s）。在 aiohttp 事件循环里直接调，
    整个 server 会被卡住 —— 实测表现为「打开工作台后所有接口都超时」，
    连首页都打不开（因为前端首屏就会请求 /api/music-options）。

    这里把阻塞部分挪到线程池（yue2_schema.get_enums_async），
    事件循环不阻塞。

    顺带修一个真问题：原实现里 style_options() 被调 6 次，
    缓存未命中时会打 **6 次** ComfyUI。现在只取一次枚举再组装。
    """
    style = await music_style.style_options_async()
    opts = model_options()
    # 用异步取到的枚举覆盖（model_options() 里那 6 次同步调用只为拼这几项）
    opts["style_fields"] = style["fields"]
    opts["style_defaults"] = style["defaults"]
    opts["style_enums"] = style["enums"]
    opts["lyric_templates"] = style["lyric_templates"]
    opts["lyric_template_default"] = style["lyric_template_default"]
    opts["length_hint_text"] = style["length_hint_text"]
    return opts


async def model_options_async() -> dict:
    """异步版 model_options —— 给 aiohttp 的 async handler 用。

    【为什么要异步版】
    model_options() 里的结构化风格部分会调 music_style.style_options()，
    而后者内部是**同步阻塞**的 urllib 拉 ComfyUI /object_info（timeout 10s）。
    在 aiohttp 事件循环里直接调，整个 server 会被卡住 —— 实测表现为
    「打开工作台后所有接口都超时」，连首页都打不开
    （因为前端首屏就会请求 /api/music-options）。

    这里把阻塞部分挪到线程池（yue2_schema.get_enums_async），
    事件循环不阻塞。

    顺带修一个真问题：原实现里 style_options() 被调 6 次，
    缓存未命中时会打 **6 次** ComfyUI。现在只取一次枚举再组装。
    """
    style = await music_style.style_options_async()
    return _model_options_core(style)


def model_options() -> dict:
    """给前端的模型清单（含本机可用性）。"""
    return _model_options_core(music_style.style_options())


def _model_options_core(style: dict) -> dict:
    """model_options 的内核 —— 结构化风格部分由调用方传入（避免重复拉 ComfyUI）。"""
    checkpoints = {}
    for key, m in YUE2_CHECKPOINTS.items():
        available = checkpoint_available(key)
        checkpoints[key] = {
            "label": m["label"],
            "ckpt": m["ckpt"],
            # 可用性描述按**运行时实测**动态拼接 —— 不再写死「未下载/不可用」
            # 这种权重到货后就是假话的文案。
            # 措辞要说清「缺的是**这种格式**的单文件」，而不是「本机没有 BF16」——
            # 本机其实有 BF16（HF 目录结构），只是原生节点不认那种布局。
            "note": m["note"] + (
                "本机已就绪（models/checkpoints/）。"
                if available else
                f"本机未安装该**格式**的权重（models/checkpoints/{m['ckpt']}）："
                "现有 m-a-p/YuE2-3B 是 HF 目录结构，原生节点不识别，暂不可用。"
            ),
            "warn": m["warn"],
            "available": available,
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
        # 时长上限已移除（2026-11-09 用户要求：生成始终自然完成）。
        # 这里只保留 max_duration 让前端知道「上限」这个事实，
        # 前端**不提供**任何时长控件，也不再提交 max_duration。
        "max_duration": YUE2_MAX_DURATION,
        "formats": YUE2_AUDIO_FORMATS,
        "default_format": YUE2_DEFAULT_FORMAT,
        # 结构化风格（对齐第三方 YuE2StylePrompt，2026-11-09）
        "style_fields": style["fields"],
        "style_defaults": style["defaults"],
        "style_enums": style["enums"],
        "lyric_templates": style["lyric_templates"],
        "lyric_template_default": style["lyric_template_default"],
        # length_hint -> style 串里的英文时长描述。
        # 【审查 P1-1】前端原先硬编码了第三份 length_hint 映射
        # （web/music.js 的 STYLE_LENGTH_HINT_TEXT）—— 那是「同一常量
        # 写三处」的典型隐患。现在由服务端下发，前端不再硬编码。
        "length_hint_text": style["length_hint_text"],
    }


def _resolve_duration(params: dict) -> float:
    """
    解析生成时长上限。

    【2026-11-09 用户要求：移除「最长时长」参数，生成始终自然完成】
    用户端已没有时长滑杆了，所以这里**不再读用户传的 max_duration**，
    也不再按它夹取 —— 固定返回 YUE2_MAX_DURATION（节点 schema 上限 900 秒）。
    YuE2GenerateMusic 的 max_duration 语义是「最大时长」：模型生成到
    MUSIC_END 结束标记就停，所以给足上限 = 「让它自然完成」。

    仍然读 params 里的 max_duration 是为了**兼容历史任务重放**：
    旧任务库里存着 max_duration=30（用户当时选的），重放时若按新语义走
    会得到 900 秒的曲子，与当初产物不一致。历史值 < 上限的按原值走，
    >= 上限的按上限走。
    """
    legacy = params.get("max_duration")
    if legacy is None:
        return YUE2_MAX_DURATION
    try:
        seconds = float(legacy)
    except (TypeError, ValueError):
        return YUE2_MAX_DURATION
    if seconds >= YUE2_MAX_DURATION:
        return YUE2_MAX_DURATION
    return max(YUE2_MIN_DURATION, seconds)


def _resolve_style(params: dict) -> str:
    """合成最终送进模型的 style 串。

    【对齐第三方，2026-11-09】
    第三方 YuE2StylePrompt 把 12 个结构化字段拼成 style（nodes.py:184-212）。
    我们的面板既有自由文本风格框，也新增了结构化字段 —— 两者合并：
    **结构化在前、自由文本在后**（自由文本是用户自己的具体描述，放后面更靠近
    [Lyrics] 段、权重更高）。

    「什么都没填」时：只用结构化字段的第三方默认值，仍能出活；
    结构化字段也全空时：回落到自由文本；两者都空 -> 报错（与旧行为一致）。
    """
    free_text = (params.get("prompt") or "").strip()
    fields = music_style.style_fields_from_params(params)
    structured = music_style.build_style_prompt(
        fields["language"], fields["genre"], fields["mood"],
        fields["vocal"], fields["vocal_color"], fields["instruments"],
        fields["tempo_bpm"], fields["energy"], fields["length_hint"],
        fields["extra"], fields["custom_language"], fields["custom_genre"],
    )
    if structured and free_text:
        return f"{structured}, {free_text}"
    if structured:
        return structured
    if free_text:
        return free_text
    raise ValueError("音乐风格描述不能为空")


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
      checkpoint    主模型 key（默认 yue2_3b_bf16）
      audio_encoder 参考生音乐的音频编码器 key
      ref_audio     参考音频文件名（ComfyUI input 里的 'subfolder/name'）
      max_duration  最长时长（秒），默认 60
      seed          随机种子
      steps / cfg / sampler / scheduler
      format        输出音频格式 flac / mp3 / opus
      music_temperature / music_top_p / music_top_k / music_repetition_penalty
      abc_temperature / abc_top_p / abc_top_k / abc_repetition_penalty
      abc_max_tokens / abc_penalty_window

      style_*       结构化风格字段（language / genre / mood / vocal /
                    vocal_color / instruments / tempo_bpm / energy /
                    length_hint / extra / custom_language / custom_genre）——
                    对齐第三方 YuE2StylePrompt 的 12 字段语义。
                    与自由文本 prompt 合并：结构化在前，自由文本在后。
    """
    ckpt_name = _resolve_checkpoint(params)
    style = _resolve_style(params)
    # 歌词：用户填了就用用户的；没填则用第三方段落骨架兜底
    # （第三方 YuE2LyricsTemplate：override 非空 -> 忽略 template，nodes.py:281-283）
    lyrics = params.get("lyrics") or ""
    if isinstance(lyrics, str):
        lyrics = lyrics.strip()
    lyrics = music_style.resolve_lyrics(lyrics, params.get("lyrics_template") or "V-C-V-C-bridge-C")

    # 谱面规划模式（full / melody）。
    #
    # 【关键，2026-11-09】历史上 params["mode"] 被 server.py 用作 HTTP 生成
    # 模式（t2a / a2a）覆盖掉了，谱面模式因此丢失。现在：
    #   - 优先读 params["plan_mode"]（server.py 落库时单独存的那份）
    #   - 其次读 params["music_plan"]（旧字段名兼容）
    #   - 最后才回退 params["mode"]（仅当它本身就是 full/melody 时）
    mode = (params.get("plan_mode") or params.get("music_plan") or "").strip()
    if mode not in YUE2_MODES:
        legacy = (params.get("mode") or "").strip()
        mode = legacy if legacy in YUE2_MODES else "full"

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
    max_duration = _resolve_duration(params)
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
    """
    粗略估计成曲时长（秒）—— 仅用于界面提示，实际以模型输出为准。

    【2026-11-09】时长上限已移除，生成自然完成，所以这里返回的是
    「时长上限」而不是「预计时长」。真正时长由模型的 MUSIC_END 决定。
    """
    try:
        return _resolve_duration(params)
    except Exception:
        return YUE2_MAX_DURATION
