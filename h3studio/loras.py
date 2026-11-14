"""Z-Image 可用 LoRA 清单与服务端持久化配置。

只收录**本机实际存在**的 Z-Image 系列 LoRA —— 清单由 ComfyUI 自己的
folder_paths 扫描得出（跟 /object_info/LoraLoaderModelOnly 同一个来源），
不写死任何可能不存在的文件名。

每个 LoRA 的说明字段来自：
  * safetensors 头部 __metadata__（modelspec / ss_* 字段）
  * 上游仓库 README（ModelScope / Civitai），触发词即上游 tags

**三基座的效果 LoRA 目录**（Z-Image / Qwen-Image 2.1 / MOSS-Image）
在 `lora_catalog_effects.py` 里 —— 那是纯数据（链接 / 强度 / 触发词 /
场景说明原样取自调研清单），与本文件的扫描 / 兼容性逻辑分开，
改目录不用碰逻辑。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

from . import lora_damping
from . import moss_lora_compat
from . import moss_lora_split
from .lora_catalog_effects import ALL_EFFECTS as _EFFECT_CATALOG
from .lora_catalog_effects import FAMILY_PREFIXES as _EFFECT_FAMILY_PREFIXES
from .lora_catalog_ea import EA_ALL_EFFECTS as _EA_CATALOG
from .lora_catalog_nsfw import NSFW_ALL_EFFECTS as _NSFW_CATALOG
from .lora_catalog_mihoyo import ALL_MIHOYO_EFFECTS as _MIHOYO_CATALOG

# ------------------------------------------------------------------ 已知说明
# 说明来源全部标注在 source 字段里，界面上可展开查看。
# trigger 为上游 README tags 中明确标出的触发词；上游未标注的一律留空，
# 不用「猜测」填满。

#: 目录之间的**同名 key 冲突台账**。
#:
#: 2026-11-10 实测：这些目录**不是互斥**的 —— 同一个文件可能同时被
#: 两个目录登记，且 plan_id 不同：
#:     `qwen21-effects/fusal-qwen-image-2.1-v2-exact-r80.safetensors`
#:       * lora_catalog_nsfw   -> QN-08（成人向二次元画风，带触发词与作者标定强度）
#:       * lora_catalog_mihoyo -> Q-28（「风格族邻接替代条目」）
#: dict.update() 是「后者覆盖前者」—— 合并顺序决定哪条生效，
#: **任何顺序都会挂掉其中一侧的 plan_id 断言**。
#:
#: 解法（见 _merge_catalog）：**先合并者优先**，后来的同名条目不覆盖，
#: 但会记进这个台账 —— 冲突被**显式登记**，不是静默丢失。
#: 结构：{key: [(来源目录, plan_id, 保留/被挡), ...]}，第一条是生效的那条。
LORA_CATALOG_CONFLICTS: dict[str, list[dict]] = {}


def _merge_catalog(catalog: dict, source_name: str) -> None:
    """把一个目录合并进 LORA_CATALOG，**不覆盖**已有的同名 key。

    规则：
      1. 同一个 key 只保留**先合并**的那条（先合并者优先）
      2. 被挡下的条目记进 LORA_CATALOG_CONFLICTS，供测试断言冲突已登记
    """
    for key, info in catalog.items():
        if key not in LORA_CATALOG:
            LORA_CATALOG[key] = info
            continue
        existing = LORA_CATALOG[key]
        conflict = LORA_CATALOG_CONFLICTS.setdefault(key, [])
        if not conflict:
            conflict.append({
                "source": "effects",
                "plan_id": existing.get("plan_id"),
                "kept": True,
                "display": existing.get("display"),
            })
        conflict.append({
            "source": source_name,
            "plan_id": info.get("plan_id"),
            "kept": False,
            "display": info.get("display"),
        })


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

# ------------------------------------------------------------------
# 三基座效果 LoRA 目录（来自调研清单，见 lora_catalog_effects.py）
# ------------------------------------------------------------------
# 这些条目统一带**族专属的目录前缀**（z-image-effects/ / qwen21-effects/ /
# moss-effects/），前缀本身就是「这个 LoRA 属于哪一族」的唯一真相源：
#   * 族判定不再靠文件名猜测（`pytorch_lora_weights.safetensors` 这种
#     三族重名的文件，没有前缀根本分不出来）
#   * 跨族误用能在服务端被直接拒绝（键名对不上，ComfyUI 会静默丢弃）
_merge_catalog(_EFFECT_CATALOG, "effects")
# 米哈游（HoYoverse）画风 / 场景效果 LoRA 目录 —— 并列新文件，不动既有目录文件
_merge_catalog(_MIHOYO_CATALOG, "mihoyo")

# 三基座的**东亚人像效果类** LoRA（2026-11-10 本轮接入）。
# 目录在 lora_catalog_ea.py，字段结构与上面那份完全一致，
# 同样靠**族专属目录前缀**（z-image-effects/ qwen21-effects/ moss-effects/）
# 判定族归属 —— 前缀是唯一真相源，不靠文件名猜。
#
# 这批条目里有一部分在清单里标的是「近似适用」（基座未指明 / 无 base_model
# tag），目录里如实保留该标注，界面 scene 字段带 ⚠️ 前缀，
# 不会被误当成「原生 / 已验证」。
_merge_catalog(_EA_CATALOG, "ea")
# 三基座的**成人向 / NSFW 效果类** LoRA（2026-11-10 本轮接入）。
# 目录在 lora_catalog_nsfw.py，字段结构与上面那份完全一致，
# 同样靠**族专属目录前缀**（z-image-effects/ qwen21-effects/ moss-effects/）
# 判定族归属 —— 前缀是唯一真相源，不靠文件名猜。
_merge_catalog(_NSFW_CATALOG, "nsfw")

# 只认这些关键词算「Z-Image 相关」，避免把 285 个 LoRA 全塞进面板
Z_IMAGE_MARKERS = ("zimage", "z-image", "z_image", "kook_zimage")
# Qwen-Image 2.1 家族的 marker（文件名里带这些字样的才可能是 2.1 的 LoRA）
QWEN_IMAGE_MARKERS = ("qwen_image_2.1", "qwen_image2.1", "qwenimage2.1",
                      "qwen_2.1", "qwen21")
# MOSS-Image 家族的 marker（文件名里带这些字样的才可能是 MOSS 的 LoRA）
MOSS_IMAGE_MARKERS = ("moss_image", "moss-image", "mossimage", "moss_hires",
                      "moss_xvstyle")

# 目录里的**族专属前缀** -> 族名。用前缀判定族，比文件名猜测可靠得多。
EFFECT_FAMILY_BY_PREFIX: dict[str, str] = {}
for _fam, _prefixes in _EFFECT_FAMILY_PREFIXES.items():
    for _prefix in _prefixes:
        EFFECT_FAMILY_BY_PREFIX[_prefix] = _fam


def effect_family_of(name: str) -> str | None:
    """这个 LoRA 属于哪一族（按目录前缀判定）。不属于效果目录则返回 None。

    前缀是**唯一**的族判定依据：调研清单里有一批文件三族同名
    （`pytorch_lora_weights.safetensors` 在 Z-Image 是「写实增强」、
    在 QI21 是「Y2K 数码」、在 MOSS 是别的东西），靠文件名分不出来。
    """
    if not name:
        return None
    for prefix, family in EFFECT_FAMILY_BY_PREFIX.items():
        if name.startswith(prefix):
            return family
    return None

# LoRA 分类
#   effect       —— 效果类（滤镜 / 写实风格 / 人像增强）：用户可自行挑选
#   acceleration —— 加速 / 蒸馏类：改变步数、CFG、调度语义，
#                   必须与特定基模搭配，由工作流固定使用，**不作为用户可选项**
#                   （用户自行选择会与 Turbo 自身的蒸馏冲突，出图质量不可控）
KIND_EFFECT = "effect"
KIND_ACCELERATION = "acceleration"

# 加速/蒸馏类 LoRA 的判定关键词 —— 命中即排除出用户可选列表。
# 只对「上游目录里没登记过 kind」的 LoRA 生效（已登记的以登记为准）。
#
# ⚠️ 关键词必须**够具体**（2026-11-09 实测踩坑）：
# 原来有 "turbo_lora"，用来抓 `Z-Image-Fun-Lora-Distill-8-Steps-2603`
# 这类加速 LoRA。但 Z-Image-Turbo 的**效果** LoRA 仓库 URL 恰好长这样：
#   https://huggingface.co/DK9/3D_MMORPG_style_z-image-turbo_lora
#   https://huggingface.co/AlekseyCalvin/Marionette_Modernism_Z-image-Turbo_LoRA
# -> 启发式把这两条**效果** LoRA 判成了加速类，工作流直接拒绝、
#    也不进用户可选列表。
# 现在改成更精确的组合词（"distill" 已经能覆盖绝大多数加速 LoRA），
# 并且效果目录里的条目一律**显式声明** kind=effect，不靠猜。
ACCELERATION_MARKERS = (
    "distill", "distillation", "accel", "speedup", "speed-up",
    "lightx2v", "lightning", "step4", "step8", "4step", "8step",
    "fewstep", "few-step", "lightning2", "sparse", "fast",
    # 加速 LoRA 的命名组合（比裸 "turbo_lora" 精确 —— 效果 LoRA 不会
    # 同时带 "turbo" + "distill"/"step"）
    "turbo_distill", "turbo-distill", "distill_turbo",
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
    """只读 safetensors 头部（不加载权重本体，快）。

    ⚠️ **必须带完整性校验**（2026-11-15 补）：
    safetensors 的布局是「8 字节长度 + JSON 头 + 数据区」，**头部本身完整
    并不代表权重完整**。下载中断 / 拷贝未完成会留下一个「头完好、数据区被
    截断」的文件 —— 旧逻辑「头部能读 = 文件正常」会把它当成好文件。

    实测（2026-11-15）：`ip-adapter-faceid-plus_sd15_lora.safetensors`
    头部声明数据区到 51,059,544 字节，实际文件只有 50,818,772 字节
    （缺 240,772 字节）；`AWPortrait-QW.safetensors` 与
    `Qwen-Image-Edit-Semi-Realistic-Detailer-v1.safetensors` 是 0 字节空文件。
    ComfyUI 加载它们会抛
    `SafetensorError: Error while deserializing header: incomplete metadata,
    file not fully covered` —— 用户选了必然报错。

    所以这里复用 MOSS 侧已经做好的完整性判定（`moss_lora_compat`），
    **不自己再写一份**：两份实现漂移 = 同类缺陷在不同族复发。
    """
    return moss_lora_compat.read_safetensors_header(path)


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
    """从 header 提取 LoRA 想 patch 的层名集合（去掉 lora_A/lora_B 后缀）。

    除了标准 LoRA（lora_A/lora_B、lora_up/lora_down），
    还要认 **LoKR**（lokr_w1/lokr_w2/alpha）和 **LoHa**（hada_w1/hada_w2）
    —— ComfyUI 的 weight_adapter 支持这两种格式
    （comfy/weight_adapter/lokr.py / loha.py），它们同样是可用的效果 LoRA。
    2026-11-09 实测：`lokr-moss-kodachrome-v1.safetensors` 用的是
    `diffusion_model.layers.N.proj.*.lokr_w1` 这种键名，
    不认的话会被当成「不是 LoRA 权重」而漏掉。
    """
    bases: set[str] = set()
    for key in header:
        if key == "__metadata__":
            continue
        for marker in (".lora_A.default.weight", ".lora_B.default.weight",
                       ".lora_A.weight", ".lora_B.weight",
                       ".lora_down.weight", ".lora_up.weight",
                       # LoKR（lokr_w1 / lokr_w2 / alpha）
                       ".lokr_w1", ".lokr_w2", ".lokr_w1_a", ".lokr_w1_b",
                       ".lokr_w2_a", ".lokr_w2_b",
                       # LoHa（hada_w1 / hada_w2）
                       ".hada_w1_a", ".hada_w1_b", ".hada_w2_a", ".hada_w2_b",
                       ".hada_w1", ".hada_w2"):
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

    ⚠️ **必须先剥掉已有的前缀**（2026-11-09 实测踩坑）：
    LoRA 权重里的层名**本身就可能带 `diffusion_model.` 前缀**
    （实测 MOSS 的 ComfyUI 版 LoRA 全是
     `diffusion_model.layers.N.proj.*`）。
    如果不剥，直接拼前缀会变成
    `diffusion_model.diffusion_model.layers.0.proj.q` —— 永远命中不了，
    于是 MOSS 的 LoRA 被判成 0 命中 -> 假不兼容。
    """
    # 先剥掉 LoRA 侧自带的前缀，得到**裸层名**
    stem = base
    for prefix in ("diffusion_model.", "transformer."):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    if stem.startswith("lora_unet_"):
        stem = stem[len("lora_unet_"):].replace("_", ".")
    out: set[str] = set()
    for prefix in _LORA_KEY_PREFIXES:
        if prefix == "lora_unet_":
            out.add("lora_unet_" + stem.replace(".", "_"))
            continue
        out.add(prefix + stem)
    # 裸层名本身也要试（ComfyUI 登记的 generic key 就是裸名）
    out.add(stem)
    return out


def _layer_hit(base: str, unet_keys: set[str]) -> bool:
    """LoRA 的某个层能否在底模里找到（尝试全部前缀 / 别名写法）。"""
    if base in unet_keys:
        return True
    for cand in _lora_key_variants(base):
        if cand in unet_keys:
            return True
    # 分离投影 to_q/to_k/to_v -> 融合 qkv 的三段（ComfyUI 的正规映射）
    # stem 要用**剥掉前缀**的裸层名，否则
    # `diffusion_model.layers.0.attention.to_q` 会拼出
    # `diffusion_model.diffusion_model.layers.0.attention.qkv`（不存在）。
    stem = base
    for prefix in ("diffusion_model.", "transformer."):
        if stem.startswith(prefix):
            stem = stem[len(prefix):]
            break
    for suffix, fused in ((".attention.to_q", ".attention.qkv"),
                          (".attention.to_k", ".attention.qkv"),
                          (".attention.to_v", ".attention.qkv"),
                          (".attention.to_out.0", ".attention.out")):
        if stem.endswith(suffix):
            head = stem[: -len(suffix)]
            for pre in ("diffusion_model.", ""):
                if pre + head + fused in unet_keys:
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

    注意力层 = 键名里含 '.attention.' 的 LoRA layer（**Z-Image 的写法**）。
    命中 = 该 layer 名能在 UNET 的同名层里找到（或其 fused / 前缀变体）。

    命中判定统一走 `_layer_hit()` —— 它把 LoRA 侧层名的三种前缀写法
    （`diffusion_model.` / `lora_unet_` / 无前缀）与 to_q/to_k/to_v
    -> 融合 qkv 的映射都试一遍。

    ⚠️ **这个函数只对 Z-Image 有效**。三族的层命名完全不同：
        Z-Image   layers.N.attention.to_q
        QI21      transformer_blocks.N.attn.to_q
        MOSS      layers.N.proj.q
    拿它去判 QI21 / MOSS 会得到「0 个注意力层 -> 命中率 1.0」的**假通过**。
    那两族请用 `_family_layer_hit_ratio()`（按族选正则）。
    """
    attn = {b for b in lora_bases if ".attention." in b}
    if not attn:
        return 1.0, 0, 0
    hit = 0
    for base in attn:
        if _layer_hit(base, unet_keys):
            hit += 1
    return (hit / len(attn) if attn else 1.0), hit, len(attn)


# ------------------------------------------------------------------
# 分族的「注意力/投影层」命中判定
# ------------------------------------------------------------------
# 三族的层命名完全不同，用同一套正则去比会得到假通过或假失败：
#
#   族          LoRA 层名                        底模层名
#   Z-Image     layers.N.attention.to_q          layers.N.attention.qkv（融合）
#   QI21        transformer_blocks.N.attn.to_q   transformer_blocks.N.attn.to_q
#   MOSS        layers.N.proj.q                  layers.N.proj.q（分离 q/k/v）
#
# 命中判定仍然复用 `_layer_hit()`：它已经把 LoRA 侧的三种前缀写法
# 与 Z-Image 的 to_q/to_k/to_v -> 融合 qkv 映射都覆盖了；
# QI21 / MOSS 的层名底模里是**同名同形**的（前缀写法试一遍即可命中）。
#
# 关键：**必须按族选正则**，否则 QI21 的 LoRA（`.attn.`）
# 在 Z-Image 的正则下会被判成「没有注意力层 -> 命中率 1.0」而假通过。
_FAMILY_ATTENTION_RE = {
    "Z-Image": re.compile(r"\.attention\.(to_q|to_k|to_v|to_out\.0|qkv)$"),
    "Qwen-Image 2.1": re.compile(r"\.attn\.(to_q|to_k|to_v|to_out\.0)$"),
    "MOSS-Image": re.compile(r"\.proj\.(q|k|v|o)$"),
}


def _family_layer_hit_ratio(lora_bases: set[str], unet_keys: set[str],
                            family: str) -> tuple[float, int, int]:
    """按底模族算「注意力/投影层」的命中率。

    返回 (命中率, 命中数, 总数)。该 LoRA 没有这类层时返回 (1.0, 0, 0)
    —— 表示「无法用命中率判定」，不是「100% 命中」。
    """
    pattern = _FAMILY_ATTENTION_RE.get(family)
    if pattern is None:
        return 1.0, 0, 0
    attn = {b for b in lora_bases if pattern.search(b)}
    if not attn:
        return 1.0, 0, 0
    hit = 0
    for base in attn:
        if _layer_hit(base, unet_keys):
            hit += 1
    return (hit / len(attn)), hit, len(attn)


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


def check_compatibility(path: Path, unet_path: Path | None = None,
                         family: str = "Z-Image",
                         deep: bool = False) -> dict:
    """检测一个 LoRA 与对应底模的兼容性。

    参数:
      path       LoRA 权重路径
      unet_path  底模 UNET 权重路径（None = 不做权重比对）
      family     底模族（"Z-Image" / "Qwen-Image 2.1" / "MOSS-Image"）
      deep       是否做**权重级**分析（相对扰动 + ctx 阻尼判定）。
                 False（默认）= 只做**结构级**判定 —— 只读 safetensors
                 **头部**（几毫秒），不碰权重本体。

    ⚠️ **family 必须传对**（2026-11-09 实测教训）：
    三族的层命名完全不同，用 Z-Image 的正则去判 QI21 的 LoRA
    （层名是 `.attn.` 而不是 `.attention.`）会得到「0 个注意力层」
    -> 命中率 1.0 -> **假通过**（明明不兼容却放行）。

    ⚠️ **deep 默认关闭**（2026-11-10 性能修复）：
    本机 LoRA 目录在 /mnt/usb2t（USB 盘，实测 95MB/s），共 226GB。
    权重级分析要**读** LoRA 与底模的权重张量，195 个 Z-Image LoRA
    实测要 **37 秒**（其中 27.7 秒是 b@a 的矩阵乘，9 秒是读盘），
    而这一切发生在 **asyncio 事件循环里**（同步阻塞整个 HTTP 服务）——
    /api/image-loras 因此要跑 40~150 秒；页面首屏为 3 个基座各请求
    一次就把服务打满，Playwright 用例 page.reload 30s 超时。

    结构兼容性（「会不会挂不上 / 会不会被静默丢弃」）**只需要头部**：
    safetensors 头部里有全部张量名与 shape，判断层是否命中底模
    完全够用。权重级分析（推荐强度 / 自动 ctx 阻尼）改为**按需触发**
    —— 见 `_list_loras_for_base` 的 deep_analysis 参数。

    返回:
      {
        "compatible": bool,
        "reason": str,            # 不兼容原因（给界面显示）
        "attention_hit": float,   # 注意力/投影层命中率
        "relative_perturbation": float | None,  # 相对扰动（deep=True 才算）
        "suggested_strength": float,  # 安全推荐强度
        "deep_analyzed": bool,     # 是否做过权重级分析
      }
    """
    global _UNET_CACHE_KEY
    unet_path = unet_path or _z_image_unet_path()
    if unet_path is None:
        # 找不到 UNET 就无法判定 —— 不阻断，但标注「未验证」
        return {"compatible": True, "reason": "", "attention_hit": 1.0,
                "relative_perturbation": None, "suggested_strength": 1.0,
                "strength_cap": 1.0, "verified": False, "deep_analyzed": False}

    # 缓存命中则直接返回（缓存键含族 + deep 标记，避免跨族串味）
    key = _cache_key(path) + (family, bool(deep))
    unet_key = _cache_key(unet_path)
    if _UNET_CACHE_KEY == unet_key and key in _COMPAT_CACHE:
        return dict(_COMPAT_CACHE[key])

    lora_header = _read_safetensors_header(path)
    bases = _lora_layer_bases(lora_header)
    unet_keys = _unet_layer_keys(unet_path)

    # 按族选正则算命中率（见 _family_layer_hit_ratio 的说明）
    hit_ratio, hit, total = _family_layer_hit_ratio(bases, unet_keys, family)
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
        "deep_analyzed": False,
    }

    # ⚠️ **fail closed：LoRA 权重文件本体读不出 -> 明确不兼容**
    # （2026-11-15 补，与 MOSS 侧的口径对齐）
    #
    # `_read_safetensors_header` 现在带**完整性校验**（头部声明的数据区
    # 必须被文件实际大小完整覆盖）。所以「读不出头部」有两种含义：
    #   1. 文件被截断 / 是 0 字节空文件 -> ComfyUI 加载必然抛
    #      `SafetensorError: incomplete metadata, file not fully covered`
    #   2. 文件根本不是 safetensors
    # 两种都意味着「用户选了必然报错」。
    #
    # 旧逻辑：header 为空 -> bases 为空 -> total=0 -> 跳过判定 ->
    # compatible=True / attention_hit=1.0 -> **照常下发**，用户选中即崩。
    # 这里改成明确不兼容并给出如实原因。
    if not lora_header:
        detail = moss_lora_compat.read_safetensors_header_detail(path)
        result["compatible"] = False
        result["verified"] = True
        result["attention_hit"] = 0.0
        result["reason"] = (
            f"{detail['reason']} "
            "这个 LoRA 无法在本机上使用，已从可选列表中排除。")
        _COMPAT_CACHE[key] = dict(result)
        _UNET_CACHE_KEY = unet_key
        return result

    if total and hit_ratio < COMPAT_MIN_ATTENTION_HIT:
        result["compatible"] = False
        result["reason"] = (
            f"注意力/投影层在 {family} 底模里不存在（仅 {hit}/{total} 命中）："
            "该 LoRA patch 的层与底模结构对不上，ComfyUI 会静默丢弃"
            "（只打 \"lora key not loaded\" 的 WARNING，出图照跑但 LoRA 没生效）。")
        _COMPAT_CACHE[key] = dict(result)
        _UNET_CACHE_KEY = unet_key
        return result

    if not deep:
        # 只要结构判定 —— 不读权重（这是性能修复的关键分支）
        _COMPAT_CACHE[key] = dict(result)
        _UNET_CACHE_KEY = unet_key
        return result

    # ---- 权重级分析（deep=True）----
    result["deep_analyzed"] = True
    # 相对扰动需要读权重（较慢），只在结构兼容时做
    try:
        pert = _relative_perturbation(path, unet_path)
        result["relative_perturbation"] = pert
    except Exception:
        pert = None

    # context_refiner 扰动 —— 这是「出彩色噪点」的决定性指标。
    # ⚠️ context_refiner 是 **Z-Image 独有**的模块（联合处理文本条件 + 潜变量），
    # QI21 / MOSS 底模里没有这个模块 —— 对它们做阻尼没有意义，
    # 而且 _ctx_damping_needed() 里的键名对不上会算出 None。
    if family == "Z-Image":
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


#: 相对扰动要采样的层（LoRA 前缀 -> 底模权重键）
#: 与 2026-11-08 的实测标定一致（见 COMPAT_MIN_ATTENTION_HIT 上方的注释）。
_PERTURBATION_SAMPLES = (
    ("context_refiner.0.attention.to_q", "context_refiner.0.attention.qkv.weight"),
    ("context_refiner.0.feed_forward.w1", "context_refiner.0.feed_forward.w1.weight"),
)

#: 底模张量读取缓存 —— key = (底模路径, mtime_ns, 张量名)
#: 只缓存**采样用到的那几个**张量（不是整个 12GB）。
_PERTURB_TENSOR_CACHE: dict[tuple, object] = {}


def _read_safetensors_tensors(path: Path, names: list[str]) -> dict:
    """只读 safetensors 里**指定的那几个**张量（不整体加载）。

    ⚠️ 2026-11-10 性能修复：旧实现用 `load_file(unet_path)` 把整个
    12GB 的 Z-Image UNET **整体读进内存**，每个 LoRA 调一次 ——
    /api/image-loras 因此要跑 150 秒，而且是在 **asyncio 事件循环里**
    同步做的（阻塞整个 HTTP 服务），页面首屏为 3 个基座各请求一次
    就把服务打满，Playwright 用例 page.reload 30s 超时。

    现在用 safetensors 的 **按名读取**（safe_open + get_tensor）：
    只读采样用到的那 2~4 个张量，耗时从「读 12GB」降到「读几 MB」。
    """
    out: dict = {}
    try:
        from safetensors import safe_open
    except Exception:
        return out
    try:
        with safe_open(str(path), framework="pt", device="cpu") as f:
            available = set(f.keys())
            for name in names:
                if name not in available:
                    continue
                key = (str(path), name)
                cached = _PERTURB_TENSOR_CACHE.get(key)
                if cached is not None:
                    out[name] = cached
                    continue
                tensor = f.get_tensor(name)
                _PERTURB_TENSOR_CACHE[key] = tensor
                out[name] = tensor
    except Exception:
        return out
    return out


def _relative_perturbation(lora_path: Path, unet_path: Path) -> float | None:
    """采样若干层，算 LoRA 有效权重相对原权重的平均扰动比例。

    ⚠️ 2026-11-10：**只读需要的张量**（见 _read_safetensors_tensors 的说明）。
    旧实现整体 load 12GB 底模，是 /api/image-loras 慢到 150 秒的根因。
    """
    try:
        from safetensors import safe_open
    except Exception:
        return None

    # ---- LoRA 侧：找出采样层对应的 lora_A / lora_B ----
    lora_pairs: dict[str, tuple] = {}
    try:
        with safe_open(str(lora_path), framework="pt", device="cpu") as f:
            lora_keys = list(f.keys())
            for lora_prefix, unet_key in _PERTURBATION_SAMPLES:
                for key in lora_keys:
                    if key.startswith(lora_prefix) and ".lora_A." in key:
                        b_key = key.replace(".lora_A.", ".lora_B.")
                        if b_key in lora_keys:
                            lora_pairs[unet_key] = (
                                f.get_tensor(key).float(), f.get_tensor(b_key).float())
                            break
    except Exception:
        return None
    if not lora_pairs:
        return None

    # ---- 底模侧：只读采样用到的张量 ----
    unet_tensors = _read_safetensors_tensors(unet_path, list(lora_pairs.keys()))

    ratios: list[float] = []
    for lora_prefix, unet_key in _PERTURBATION_SAMPLES:
        pair = lora_pairs.get(unet_key)
        weight_tensor = unet_tensors.get(unet_key)
        if pair is None or weight_tensor is None:
            continue
        a, b = pair
        weight = weight_tensor.float()
        # ⚠️ 2026-11-10 性能修复：不物化完整的 b@a 矩阵
        #   （见 lora_damping._frob_sq_of_product 的说明 —— 它是 /api/image-loras
        #   慢到 40~150 秒的真正瓶颈）。
        delta_sq = lora_damping._frob_sq_of_product(a, b)
        if delta_sq is None:
            continue
        if lora_prefix.endswith("to_q"):
            # fused qkv 的第一段对应 to_q
            seg = weight.shape[0] // 3
            weight = weight[:seg]
        denom = float(weight.norm().item())
        if denom <= 0:
            continue
        ratios.append((delta_sq ** 0.5) / denom)
    return sum(ratios) / len(ratios) if ratios else None


def _inherit_catalog_info(rel: str) -> dict:
    """派生 LoRA 文件**继承**原文件的目录条目。

    派生文件（本服务自己产出的归一版）的文件名规则是
    `h3studio_mossnorm__<原文件名>`（见
    `h3studio.moss_lora_split.derived_name`），跟原文件是**同一个 LoRA**，
    但 LORA_CATALOG 只登记了原文件 —— 派生文件查不到条目 ->
    display / scene / trigger / 推荐强度全丢 -> 界面显示空白条目。

    这里按同一规则反查：把目录里的每个登记条目的**文件名**按
    derived_name 展开一遍，看有没有正好等于这个派生文件名。
    （比对文件名而不是全路径 —— 派生产物落在原文件**同目录**下，
    目录前缀保持不变。）
    找不到就返回空 dict（不猜）。
    """
    try:
        from .moss_lora_split import DERIVED_PREFIX, derived_name
    except Exception:
        return {}
    name = rel or ""
    if not Path(name).name.startswith(DERIVED_PREFIX):
        return {}
    for key, entry in LORA_CATALOG.items():
        if not key:
            continue
        try:
            if derived_name(Path(key).name) == Path(name).name:
                return dict(entry)
        except Exception:
            continue
    return {}


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


def _list_loras_for_base(markers, unet_path, base_label, family=None,
                         deep_analysis: bool = False):
    """扫描本机，返回某一底模族下真实存在的 LoRA 清单。

    markers: 文件名里出现即视为该族的 marker
    unet_path: 兼容性比对用的底模 UNET 权重（找不到则跳过校验）
    base_label: 底模名（写进条目的 base 字段）
    family: 底模族名（用于按族选正则做兼容性判定）——
             默认取 base_label。

    ⚠️ **unet_path=None 表示「这一族不做权重比对」**（MOSS-Image 的底模是
    diffusers 目录，没有单文件 UNET 可读）。
    之前这个 None 会一路传到 check_compatibility()，而那里的默认参数是
    `unet_path or _z_image_unet_path()` —— 于是 MOSS 的 LoRA 被拿去和
    **Z-Image 的 UNET** 比键集，结论毫无意义（verified=True 但数据是错的）。
    这里显式分流：None -> 不调用 check_compatibility()。

    收录规则（**族判定优先于文件名猜测**）：
      1. 目录前缀命中族专属前缀（z-image-effects/ / qwen21-effects/ /
         moss-effects/）-> 一定属于该族，即使文件名里没有任何族关键词
         （`pytorch_lora_weights.safetensors` 这种三族同名的文件靠前缀分）
      2. 否则按文件名 marker 命中
    """
    family = family or base_label
    effect_prefixes = _EFFECT_FAMILY_PREFIXES.get(family, ())
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
            # ⚠️ **2026-11-15 修复（派生文件混进用户可选列表）**：
            # 本服务自己生成的派生文件（`h3studio_ctx_*` 阻尼版 /
            # `h3studio_mossnorm__*` MOSS 归一版）是**工作流内部副本**，
            # 不是用户可选的 LoRA —— 它们是原文件的**下游**。
            #
            # ⚠️ **判据必须看文件名，不能看相对路径**：派生文件放在
            # **族专属子目录**里（实测 `moss-effects/h3studio_mossnorm__*.safetensors`，
            # 3 个文件），`rel.startswith(prefix)` 永远不成立
            # -> 派生文件以 compatible=true 原样下发。
            #
            # 实测后果（8190 线上）：同一条 LoRA 在 MOSS 面板下拉里出现
            # **两次**，而且两条的 display 完全相同（都继承原文件的
            # 「Fizgig Look LoRA（HF 原生）」）—— 用户分不清哪条是哪条。
            #
            # 修法：按**文件名**排掉两种派生前缀。用户选原文件即可，
            # 提交工作流时 `workflows.py` 的 `ensure_normalized()` 会自动用归一版。
            if (path.name.startswith(lora_damping.DERIVED_PREFIX)
                    or path.name.startswith(moss_lora_split.DERIVED_PREFIX)):
                continue
            # 族归属：目录前缀优先（唯一真相源），否则退回文件名 marker
            by_prefix = any(rel.startswith(prefix) for prefix in effect_prefixes)
            by_marker = any(marker in lowered for marker in markers)
            if not by_prefix and not by_marker:
                continue
            # 别的族的专属前缀 -> 排除（跨族误用会被 ComfyUI 静默丢弃）
            other_prefix = False
            for other_family, prefixes in _EFFECT_FAMILY_PREFIXES.items():
                if other_family == family:
                    continue
                if any(rel.startswith(prefix) for prefix in prefixes):
                    other_prefix = True
                    break
            if other_prefix:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            meta = _read_safetensors_metadata(path)
            info = LORA_CATALOG.get(rel) or LORA_CATALOG.get(path.name) or {}
            # ⚠️ **2026-11-15 修复（派生文件丢失目录元数据）**：
            # 派生文件（`h3studio_mossnorm__<原名>`）是本服务自己产出的
            # **归一版**，它跟原文件是同一个 LoRA —— 但 LORA_CATALOG 里
            # 只登记了原文件的条目（`moss-effects/Cinematic_...safetensors`），
            # 派生文件的键（`moss-effects/h3studio_mossnorm__Cinematic_...`）
            # 查不到 -> info={} -> display / scene / trigger / 推荐强度全丢
            # -> 界面显示空白条目（实测
            # test_effect_entries_carry_trigger_and_description 报「缺场景说明」）。
            # 修法：查不到时**回落到原文件的条目**（按派生命名规则反解原名）。
            if not info:
                inherited = _inherit_catalog_info(rel)
                if inherited:
                    info = inherited
            kind = classify_kind(rel, info)
            compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                      "relative_perturbation": None, "suggested_strength": 1.0,
                      "ctx_perturbation": None, "ctx_damping": None,
                      "verified": False}
            if kind == KIND_EFFECT and unet_path is not None:
                try:
                    compat = check_compatibility(path, unet_path, family=family,
                                                deep=deep_analysis)
                except Exception:
                    compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                              "relative_perturbation": None, "suggested_strength": 1.0,
                              "ctx_perturbation": None, "ctx_damping": None,
                              "verified": False}
            elif kind == KIND_EFFECT and family == "MOSS-Image":
                # MOSS 没有单文件 UNET 可比，但底模的层键清单在
                # index.json 里 —— 用它做同样的「投影层命中率」判定。
                compat = _check_moss_compatibility(path)
            found.append({
                "name": rel,
                "path": str(path),
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "display": info.get("display") or path.stem,
                "base": info.get("base") or base_label,
                "trigger": info.get("trigger", ""),
                # 推荐强度：**目录里人工核对的 upstream 值优先**。
                # 兼容性检测算出的 suggested_strength 只是「这个权重扰动
                # 大不大」的兜底安全值 —— 它不该覆盖作者明示的推荐值
                # （例：调研清单写「作者明示 0.5」，实测扰动算出来 1.0，
                #  用 1.0 会让作者精心标定的强度失效）。
                # 只有目录**没有**给出推荐强度时，才用检测算出的安全值。
                "recommended_strength": (info.get("recommended_strength")
                                         if info.get("recommended_strength") is not None
                                         else (compat.get("suggested_strength")
                                               if compat.get("verified")
                                               and compat.get("suggested_strength")
                                               else 1.0)),
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
                # MOSS 专用：注意力投影形态（split / fused / n/a）与可否无损转换。
                # 前端据此如实标注「融合 qkv（自动转换）」，不装作原生。
                "qkv_shape": compat.get("qkv_shape", ""),
                "qkv_convertible": bool(compat.get("convertible", False)),
                "scene": info.get("scene", ""),
                "source": info.get("source", ""),
                # 认定方式：filename（文件名含族关键词）/
                # effect_dir（族专属目录前缀，最可靠）/
                # layer_structure（靠层结构比对认出来的）
                "discovered_by": info.get(
                    "discovered_by",
                    "effect_dir" if by_prefix else "filename"),
                "family": family,
                "plan_id": info.get("plan_id", ""),
                # 分组标签（东亚人像目录用）：界面下拉按它分组，
                # 85 条同一个下拉不分组根本没法找。
                "group": info.get("group", ""),
                "base_verdict": info.get("base_verdict", ""),
                "metadata": meta,
            })
    return found


def list_qwen_image_loras(deep_analysis: bool = False) -> list[dict]:
    """本机真实存在的 Qwen-Image 2.1 LoRA 清单。

    与 Z-Image 清单分开返回 —— 两者的底模结构不同，兼容性要各自比对，
    把 2.1 的 LoRA 混进 Z-Image 的下拉会让用户选到不匹配的权重。

    deep_analysis=True 时额外做**权重级**分析（相对扰动 / ctx 阻尼），
    要读权重本体，慢 —— 默认关闭（见 check_compatibility 的说明）。
    """
    return _list_loras_for_base(QWEN_IMAGE_MARKERS, _qwen_image_unet_path(),
                                "Qwen-Image 2.1", family="Qwen-Image 2.1",
                                deep_analysis=deep_analysis)


def list_moss_image_loras(deep_analysis: bool = False) -> list[dict]:
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
    return _list_loras_for_base(MOSS_IMAGE_MARKERS, None, "MOSS-Image",
                                family="MOSS-Image", deep_analysis=deep_analysis)


# MOSS 底模（diffusers 目录）的层键缓存
_MOSS_KEYS_CACHE: set[str] | None = None


def _moss_base_layer_keys() -> set[str] | None:
    """MOSS-Image 底模的层键集合（带 `diffusion_model.` 前缀）。

    实现搬到了 `h3studio.moss_lora_compat` —— 那里同时住着
    「融合 qkv vs 分离 q/k/v」的形态判定与无损拆分规则，
    键集合与形态判定必须用**同一份底模真相**，否则又会各说各话。

    键前缀说明（2026-11-09 实测）：ComfyUI 加载 MOSS 后，
    `model_lora_keys_unet()` 只登记 `diffusion_model.*` 一种写法
    （没有 `transformer.` 别名 —— 实测 909 个 key 里 transformer. 前缀为 0）。
    所以上游 diffusers 原版（`transformer.layers.N.proj.*`）
    直接丢进 models/loras/ 会**静默不加载**，
    必须用带 `diffusion_model.` 前缀的版本。
    """
    return moss_lora_compat.moss_base_layer_keys()


def _check_moss_compatibility(path: Path) -> dict:
    """MOSS-Image 的兼容性判定（逐模块去底模真实键集合里查）。

    ⚠️ 2026-11-10 重写 —— 旧实现是**假兼容**，是「静默失效」的根因：

        旧实现用正则 `\\.proj\\.(q|k|v|o)$` 挑注意力模块。
        对**融合 qkv** 的 LoRA（键名 `layers.N.proj.qkv.*`），
        这个正则**一个都匹配不到** -> 匹配集合为空 ->
        `_family_layer_hit_ratio()` 返回 (1.0, 0, 0)（「无法判定」）
        -> 被当成「100% 兼容」-> 界面照常列出 -> 用户选了以为生效，
        实际 ComfyUI 把 qkv 那部分**静默丢弃**（只打 lora key not loaded）。

    新实现（见 `h3studio.moss_lora_compat.check_moss_compatibility`）：
      1. 先判定形态（split / fused / n/a）
      2. 逐个模块名去底模**真实**键集合里查，命中才叫命中
      3. 融合 qkv 的模块按「可无损转换」判定（转换后必然命中）
      4. 命中率 0 / 有未知缺失 -> 明确判为不兼容并给出原因
    """
    return moss_lora_compat.check_moss_compatibility(path)


def list_moss_effect_loras(include_incompatible: bool = False,
                           deep_analysis: bool = False) -> list[dict]:
    """MOSS-Image 的「效果类」LoRA —— 用户可自由挑选的列表。"""
    items = [item for item in list_moss_image_loras(deep_analysis=deep_analysis)
             if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_moss_incompatible_loras(deep_analysis: bool = False) -> list[dict]:
    """MOSS-Image 下被过滤掉的不兼容效果 LoRA（界面显示原因用）。"""
    return [item for item in list_moss_image_loras(deep_analysis=deep_analysis)
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


def list_z_image_loras(deep_analysis: bool = False) -> list[dict]:
    """扫描本机，返回真实存在的 Z-Image LoRA 清单。

    deep_analysis=True 时额外做**权重级**分析（相对扰动 / ctx 阻尼），
    要读权重本体（实测 195 个 LoRA 要 37 秒）—— 默认关闭，
    见 check_compatibility 的说明。
    """
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
            # 族专属目录前缀（z-image-effects/）-> 一定是 Z-Image 的，
            # 即使文件名里一个 z-image 字样都没有。
            by_prefix = rel.startswith("z-image-effects/")
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
            if not name_hit and not by_prefix:
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
                    compat = check_compatibility(path, family="Z-Image",
                                                deep=deep_analysis)
                except Exception:
                    compat = {"compatible": True, "reason": "", "attention_hit": 1.0,
                              "relative_perturbation": None, "suggested_strength": 1.0,
                              "ctx_perturbation": None, "ctx_damping": None,
                              "verified": False}
            if not name_hit and not by_prefix:
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
                # 推荐强度：**目录里人工核对的 upstream 值优先**；
                # 目录没给才用兼容性检测算出的安全值（理由同 _list_loras_for_base）
                "recommended_strength": (info.get("recommended_strength")
                                         if info.get("recommended_strength") is not None
                                         else (compat.get("suggested_strength")
                                               if compat.get("suggested_strength")
                                               else 1.0)),
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
                # MOSS 专用：注意力投影形态（split / fused / n/a）与可否无损转换。
                # 前端据此如实标注「融合 qkv（自动转换）」，不装作原生。
                "qkv_shape": compat.get("qkv_shape", ""),
                "qkv_convertible": bool(compat.get("convertible", False)),
                "scene": info.get("scene", ""),
                "source": info.get("source", ""),
                # 认定方式：filename（文件名含 z-image 字样）/
                # effect_dir（族专属目录前缀，最可靠）/
                # layer_structure（靠层结构比对认出来的）
                "discovered_by": info.get(
                    "discovered_by",
                    "effect_dir" if by_prefix else "filename"),
                "family": "Z-Image",
                "plan_id": info.get("plan_id", ""),
                # 分组标签（东亚人像目录用）：界面下拉按它分组，
                # 85 条同一个下拉不分组根本没法找。
                "group": info.get("group", ""),
                "base_verdict": info.get("base_verdict", ""),
                "metadata": meta,
            })
    return found


def list_qwen_effect_loras(include_incompatible: bool = False,
                          deep_analysis: bool = False) -> list[dict]:
    """Qwen-Image 2.1 的「效果类」LoRA —— 用户可自由挑选的列表。

    与 Z-Image 同语义：加速/蒸馏类不进（由工作流固定使用），
    不兼容的不进（权重键与 2.1 底模对不上）。
    """
    items = [item for item in list_qwen_image_loras(deep_analysis=deep_analysis)
             if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_qwen_incompatible_loras(deep_analysis: bool = False) -> list[dict]:
    """Qwen-Image 2.1 下被过滤掉的不兼容效果 LoRA（界面显示原因用）。"""
    return [item for item in list_qwen_image_loras(deep_analysis=deep_analysis)
            if item["kind"] == KIND_EFFECT and not item.get("compatible", True)]


def list_effect_loras(include_incompatible: bool = False,
                      deep_analysis: bool = False) -> list[dict]:
    """只返回「效果类」LoRA —— 这才是用户可以自由挑选的列表。

    两条过滤规则：
      1. 加速 / 蒸馏类不进（它们由工作流固定使用，混进来会让用户
         选到与当前基模不匹配的加速器）
      2. **不兼容的不进** —— 兼容性由权重键比对得出（见 check_compatibility）：
         名义上写着 Z-Image、但注意力键名对不上底模的 LoRA，
         加载后会把出图变成彩色噪点，不能交给用户选。
         include_incompatible=True 时会带上（用于界面提示原因）。
    """
    items = [item for item in list_z_image_loras(deep_analysis=deep_analysis)
             if item["kind"] == KIND_EFFECT]
    if include_incompatible:
        return items
    return [item for item in items if item.get("compatible", True)]


def list_incompatible_loras(deep_analysis: bool = False) -> list[dict]:
    """被过滤掉的不兼容效果 LoRA（界面用来显示「已排除」及原因）。"""
    return [item for item in list_z_image_loras(deep_analysis=deep_analysis)
            if item["kind"] == KIND_EFFECT and not item.get("compatible", True)]


def list_acceleration_loras(deep_analysis: bool = False) -> list[dict]:
    """加速 / 蒸馏类 LoRA（工作流固定使用，不进用户下拉）。"""
    return [item for item in list_z_image_loras(deep_analysis=deep_analysis)
            if item["kind"] == KIND_ACCELERATION]


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


def lora_name_for_path(path) -> str:
    """绝对路径 -> ComfyUI 里的 lora_name（相对 loras 根目录的 posix 路径）。

    找不到归属目录时返回 ""（调用方必须据此报错）。
    """
    if path is None:
        return ""
    candidate = Path(path)
    for directory in comfyui_lora_dirs():
        try:
            if candidate.is_file() and candidate.is_relative_to(directory):
                return candidate.relative_to(directory).as_posix()
        except (OSError, ValueError):
            continue
    return ""


# ------------------------------------------------------------------ 持久化
DEFAULT_PREF_KEY = "z_image_loras"
# Qwen-Image 2.1 的 LoRA 配置单独存一份（底模不同，不能混）
QWEN_PREF_KEY = "qwen_image_2.1_loras"
# MOSS-Image 的 LoRA 配置单独存一份（底模不同，不能混）
MOSS_PREF_KEY = "moss_image_loras"
# MiniMax-H3-Image 的 LoRA 配置 key（官方暂未发布该模型的 LoRA，
# 但 key 必须独立 —— 否则会读写 Z-Image 那份配置，串族）。
H3IMAGE_PREF_KEY = "h3_image_loras"
# 界面上的底模族名 -> (配置 key, 清单函数)
LORA_BASES = {
    "Z-Image": (DEFAULT_PREF_KEY, None),
    "Qwen-Image 2.1": (QWEN_PREF_KEY, None),
    "MOSS-Image": (MOSS_PREF_KEY, None),
    # MiniMax-H3-Image：官方暂未发布该模型的 LoRA ——
    # 但配置 key 仍要**独立**，否则 pref_key_for_base 会退回 Z-Image 的 key，
    # 于是 H3-Image 面板的 LoRA 配置会读写 Z-Image 那份配置（串族）。
    "H3-Image": (H3IMAGE_PREF_KEY, None),
}


def pref_key_for_base(base: str) -> str:
    """底模族名 -> 配置 key。未知族一律退回 Z-Image 的 key。"""
    return LORA_BASES.get(base or "", (DEFAULT_PREF_KEY, None))[0]


def is_qwen_base(base: str) -> bool:
    return (base or "") in ("Qwen-Image 2.1", "Qwen-Image 2.1 ") or         (base or "").startswith("Qwen-Image 2.1")


def is_moss_base(base: str) -> bool:
    """MOSS-Image 底模族判定（MOSS 的配置与清单都与其它族分开）。"""
    return (base or "") == "MOSS-Image" or (base or "").startswith("MOSS-Image")


def is_h3image_base(base: str) -> bool:
    """MiniMax-H3-Image 底模族判定。

    【为什么必须有这个判定】api_image_loras 的分派是
    moss -> qwen -> **else(Z-Image)**。多一族但不加判定，
    `base=H3-Image` 会掉进 else 分支 -> 返回 **Z-Image 族的 LoRA 清单**
    -> 用户在 H3-Image 面板能选到 Z-Image 的 LoRA（底模结构不同，
    挂上去 ComfyUI 会静默丢弃 = 选了等于没选）。
    """
    return (base or "") == "H3-Image" or (base or "").startswith("H3-Image")


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
