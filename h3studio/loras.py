"""Z-Image 可用 LoRA 清单与服务端持久化配置。

只收录**本机实际存在**的 Z-Image 系列 LoRA —— 清单由 ComfyUI 自己的
folder_paths 扫描得出（跟 /object_info/LoraLoaderModelOnly 同一个来源），
不写死任何可能不存在的文件名。

每个 LoRA 的说明字段来自：
  * safetensors 头部 __metadata__（modelspec / ss_* 字段）
  * 上游仓库 README（ModelScope / Civitai），触发词即上游 tags
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from . import lora_damping

# ------------------------------------------------------------------ 已知说明
# 说明来源全部标注在 source 字段里，界面上可展开查看。
# trigger 为上游 README tags 中明确标出的触发词；上游未标注的一律留空，
# 不用「猜测」填满。
LORA_CATALOG: dict[str, dict] = {
    # ---- Qwen-Image 2.1 ----
    # 元数据实读：ss_base_model_version=qwen_image_2 / ss_output_name=bfs_qwen_2.1，
    # ss_tag_frequency 里带 "1_head_swap: start with <image1> as the base image..."
    # -> 这是「换头」效果 LoRA，不是加速器。官方图片编辑工作流把它固定在强度 1.0，
    #    配合 <image1>/<image2> 做头部替换。
    "qwen21/bfs_head_v1_qwen_2.1.safetensors": {
        "display": "换头（Head Swap，Qwen-Image 2.1）",
        "base": "Qwen-Image 2.1",
        "trigger": "1_head_swap: start with <image1> as the base image, keeping its "
                   "lighting, environment, and background. remove the head from <image1> "
                   "completely and replace it with the head from <image2>, strictly "
                   "preserving the hair, eye color, nose structure from <image2>.",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "多图编辑：把 <image2> 的头部替换到 <image1> 上，保留图1的光线/环境/"
                 "背景与头部朝向、表情。官方图片编辑工作流固定用强度 1.0。",
        "source": "safetensors 元数据（ss_tag_frequency / ss_base_model_version）",
        "strength_source": "官方图片编辑工作流 Qwen+image+2.1图片编辑.json 固定强度 1.0",
    },
    "qwen21/RadianceChromeVoluptuous_QwenImage2.1_v1.0.safetensors": {
        "display": "Radiance Chrome Voluptuous（Qwen-Image 2.1 风格）",
        "base": "Qwen-Image 2.1",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "风格 LoRA（体积感/光泽向风格）。上游 metadata 里没有触发词标注。",
        "source": "本机 /mnt/usb2t/comfyui-models/loras/qwen21/",
        "strength_source": "上游未标注，按 LoRA 默认强度 1.0",
    },
    "Kook_Zimage_如梦似幻.safetensors": {
        "display": "如梦似幻（Kook Z-Image 滤镜）",
        "base": "Tongyi-MAI/Z-Image（Z_IMAGE_TURBO）",
        "trigger": "如梦似幻",
        "recommended_strength": 1.0,
        # effect = 效果类（人像/风格/特效），可作为用户可选项
        # acceleration = 加速/蒸馏类（改变步数/CFG 语义），固定内置于工作流
        "kind": "effect",
        "scene": "梦幻朦胧/柔光滤镜风格，官方 tags 标注为「滤镜lora」。",
        "source": "ModelScope KookYan/Kook_Zimage_Rmsh（README tags）",
        # 上游 README 未给出推荐强度，1.0 = LoRA 默认强度（无 alpha 缩放）
        "strength_source": "上游未标注，按 LoRA 默认强度 1.0",
    },
    "Kook_Zimage_瑶光.safetensors": {
        "display": "瑶光（Kook Z-Image 真实幻想）",
        "base": "Tongyi-MAI/Z-Image（Z_IMAGE_TURBO）",
        "trigger": "瑶光 / 真实幻想",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "写实向「真实幻想」质感，官方 tags 标注为「瑶光 / 真实幻想」。",
        "source": "ModelScope KookYan/Kook_Zimage_Yg（README tags）",
        "strength_source": "上游未标注；本机实测：强度 1.0 出彩色噪点，"
                           "根因是 context_refiner 扰动过大，已自动做 0.8 阻尼",
    },
    "dejpeg_v3.safetensors": {
        "display": "去压缩伪影（DeJPEG v3）",
        "base": "Z-Image",
        # 上游元数据为空（safetensors __metadata__ 是 {}）—— 没有触发词标注，
        # 不编造。实测它 patch 的是 diffusion_model.layers.*（UNet 主干），
        # 与 Z-Image-Turbo 的层结构 100% 对上（attention_hit=1.0），
        # 实测在 ComfyUI 里 attach 出 180 patches、真实生效。
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "针对 JPEG 压缩伪影的修复型效果 LoRA（patch UNet 主干层）。",
        "source": "本机 safetensors 层结构比对（上游未提供仓库/README）",
        "strength_source": "上游未标注，按 LoRA 默认强度 1.0",
    },
    "Z-Image-Fun-Lora-Distill-8-Steps-2603-ComfyUI.safetensors": {
        "display": "Z-Image-Fun 蒸馏加速 8 步（2603）",
        "base": "Z-Image Base（ZImageBase）",
        "trigger": "",
        "recommended_strength": 1.0,
        # 蒸馏加速：改变步数/CFG 的语义，不能当效果 LoRA 让用户随便挑
        "kind": "acceleration",
        "scene": "步数+CFG 双蒸馏加速：8 步出图，比 Base 的 25 步快 3 倍；"
                 "2603 版新增随机时间步策略，sigma<0.5 区间不再糊。",
        "source": "Civitai 2362961 / HF alibaba-pai/Z-Image-Fun-Lora-Distill",
        "strength_source": "上游默认强度 1.0",
    },
    "z-image/Z-Image-Fun-Distill-Lora.safetensors": {
        "display": "Z-Image 蒸馏 LoRA（本地抽取版 rank64）",
        "base": "Z-Image（zimage）",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "acceleration",
        "scene": "从 Z-Image 权重抽出的 rank64 蒸馏 LoRA（ModelDiffToLoRA 抽取），"
                 "用于低步数加速，本地无上游训练参数说明。",
        "source": "本地 safetensors __metadata__（extracted_by: comfyui-zimage-realtime-lora）",
        "strength_source": "上游未标注，按 LoRA 默认强度 1.0",
    },
    # ---- MOSS-Image ----
    # 元数据实读（moss_xvstyle 的 safetensors __metadata__）：
    #   base_model = "OpenMOSS-Team/MOSS-Image-SFT (train parent: MOSS-Image-Base)"
    #   trigger_word = moss_xvstyle / lora_alpha=16 / lora_rank=16
    #   recommended_strength = 0.7 / recommended_steps = 30 / recommended_cfg = 3.0
    #   source = https://huggingface.co/axiomofmind/Moss-image-nsfw-uncensored-lora
    "moss/moss_xvstyle_nsfw_uncensored_1024_w0.7_s30_axiomofmind_v1.safetensors": {
        "display": "XV 风格 · 人体质感（MOSS-Image）",
        "base": "MOSS-Image-Base / MOSS-Image-SFT",
        "trigger": "moss_xvstyle",
        "recommended_strength": 0.7,
        "kind": "effect",
        "scene": "成人写真 / 人体与皮肤细节质感风格 LoRA。上游标注 trigger=moss_xvstyle、"
                 "recommended_strength=0.7 / steps=30 / cfg=3.0 / 1024×1024。"
                 "⚠️ 单独使用触发词时底模会把 moss 理解成苔藓（英文 moss = 苔藓），"
                 "必须搭配画面描述使用。",
        "source": "safetensors __metadata__（axiomofmind/Moss-image-nsfw-uncensored-lora）",
        "strength_source": "上游 __metadata__ recommended_strength = 0.7",
    },
    # hires LoHa 系列：ComfyUI 作者本人训的 4MP+ 高分辨率 LoHa。
    # 本机做过逐位无损的 qkv 拆分（上游融合 qkv vs 本机 diffusers 分离 q/k/v，
    # 不拆会静默丢 42 个 target）—— 见 loras/moss/_INDEX.md §2.3。
    "moss/moss_hires_lora_v0.1_qkv_split.safetensors": {
        "display": "4MP+ 高分辨率增强 LoHa v0.1（MOSS-Image · 全 42 层覆盖）",
        "base": "MOSS-Image-Base / MOSS-Image-SFT",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "ComfyUI 作者训的 4 兆像素以上高分辨率 LoHa。v0.1 覆盖最广"
                 "（全 42 层 × 全 4 target，本机实测 252 patches 全命中）。"
                 "作者配方：er_sde / simple / 25 步 / CFG 4.0 / shift 5 / SLG 关闭。",
        "source": "https://huggingface.co/comfyanonymous/MOSS_image_MAX_ULTRA_hires_lora_test"
                  "（本机做了 qkv 拆分转换）",
        "strength_source": "上游默认强度 1.0",
    },
    "moss/moss_hires_lora_v0.2_qkv_split.safetensors": {
        "display": "4MP+ 高分辨率增强 LoHa v0.2（MOSS-Image）",
        "base": "MOSS-Image-Base / MOSS-Image-SFT",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "4 兆像素以上高分辨率 LoHa（204 targets：ffn_up_gate 全 42 层 + "
                 "qkv 全 42 层 + o 21 层 + ffn_down 15 层，本机实测 204 patches 命中）。"
                 "与 v0.3 同 target 集但权重不同。",
        "source": "https://huggingface.co/comfyanonymous/MOSS_image_MAX_ULTRA_hires_lora_test"
                  "（本机做了 qkv 拆分转换）",
        "strength_source": "上游默认强度 1.0",
    },
    "moss/moss_hires_lora_v0.3_qkv_split.safetensors": {
        "display": "4MP+ 高分辨率增强 LoHa v0.3（MOSS-Image · 官方 Space 同款）",
        "base": "MOSS-Image-Base / MOSS-Image-SFT",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "4 兆像素以上高分辨率 LoHa，官方 Space（hugging-apps/moss-image-hires-lora）"
                 "用的就是这版，默认首选。204 targets，本机实测 204 patches 命中。",
        "source": "https://huggingface.co/comfyanonymous/MOSS_image_MAX_ULTRA_hires_lora_test"
                  "（本机做了 qkv 拆分转换）",
        "strength_source": "上游默认强度 1.0",
    },
    "moss/moss_hires_lora_v0.4_qkv_split.safetensors": {
        "display": "4MP+ 高分辨率增强 LoHa v0.4（MOSS-Image · 上游最新）",
        "base": "MOSS-Image-Base / MOSS-Image-SFT",
        "trigger": "",
        "recommended_strength": 1.0,
        "kind": "effect",
        "scene": "4 兆像素以上高分辨率 LoHa，上游最新版本（204 targets，"
                 "本机实测 204 patches 命中）。上游未提供示例图。",
        "source": "https://huggingface.co/comfyanonymous/MOSS_image_MAX_ULTRA_hires_lora_test"
                  "（本机做了 qkv 拆分转换）",
        "strength_source": "上游默认强度 1.0",
    },
}

# 只认这些关键词算「Z-Image 相关」，避免把 285 个 LoRA 全塞进面板
Z_IMAGE_MARKERS = ("zimage", "z-image", "z_image", "kook_zimage")
# Qwen-Image 2.1 家族的 marker（文件名里带这些字样的才可能是 2.1 的 LoRA）
QWEN_IMAGE_MARKERS = ("qwen_image_2.1", "qwen_image2.1", "qwenimage2.1",
                      "qwen_2.1", "qwen21")
# MOSS-Image 家族的 marker（文件名里带这些字样的才可能是 MOSS 的 LoRA）
MOSS_IMAGE_MARKERS = ("moss_image", "moss-image", "mossimage", "moss_hires",
                      "moss_xvstyle")

# LoRA 分类
#   effect       —— 效果类（滤镜 / 写实风格 / 人像增强）：用户可自行挑选
#   acceleration —— 加速 / 蒸馏类：改变步数、CFG、调度语义，
#                   必须与特定基模搭配，由工作流固定使用，**不作为用户可选项**
#                   （用户自行选择会与 Turbo 自身的蒸馏冲突，出图质量不可控）
KIND_EFFECT = "effect"
KIND_ACCELERATION = "acceleration"

# 加速/蒸馏类 LoRA 的判定关键词 —— 命中即排除出用户可选列表。
# 只对「上游目录里没登记过 kind」的 LoRA 生效（已登记的以登记为准）。
ACCELERATION_MARKERS = (
    "distill", "distillation", "accel", "speedup", "speed-up",
    "lightx2v", "lightning", "turbo_lora", "step4", "step8", "4step", "8step",
    "fewstep", "few-step", "lightning2", "sparse", "fast",
)


# ------------------------------------------------------------------ 兼容性检测
#
# 为什么要做这个检测（2026-11-08 实测教训）：
#   「瑶光」LoRA 名义上写着 Z-Image，但它的注意力层键名是
#   to_q / to_k / to_v / to_out.0（分离投影），
#   而 Z-Image-Turbo UNET 用的是 **融合的 qkv**（[11520, 3840]）。
#   ComfyUI 只能把它按名字近似映射到 fused qkv 上，
#   语义错位 + 权重强度过大 -> 出图变成彩色噪点。
#
# 检测方法（不依赖文件名，只看权重）：
#   1. 读 UNET 的 state_dict 键集合，得到「这个底模真实存在的层键」
#   2. 读 LoRA 的 lora_A/lora_B 键，得到「它想 patch 哪些层」
#   3. 算命中比例 + 每个 patch 相对原权重的扰动幅度
#   4. 命中率过低 或 扰动幅度过大 -> 判定为不兼容，不给用户选
#
# 关键阈值（实测标定，见 evidence/ 排查报告）：
#   - 注意力键命中率 < 0.8  -> 不兼容（要 patch 的层在底模里根本不存在）
#   - 相对扰动 > 0.08 (>8%)  -> 不兼容（会崩图，实测彩色噪点）
#
# 注意：Z-Image 用**融合 qkv**（[3*d, d]），而 Kook 系 LoRA 用分离投影
# to_q/to_k/to_v（[d, d]）。ComfyUI 的 model_lora_keys_unet 会把后者
# 正规地映射到 qkv 的三段（本机实测确认），所以**键名不同不代表不兼容** ——
# 真正的判据是「层在底模里完全找不到」以及「权重扰动幅度」。

# UNET 底模权重路径（与 workflows.Z_IMAGE_MODELS 一致）
Z_IMAGE_UNET_FILES = {
    "z_image_turbo_bf16": "z_image_turbo_bf16.safetensors",
    "z_image_base_bf16": "z_image_bf16.safetensors",
}

# 判定阈值
COMPAT_MIN_ATTENTION_HIT = 0.8      # 注意力层命中率下限（层在底模里根本不存在 -> 真不兼容）
COMPAT_MAX_RELATIVE_PERTURBATION = 0.08   # 相对扰动上限（8%）：超过则强度 1.0 会崩图
# 安全扰动上限 —— 强度 1.0 时也不该超过这个值
COMPAT_SAFE_RELATIVE_PERTURBATION = 0.08
# 实测标定（2026-11-08，用户真实提示词 + 真实种子 132199052268679，
#              用户真实失败种子下强度 1.0 复现 MAE=0.0000）：
#   瑶光整体扰动 13.2% -> 强度 1.0 出彩色噪点
#
# 2026-11-08 进一步定位（模块消融实验，见 h3studio/lora_damping.py）：
#   真正的问题不在「整体扰动」，而在 **context_refiner** 这一组模块：
#     瑶光     context_refiner  max 12.85% / median 9.62%
#     如梦似幻 context_refiner  max  1.36% / median 0.74%
#   context_refiner 是 Z-Image 里**联合处理文本条件 + 潜变量**的模块，
#   长提示词下它的输入本来就偏离训练分布，再叠 10% 量级扰动 -> 采样发散。
#
#   消融（同一提示词 / 同一种子 / 强度 1.0）：
#     原始            base 14.5(噪) tmpl 14.6(噪) tmpl+瑶光 14.6(噪)
#     ctx x0.5         base  8.6(ok) tmpl  8.5(ok) tmpl+瑶光  8.4(ok)
#     去掉 ctx         base  8.2(ok) tmpl  7.8(ok) tmpl+瑶光  7.7(ok)
#
#   => 修复手段是**对 context_refiner 做阻尼**（派生 LoRA），
#      而不是全局降强度：全局降到 0.8 只是把阈值挪个位置，
#      崩不崩仍取决于提示词文本条件复杂度（实测存在刀刃效应）。
#      因此这里**不再对这类 LoRA 封顶强度**，改由阻尼保证稳定。
COMPAT_SAFE_STRENGTH_CAP = 1.0
# 换算到「等效扰动」的安全上限
COMPAT_SAFE_RELATIVE_PERTURBATION = 0.08
# context_refiner 相对扰动超过这个值 -> 需要做阻尼派生
COMPAT_CTX_DAMPING_THRESHOLD = 0.03


def _read_safetensors_header(path: Path) -> dict:
    """只读 safetensors 头部（不加载权重本体，快）。"""
    import struct
    try:
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            return json.loads(handle.read(size))
    except Exception:
        return {}


# Z-Image 底模独有的模块前缀（实测 z_image_turbo / z_image_bf16 的键集）
#   context_refiner.* / noise_refiner.* / layers.N.attention.qkv
# SDXL / Flux / H3 的 LoRA 都不含这些前缀 —— 这是区分它们的关键。
_Z_IMAGE_MODULE_PREFIXES = ("context_refiner.", "noise_refiner.", "layers.")


def _looks_like_z_image_layers(path: Path) -> bool:
    """判断一个 LoRA 的层结构是不是 Z-Image 家族的。

    只在「文件名不含 z-image 字样、但可能是这一族的 LoRA」时用
    （实测 `dejpeg_v3.safetensors` 就是这种：文件名毫无线索，但层结构
    与 Z-Image 完全对应，真实 attach 出 180 patches）。

    判据（实测标定）：
      1. LoRA patch 的层里必须出现 Z-Image 底模独有的模块前缀
         （context_refiner / noise_refiner / layers.）
      2. 必须含 Z-Image 的**融合 qkv** 层（`layers.N.attention.qkv`），
         或分离投影 to_q/to_k/to_v（ComfyUI 会映射到融合 qkv）
      3. 必须含 context_refiner —— 这是 Z-Image/Lumina2 系最独特的模块，
         SDXL / Flux / H3 都没有
    """
    try:
        header = _read_safetensors_header(path)
    except Exception:
        return False
    bases = _lora_layer_bases(header)
    if not bases:
        return False
    # Z-Image 底模的层结构（实测 z_image_turbo / z_image_bf16）：
    #   context_refiner.N.attention.{qkv,k_norm,q_norm,out}
    #   noise_refiner.N.attention.{...}
    #   layers.N.attention.{qkv,...}
    # LoRA 侧可以是这三类中的任意子集 —— 实测：
    #   Kook_Zimage_*      只 patch context_refiner + noise_refiner + layers
    #   dejpeg_v3          只 patch layers.*（**没有** context_refiner）
    #   官方蒸馏 LoRA      只 patch diffusion_model.context_refiner.*
    # 所以「有没有 context_refiner」不能作为硬条件（会漏掉 dejpeg_v3）。
    #
    # 真正的判据是「层名能不能在 Z-Image 底模里 100% 命中」——
    # 这由 check_compatibility() 的 attention_hit 负责（要求 == 1.0）。
    # 这里只做**结构形状**的粗筛：必须出现 Z-Image 独有的模块前缀。
    # 注意：LoRA 层名可能带 `diffusion_model.` 前缀（实测 dejpeg_v3 与官方
    # 蒸馏 LoRA 都带），所以要先剥掉前缀再判断模块名。
    def _module_of(base: str) -> str:
        for pre in ("diffusion_model.", "lora_unet_"):
            if base.startswith(pre):
                base = base[len(pre):]
                break
        return base.split(".")[0]

    has_zimg_module = any(
        _module_of(b) in ("context_refiner", "noise_refiner", "layers")
        for b in bases)
    if not has_zimg_module:
        return False
    # 必须含 Z-Image 的注意力层（融合 qkv，或分离投影 to_q/to_k/to_v）
    has_fused = any(".attention.qkv" in b for b in bases)
    has_split = any(b.endswith((".attention.to_q", ".attention.to_k",
                                ".attention.to_v")) for b in bases)
    return has_fused or has_split


def _lora_layer_bases(header: dict) -> set[str]:
    """从 header 提取 LoRA 想 patch 的层名集合（去掉 lora_A/lora_B 后缀）。"""
    bases: set[str] = set()
    for key in header:
        if key == "__metadata__":
            continue
        for marker in (".lora_A.default.weight", ".lora_B.default.weight",
                       ".lora_A.weight", ".lora_B.weight",
                       ".lora_down.weight", ".lora_up.weight"):
            if key.endswith(marker):
                bases.add(key[: -len(marker)])
                break
    return bases


def _unet_layer_keys(unet_path: Path) -> set[str]:
    """UNET 的层键集合（去掉 .weight/.bias 后缀）。

    ⚠️ **必须带 `diffusion_model.` 前缀**（2026-11-08 实测教训）。

    ComfyUI 加载 UNET 后，模型 state_dict 的键**统一带 `diffusion_model.` 前缀**
    （comfy/lora.py:192-196 的 `model_lora_keys_unet()` 就是按这个前缀建映射的）。
    而 LoRA 权重文件里的层名**带或不带**这个前缀都实测存在：
      * `dejpeg_v3.safetensors` / `Z-Image-Fun-Lora-Distill-8-Steps-2603`
        -> 带 `diffusion_model.` 前缀
      * `Kook_Zimage_*` -> 不带前缀（`context_refiner.0.attention.to_q`）

    之前这里返回**裸键**（无前缀），跟带前缀的 LoRA 一比全是 0 命中，
    导致 `dejpeg_v3` 和官方蒸馏 LoRA 被**误判为「不兼容」** ——
    而实测它们在 ComfyUI 里明明 attach 成功（180 / 136 patches attached）。

    所以这里返回**带前缀**的键集（跟 ComfyUI 运行时一致），
    由 `_layer_hit()` 负责把 LoRA 侧的多种前缀写法都试一遍。
    """
    header = _read_safetensors_header(unet_path)
    keys: set[str] = set()
    for key in header:
        if key == "__metadata__":
            continue
        for suffix in (".weight", ".bias"):
            if key.endswith(suffix):
                keys.add("diffusion_model." + key[: -len(suffix)])
                break
        else:
            keys.add("diffusion_model." + key)
    return keys


# LoRA 层名可能带的前缀写法（实测三种都存在）
_LORA_KEY_PREFIXES = ("diffusion_model.", "lora_unet_", "")


def _lora_key_variants(base: str) -> set[str]:
    """把一个 LoRA 层名展开成所有可能的等价写法。

    ComfyUI 的 `model_lora_keys_unet()` 同时登记多种别名（comfy/lora.py:192-212）：
      * `diffusion_model.<层名>`                （通用格式）
      * `lora_unet_<层名把点换成下划线>`          （diffusers 风格）
      * diffusers 的 to_q/to_k/to_v -> 融合 qkv 的映射
    比对时必须把 LoRA 侧的层名展开成全部候选再命中。
    """
    out: set[str] = set()
    for prefix in _LORA_KEY_PREFIXES:
        if prefix == "lora_unet_":
            out.add("lora_unet_" + base.replace(".", "_"))
            continue
        out.add(prefix + base)
    return out


def _layer_hit(base: str, unet_keys: set[str]) -> bool:
    """LoRA 的某个层能否在底模里找到（尝试全部前缀 / 别名写法）。"""
    if base in unet_keys:
        return True
    for cand in _lora_key_variants(base):
        if cand in unet_keys:
            return True
    # 分离投影 to_q/to_k/to_v -> 融合 qkv 的三段（ComfyUI 的正规映射）
    for suffix, fused in ((".attention.to_q", ".attention.qkv"),
                          (".attention.to_k", ".attention.qkv"),
                          (".attention.to_v", ".attention.qkv"),
                          (".attention.to_out.0", ".attention.out")):
        if base.endswith(suffix):
            stem = base[: -len(suffix)]
            for pre in ("diffusion_model.", ""):
                if pre + stem + fused in unet_keys:
                    return True
    return False


def _z_image_unet_path() -> Path | None:
    """定位本机 Z-Image-Turbo UNET 权重（用于兼容性比对）。"""
    for models_dir in _model_dirs():
        for name in Z_IMAGE_UNET_FILES.values():
            for sub in ("diffusion_models", "unet"):
                candidate = models_dir / sub / name
                if candidate.is_file():
                    return candidate
    return None


def _qwen_image_unet_path() -> Path | None:
    """定位本机 Qwen-Image 2.1 UNET 权重（用于兼容性比对）。"""
    for models_dir in _model_dirs():
        for sub in ("diffusion_models", "unet"):
            candidate = models_dir / sub / "qwen_image_2.1_bf16.safetensors"
            if candidate.is_file():
                return candidate
    return None


def _model_dirs() -> list[Path]:
    """本机 models 目录（与 ComfyUI 实际扫描的目录同源）。

    包含 extra_model_paths.yaml 里声明的所有 base_path —— 本机 Z-Image 权重
    放在外接盘（/mnt/nvme1t_colorful/comfyui-models），只查默认目录会找不到。
    """
    dirs: list[Path] = []
    cfg = Path(os.environ.get("H3STUDIO_CONFIG", "")) if os.environ.get("H3STUDIO_CONFIG") else None
    if cfg and cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            if data.get("models_dir"):
                dirs.append(Path(data["models_dir"]))
        except Exception:
            pass
    dirs += [Path("/home/ubuntu/comfyui/models"), Path("/root/ComfyUI/models")]
    # extra_model_paths.yaml 里声明的额外 base_path
    for extra in (Path("/home/ubuntu/comfyui/extra_model_paths.yaml"),
                   Path("/root/ComfyUI/extra_model_paths.yaml")):
        if not extra.is_file():
            continue
        try:
            dirs.extend(_parse_extra_base_dirs(extra.read_text(encoding="utf-8")))
        except Exception:
            pass
    out: list[Path] = []
    for base in dirs:
        if base not in out:
            out.append(base)
    return out


def _parse_extra_base_dirs(raw: str) -> list[Path]:
    """从 extra_model_paths.yaml 解析出所有 section 的 base_path。"""
    out: list[Path] = []
    base_path: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.split("#")[0].rstrip()
        if not line.strip():
            continue
        indented = line.startswith((" ", "\t"))
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if not indented:
            base_path = None
            continue
        if key == "base_path" and val:
            base_path = val
        elif base_path and key in ("diffusion_models", "unet", "loras"):
            candidate = Path(base_path)
            if candidate.is_dir():
                out.append(candidate)
    return out


def _attention_hit_ratio(lora_bases: set[str], unet_keys: set[str]) -> tuple[float, int, int]:
    """注意力层的命中率。

    注意力层 = 键名里含 '.attention.' 的 LoRA layer。
    命中 = 该 layer 名能在 UNET 的同名层里找到（或其 fused / 前缀变体）。

    命中判定统一走 `_layer_hit()` —— 它把 LoRA 侧层名的三种前缀写法
    （`diffusion_model.` / `lora_unet_` / 无前缀）与 to_q/to_k/to_v
    -> 融合 qkv 的映射都试一遍。
    """
    attn = {b for b in lora_bases if ".attention." in b}
    if not attn:
        return 1.0, 0, 0
    hit = 0
    for base in attn:
        if _layer_hit(base, unet_keys):
            hit += 1
    return (hit / len(attn) if attn else 1.0), hit, len(attn)


# 兼容性检测缓存 —— 检测要读 UNET 权重（12GB），不能每次调用都重算。
# key = (lora 文件路径, mtime, size)；UNET 变了则整体失效。
_COMPAT_CACHE: dict = {}
_UNET_CACHE_KEY: tuple | None = None


def _cache_key(path: Path) -> tuple:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), 0, 0)


def clear_compat_cache() -> None:
    """清空兼容性缓存（换模型 / 换 LoRA 后调用）。"""
    global _UNET_CACHE_KEY
    _COMPAT_CACHE.clear()
    _UNET_CACHE_KEY = None


def check_compatibility(path: Path, unet_path: Path | None = None) -> dict:
    """检测一个 LoRA 与 Z-Image 底模的兼容性。

    返回:
      {
        "compatible": bool,
        "reason": str,            # 不兼容原因（给界面显示）
        "attention_hit": float,   # 注意力层命中率
        "relative_perturbation": float | None,  # 相对扰动（需读权重，慢；失败为 None）
        "suggested_strength": float,  # 安全推荐强度
      }
    """
    global _UNET_CACHE_KEY
    unet_path = unet_path or _z_image_unet_path()
    if unet_path is None:
        # 找不到 UNET 就无法判定 —— 不阻断，但标注「未验证」
        return {"compatible": True, "reason": "", "attention_hit": 1.0,
                "relative_perturbation": None, "suggested_strength": 1.0,
                "strength_cap": 1.0, "verified": False}

    # 缓存命中则直接返回
    key = _cache_key(path)
    unet_key = _cache_key(unet_path)
    if _UNET_CACHE_KEY == unet_key and key in _COMPAT_CACHE:
        return dict(_COMPAT_CACHE[key])

    lora_header = _read_safetensors_header(path)
    bases = _lora_layer_bases(lora_header)
    unet_keys = _unet_layer_keys(unet_path)

    hit_ratio, hit, total = _attention_hit_ratio(bases, unet_keys)
    result = {
        "compatible": True,
        "reason": "",
        "attention_hit": round(hit_ratio, 4),
        "attention_hit_count": hit,
        "attention_total": total,
        "relative_perturbation": None,
        "suggested_strength": 1.0,
        # 强度上限 —— 1.0 表示无需限制
        "strength_cap": 1.0,
        # context_refiner 相对扰动 —— 决定是否需要阻尼派生
        "ctx_perturbation": None,
        "ctx_damping": None,
        "verified": True,
    }

    if total and hit_ratio < COMPAT_MIN_ATTENTION_HIT:
        result["compatible"] = False
        result["reason"] = (
            f"注意力层在 Z-Image 底模里不存在（仅 {hit}/{total} 命中）："
            "该 LoRA patch 的层与底模结构对不上，加载后出图会变成彩色噪点。")
        _COMPAT_CACHE[key] = dict(result)
        _UNET_CACHE_KEY = unet_key
        return result

    # 相对扰动需要读权重（较慢），只在结构兼容时做
    try:
        pert = _relative_perturbation(path, unet_path)
        result["relative_perturbation"] = pert
    except Exception:
        pert = None

    # context_refiner 扰动 —— 这是「出彩色噪点」的决定性指标
    try:
        from . import lora_damping
        ctx = lora_damping._ctx_damping_needed(path, unet_path)
        result["ctx_perturbation"] = ctx
        if ctx is not None and ctx >= COMPAT_CTX_DAMPING_THRESHOLD:
            factor = lora_damping.DEFAULT_CTX_DAMPING
            result["ctx_damping"] = factor
            result["strength_cap"] = 1.0
            result["suggested_strength"] = 1.0
            result["reason"] = (
                f"context_refiner 权重扰动较大（相对原权重 {ctx * 100:.1f}%）："
                f"该模块联合处理文本条件与潜变量，长提示词下会把采样推到发散、"
                f"出图为彩色噪点。已自动对 context_refiner 做 {factor:.2f} 阻尼，"
                f"强度可保持 1.0。")
    except Exception:
        pass

    if pert is not None and pert > 0 and not result.get("ctx_damping"):
        # 整体扰动大但 context_refiner 正常 -> 仍按比例给个保守推荐强度
        try:
            result["suggested_strength"] = round(
                min(1.0, COMPAT_SAFE_RELATIVE_PERTURBATION / pert), 2)
        except Exception:
            pass
    _COMPAT_CACHE[key] = dict(result)
    _UNET_CACHE_KEY = unet_key
    return result


def _relative_perturbation(lora_path: Path, unet_path: Path) -> float | None:
    """采样若干层，算 LoRA 有效权重相对原权重的平均扰动比例。"""
    try:
        import torch
        from safetensors.torch import load_file
    except Exception:
        return None
    try:
        lora_sd = load_file(str(lora_path))
        unet_sd = load_file(str(unet_path))
    except Exception:
        return None

    def pick(prefix: str) -> tuple | None:
        for key, value in lora_sd.items():
            if key.startswith(prefix) and ".lora_A." in key:
                b_key = key.replace(".lora_A.", ".lora_B.")
                if b_key in lora_sd:
                    return value.float(), lora_sd[b_key].float()
        return None

    ratios: list[float] = []
    for lora_prefix, unet_key in (
        ("context_refiner.0.attention.to_q", "context_refiner.0.attention.qkv.weight"),
        ("context_refiner.0.feed_forward.w1", "context_refiner.0.feed_forward.w1.weight"),
    ):
        pair = pick(lora_prefix)
        if pair is None or unet_key not in unet_sd:
            continue
        a, b = pair
        weight = unet_sd[unet_key].float()
        try:
            delta = b @ a
        except Exception:
            continue
        if lora_prefix.endswith("to_q"):
            # fused qkv 的第一段对应 to_q
            seg = weight.shape[0] // 3
            weight = weight[:seg]
        denom = float(weight.norm().item())
        if denom <= 0:
            continue
        ratios.append(float(delta.norm().item()) / denom)
    return sum(ratios) / len(ratios) if ratios else None


def classify_kind(name: str, info: dict) -> str:
    """判定 LoRA 属于「效果类」还是「加速/蒸馏类」。

    1. 上游目录已登记 kind -> 直接采信（人工核对过）
    2. 否则按文件名 / 说明里的关键词判定
    3. 判不出来 -> 按效果类处理（保守：让用户自己决定，但会给出提示）
    """
    declared = (info.get("kind") or "").strip()
    if declared in (KIND_EFFECT, KIND_ACCELERATION):
        return declared
    blob = " ".join([name, info.get("display", ""), info.get("scene", ""),
                      info.get("source", "")]).lower()
    for marker in ACCELERATION_MARKERS:
        if marker in blob:
            return KIND_ACCELERATION
    return KIND_EFFECT


# ------------------------------------------------------------------ 扫描本机
def _comfyui_root() -> Path | None:
    """定位本机 ComfyUI 根目录（不写死 /root/ComfyUI）。"""
    cfg = Path(os.environ.get("H3STUDIO_CONFIG", "")) if os.environ.get("H3STUDIO_CONFIG") else None
    candidates: list[Path] = []
    if cfg and cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            models_dir = data.get("models_dir")
            if models_dir:
                candidates.append(Path(models_dir))
        except Exception:
            pass
    candidates += [
        Path("/home/ubuntu/comfyui/models"),
        Path("/root/ComfyUI/models"),
    ]
    for base in candidates:
        if (base / "loras").is_dir():
            return base
    return None


def _parse_extra_lora_dirs(raw: str) -> list[Path]:
    """解析 extra_model_paths.yaml，取出所有 section 的 loras 目录。

    文件形态（注意 base_path 缩进在 section 名下面）：
        local_fast:
            base_path: /home/ubuntu/comfyui/models
            loras: loras
        nvme1t_models:
            base_path: /mnt/nvme1t_colorful/comfyui-models/
            loras: loras
    """
    out: list[Path] = []
    base_path: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.split("#")[0].rstrip()
        if not line.strip():
            continue
        indented = line.startswith((" ", "\t"))
        key, sep, val = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        val = val.strip()
        if not indented:
            # 顶层行：section 名 -> 重置 base_path
            base_path = None
            continue
        if key == "base_path":
            base_path = val
        elif key == "loras" and base_path:
            for item in val.split("|"):
                item = item.strip()
                if item:
                    out.append(Path(base_path) / item)
    return out


def comfyui_lora_dirs() -> list[Path]:
    """列出 ComfyUI 实际会扫描的 loras 目录（本地 + extra_model_paths.yaml）。"""
    dirs: list[Path] = []
    root = _comfyui_root()
    if root:
        dirs.append(root / "loras")
    # extra_model_paths.yaml 里声明的额外 loras 目录
    for extra in (Path("/home/ubuntu/comfyui/extra_model_paths.yaml"),
                   Path("/root/ComfyUI/extra_model_paths.yaml")):
        if not extra.is_file():
            continue
        try:
            dirs.extend(_parse_extra_lora_dirs(extra.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 去重 + 只保留存在的
    seen: set[str] = set()
    out: list[Path] = []
    for d in dirs:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            out.append(d)
    return out


def _rel_to_lora_dir(name: str, dirs: list[Path]) -> tuple[str, str]:
    """返回 (ComfyUI 里的 lora_name, 绝对路径)。找不到返回 ("", "")。"""
    for d in dirs:
        candidate = d / name
        if candidate.is_file():
            return name, str(candidate)
    # 支持带子目录的写法（如 z-image/xxx.safetensors）
    for d in dirs:
        for path in d.rglob(name):
            if path.is_file():
                rel = path.relative_to(d).as_posix()
                return rel, str(path)
    return "", ""


def _list_loras_for_base(markers, unet_path, base_label):
    """扫描本机，返回某一底模族下真实存在的 LoRA 清单。

    markers: 文件名里出现即视为该族的 marker
    unet_path: 兼容性比对用的底模 UNET 权重（找不到则跳过校验）
    base_label: 底模名（写进条目的 base 字段）

    ⚠️ **unet_path=None 表示「这一族不做权重比对」**（MOSS-Image 的底模是
    diffusers 目录，没有单文件 UNET 可读）。
    之前这个 None 会一路传到 check_compatibility()，而那里的默认参数是
    `unet_path or _z_image_unet_path()` —— 于是 MOSS 的 LoRA 被拿去和
    **Z-Image 的 UNET** 比键集，结论毫无意义（verified=True 但数据是错的）。
    这里显式分流：None -> 不调用 check_compatibility()。
    """
    dirs = comfyui_lora_dirs()
    found: list[dict] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            candidates = sorted(d.rglob("*.safetensors"))
        except Exception:
            continue
        for path in candidates:
            rel = path.relative_to(d).as_posix()
            lowered = rel.lower()
            if lowered.startswith(lora_damping.DERIVED_PREFIX):
                continue
            if not any(marker in lowered for marker in markers):
                continue
            if rel in seen:
                continue
            seen.add(rel)
            meta = _read_safetensors_metadata(path)
            info = LORA_CATALOG.get(rel) or LORA_CATALOG.get(path.name) or {}
            kind = classify_kind(rel, info)
            compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                      "relative_perturbation": None, "suggested_strength": 1.0,
                      "ctx_perturbation": None, "ctx_damping": None,
                      "verified": False}
            if kind == KIND_EFFECT and unet_path is not None:
                try:
                    compat = check_compatibility(path, unet_path)
                except Exception:
                    compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                              "relative_perturbation": None, "suggested_strength": 1.0,
                              "ctx_perturbation": None, "ctx_damping": None,
                              "verified": False}
            found.append({
                "name": rel,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "display": info.get("display") or path.stem,
                "base": info.get("base") or base_label,
                "trigger": info.get("trigger", ""),
                # 推荐强度：只有**真的做过权重比对**（verified=True）时，
                # 兼容性检测算出的安全值才优先；否则用目录里人工核对的
                # recommended_strength。
                # 为什么：底模是 diffusers 目录的模型（MOSS）没有单文件 UNET
                # 可比对，check_compatibility() 会返回「未验证 + 1.0」，
                # 若无条件采纳就会把上游标注的 0.7 覆盖成 1.0。
                "recommended_strength": (compat.get("suggested_strength")
                                         if compat.get("verified")
                                         and compat.get("suggested_strength")
                                         else info.get("recommended_strength", 1.0)),
                "strength_cap": compat.get("strength_cap", 1.0),
                "ctx_perturbation": compat.get("ctx_perturbation"),
                "ctx_damping": compat.get("ctx_damping"),
                "strength_source": info.get("strength_source", ""),
                "kind": kind,
                "compatible": bool(compat.get("compatible", True)),
                "incompatible_reason": compat.get("reason", ""),
                "attention_hit": compat.get("attention_hit"),
                "relative_perturbation": compat.get("relative_perturbation"),
                "compat_verified": bool(compat.get("verified", False)),
                "scene": info.get("scene", ""),
                "source": info.get("source", ""),
                # 认定方式：filename（文件名含 z-image 字样）或
                # layer_structure（靠层结构比对认出来的）
                "discovered_by": info.get("discovered_by", "filename"),
                "metadata": meta,
            })
    return found


def list_qwen_image_loras() -> list[dict]:
    """本机真实存在的 Qwen-Image 2.1 LoRA 清单。

    与 Z-Image 清单分开返回 —— 两者的底模结构不同，兼容性要各自比对，
    把 2.1 的 LoRA 混进 Z-Image 的下拉会让用户选到不匹配的权重。
    """
    return _list_loras_for_base(QWEN_IMAGE_MARKERS, _qwen_image_unet_path(),
                                "Qwen-Image 2.1")


def list_moss_image_loras() -> list[dict]:
    """本机真实存在的 MOSS-Image LoRA 清单。

    与 Z-Image / Qwen-Image 分开返回 —— MOSS 的底模是 diffusers 目录
    （transformer/ + text_encoder/ + tokenizer/ + vae/），不是单文件 safetensors，
    所以**不能**用 check_compatibility() 那种「读 UNET 权重比键集」的办法
    做兼容性判定（键集要跨 3 个 transformer 分片拼起来，成本高且易错）。

    MOSS 的兼容性靠两点保证：
      1. 键名前缀：MOSS LoRA 的键是 diffusion_model.layers.N.proj.*，
         与本机 diffusers 版 MOSS 底模的层名一一对应（见 moss/_INDEX.md
         的 204/252 patch 实测记录）
      2. 本机已把上游的「融合 qkv」版本转成「分离 q/k/v」版（逐位无损），
         所以能 100% 命中

    因此这里只做「文件名 marker 命中 + 元数据」两步，不做权重比对。
    """
    return _list_loras_for_base(MOSS_IMAGE_MARKERS, None, "MOSS-Image")


def list_moss_effect_loras(include_incompatible: bool = False) -> list[dict]:
    """MOSS-Image 的「效果类」LoRA —— 用户可自由挑选的列表。"""
    items = [item for item in list_moss_image_loras()
             if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_moss_incompatible_loras() -> list[dict]:
    """MOSS-Image 下被过滤掉的不兼容效果 LoRA（界面显示原因用）。"""
    return [item for item in list_moss_image_loras()
            if item["kind"] == KIND_EFFECT and not item.get("compatible", True)]


def is_moss_family_lora(name: str) -> bool:
    """这个 LoRA 文件是不是 MOSS-Image 家族的。

    用途：**挡住跨底模族误用**。底模结构不同，Z-Image 的 LoRA 挂到 MOSS 上
    时键名对不上 —— ComfyUI 只会打一串 "lora key not loaded" 的 WARNING，
    出图照跑但效果 LoRA 根本没生效（静默失效，比报错更难查）。

    判定依据（任一命中即算 MOSS 家族）：
      1. 在 MOSS 清单里（文件名 marker 命中）
      2. 文件实际放在名为 `moss` 的目录下（本机的归档习惯）
    """
    if not name:
        return False
    for item in list_moss_image_loras():
        if item["name"] == name or Path(item["name"]).name == Path(name).name:
            return True
    # 没进清单的，看它是不是放在 moss/ 目录下
    resolved, path = resolve_lora(name)
    if path is None:
        return False
    for part in path.parts:
        if part.lower() == "moss":
            return True
    return False


def list_z_image_loras() -> list[dict]:
    """扫描本机，返回真实存在的 Z-Image LoRA 清单。"""
    dirs = comfyui_lora_dirs()
    # 清掉阻尼系数变更后遗留的旧派生文件（避免 ComfyUI 下拉里出现多份副本）
    try:
        lora_damping.cleanup_stale_derived(dirs)
    except Exception:
        pass
    found: list[dict] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            candidates = sorted(d.rglob("*.safetensors"))
        except Exception:
            continue
        for path in candidates:
            rel = path.relative_to(d).as_posix()
            lowered = rel.lower()
            # 服务端自己生成的 context_refiner 阻尼派生文件不是用户可选的
            # LoRA（它只是原 LoRA 的工作流内部副本），不能出现在列表里。
            # 两种命名都要排：lora_damping 写的 `h3studio_ctx_*`，
            # 以及旧版本写出的 `__p14_*`（同一条线的早期产物）。
            if (lowered.startswith(lora_damping.DERIVED_PREFIX)
                    or lowered.startswith("__p14_")):
                continue
            name_hit = any(marker in lowered for marker in Z_IMAGE_MARKERS)
            if rel in seen:
                continue
            seen.add(rel)
            meta = _read_safetensors_metadata(path)
            info = LORA_CATALOG.get(rel) or LORA_CATALOG.get(path.name) or {}
            kind = classify_kind(rel, info)
            # 【结构兜底】文件名不含 z-image 字样的 LoRA 也可能是 Z-Image 的。
            # 实测：`dejpeg_v3.safetensors` 文件名毫无 z-image 痕迹，
            # 但层结构与 Z-Image 完全对应 —— 实测在 ComfyUI 里 attach 出
            # 180 patches，真实生效；只按文件名 marker 过滤会把它整个漏掉。
            #
            # 判据必须**足够严**，否则 SDXL / Flux / H3 的 LoRA 会全部混进来
            # （实测按「注意力命中率」判会放进 218 个 —— 毫无意义）。
            #
            # ⚠️ 顺序很重要：`_looks_like_z_image_layers()` 只读 safetensors
            # 头部（毫秒级），而 `check_compatibility()` 要读 12GB 的 UNET +
            # LoRA 权重（几十秒）。所以**先用便宜的粗筛**，粗筛过了再做
            # 昂贵的精确检测 —— 否则每次打开面板都要扫 264 个 LoRA 的权重。
            if not name_hit:
                if kind != KIND_EFFECT:
                    continue
                if not _looks_like_z_image_layers(path):
                    continue
            # 兼容性检测：只对效果类做（加速类由工作流固定使用，不进用户列表）
            compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                      "relative_perturbation": None, "suggested_strength": 1.0,
                      "ctx_perturbation": None, "ctx_damping": None,
                      "verified": False}
            if kind == KIND_EFFECT:
                try:
                    compat = check_compatibility(path)
                except Exception:
                    compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                              "relative_perturbation": None, "suggested_strength": 1.0,
                              "ctx_perturbation": None, "ctx_damping": None,
                              "verified": False}
            if not name_hit:
                # 粗筛过了还要精确验证：注意力层必须 100% 命中
                if float(compat.get("attention_hit") or 0) < 1.0:
                    continue
                info = dict(info)
                info.setdefault("display", path.stem)
                info["discovered_by"] = "layer_structure"
            found.append({
                "name": rel,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "display": info.get("display") or path.stem,
                "base": info.get("base") or (meta.get("modelspec.architecture") or meta.get("ss_base_model_version") or "Z-Image"),
                "trigger": info.get("trigger", ""),
                # 推荐强度：上游标注优先；否则按兼容性检测给出的安全值
                "recommended_strength": (compat.get("suggested_strength")
                                         if compat.get("suggested_strength")
                                         else info.get("recommended_strength", 1.0)),
                # 强度上限（1.0 = 不限制）；前端滑块按这个封顶
                "strength_cap": compat.get("strength_cap", 1.0),
                # context_refiner 阻尼信息（界面提示用）
                "ctx_perturbation": compat.get("ctx_perturbation"),
                "ctx_damping": compat.get("ctx_damping"),
                "strength_source": info.get("strength_source", ""),
                "kind": kind,
                "compatible": bool(compat.get("compatible", True)),
                "incompatible_reason": compat.get("reason", ""),
                "attention_hit": compat.get("attention_hit"),
                "relative_perturbation": compat.get("relative_perturbation"),
                "compat_verified": bool(compat.get("verified", False)),
                "scene": info.get("scene", ""),
                "source": info.get("source", ""),
                # 认定方式：filename（文件名含 z-image 字样）或
                # layer_structure（靠层结构比对认出来的）
                "discovered_by": info.get("discovered_by", "filename"),
                "metadata": meta,
            })
    return found


def list_qwen_effect_loras(include_incompatible: bool = False) -> list[dict]:
    """Qwen-Image 2.1 的「效果类」LoRA —— 用户可自由挑选的列表。

    与 Z-Image 同语义：加速/蒸馏类不进（由工作流固定使用），
    不兼容的不进（权重键与 2.1 底模对不上）。
    """
    items = [item for item in list_qwen_image_loras() if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_qwen_incompatible_loras() -> list[dict]:
    """Qwen-Image 2.1 下被过滤掉的不兼容效果 LoRA（界面显示原因用）。"""
    return [item for item in list_qwen_image_loras()
            if item["kind"] == KIND_EFFECT and not item.get("compatible", True)]


def list_effect_loras(include_incompatible: bool = False) -> list[dict]:
    """只返回「效果类」LoRA —— 这才是用户可以自由挑选的列表。

    两条过滤规则：
      1. 加速 / 蒸馏类不进（它们由工作流固定使用，混进来会让用户
         选到与当前基模不匹配的加速器）
      2. **不兼容的不进** —— 兼容性由权重键比对得出（见 check_compatibility）：
         名义上写着 Z-Image、但注意力键名对不上底模的 LoRA，
         加载后会把出图变成彩色噪点，不能交给用户选。
         include_incompatible=True 时会带上（用于界面提示原因）。
    """
    items = [item for item in list_z_image_loras() if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_incompatible_loras() -> list[dict]:
    """被过滤掉的不兼容效果 LoRA（界面用来显示「已排除」及原因）。"""
    return [item for item in list_z_image_loras()
            if item["kind"] == KIND_EFFECT and not item.get("compatible", True)]


def list_acceleration_loras() -> list[dict]:
    """加速 / 蒸馏类 LoRA（工作流固定使用，不进用户下拉）。"""
    return [item for item in list_z_image_loras() if item["kind"] == KIND_ACCELERATION]


def _read_safetensors_metadata(path: Path) -> dict:
    """只读 safetensors 头部元数据（不加载权重）。"""
    try:
        import struct
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(size))
        return header.get("__metadata__") or {}
    except Exception:
        return {}


def resolve_lora(name: str) -> tuple[str, Path | None]:
    """把界面上的 lora_name 解析成 (ComfyUI lora_name, 绝对路径)。

    找不到时返回 ("", None) —— 调用方必须据此报错，不能静默忽略。
    """
    dirs = comfyui_lora_dirs()
    rel, absolute = _rel_to_lora_dir(name, dirs)
    return rel, (Path(absolute) if absolute else None)


# ------------------------------------------------------------------ 持久化
DEFAULT_PREF_KEY = "z_image_loras"
# Qwen-Image 2.1 的 LoRA 配置单独存一份（底模不同，不能混）
QWEN_PREF_KEY = "qwen_image_2.1_loras"
# MOSS-Image 的 LoRA 配置单独存一份（底模不同，不能混）
MOSS_PREF_KEY = "moss_image_loras"
# 界面上的底模族名 -> (配置 key, 清单函数)
LORA_BASES = {
    "Z-Image": (DEFAULT_PREF_KEY, None),
    "Qwen-Image 2.1": (QWEN_PREF_KEY, None),
    "MOSS-Image": (MOSS_PREF_KEY, None),
}


def pref_key_for_base(base: str) -> str:
    """底模族名 -> 配置 key。未知族一律退回 Z-Image 的 key。"""
    return LORA_BASES.get(base or "", (DEFAULT_PREF_KEY, None))[0]


def is_qwen_base(base: str) -> bool:
    return (base or "") in ("Qwen-Image 2.1", "Qwen-Image 2.1 ") or         (base or "").startswith("Qwen-Image 2.1")


def is_moss_base(base: str) -> bool:
    """MOSS-Image 底模族判定（MOSS 的配置与清单都与其它族分开）。"""
    return (base or "") == "MOSS-Image" or (base or "").startswith("MOSS-Image")


def load_config(conn: sqlite3.Connection, key: str = DEFAULT_PREF_KEY) -> list[dict]:
    """读取服务端保存的 LoRA 配置（局域网所有客户端共用这一份）。"""
    try:
        row = conn.execute("SELECT value FROM model_provider_prefs WHERE key=?", (key,)).fetchone()
    except sqlite3.Error:
        return []
    if not row or not row["value"]:
        return []
    try:
        data = json.loads(row["value"])
    except Exception:
        return []
    return data if isinstance(data, list) else []


def save_config(conn: sqlite3.Connection, items: list[dict], key: str = DEFAULT_PREF_KEY) -> list[dict]:
    payload = json.dumps(items, ensure_ascii=False)
    conn.execute(
        "INSERT INTO model_provider_prefs (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, payload),
    )
    return items
