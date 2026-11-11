# -*- coding: utf-8 -*-
"""
文生视频（MiniMax-H3）面板的**唯一真相源**：可用模型 / 加速 LoRA / 注意力机制 / 二采。

设计原则与图像面板一致（见 h3studio/workflows.py 的 Z_IMAGE_MODELS）：

  1. **清单来自本机实测**，不臆造。每一个模型 / LoRA 条目都标注了它在本机的
     落盘位置与体积（实测 2026-11-10，ComfyUI 0.37.0 / RTX 4090D 48GB）。
  2. **服务端是权威**：前端只渲染服务端下发的清单，不自己硬编码文件名。
  3. **兼容就绪位**：底模与 LoRA 的层结构能不能对上，用权重头（safetensors
     header）实测比对 —— 对不上的标成「不可用」并给出原因，不让用户选完
     才在跑图时炸掉。

与图像面板的两处**结构差异**（视频侧特有）：

  * 分辨率不再是「544p / 768p」快捷按钮，而是**宽高输入 + 自动吸附**
    （与生图一致）。吸附倍数来自 H3 节点的 schema（step=32）。
  * 加速 LoRA 拆成两层：
      - **固定加速 LoRA**（工作流硬性必需）：始终挂，不出现在可勾选列表；
      - **可勾选的加速 LoRA 组合**：用户可多选，每项带用途与推荐强度。
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

# ------------------------------------------------------------------
# 尺寸约束（实测依据）
# ------------------------------------------------------------------
# MiniMaxH3ImageToVideo 的 schema（实测 /object_info）：
#   width/height = INT(min=32, max=16384, step=32)
# 节点内部 CANVAS_MULTIPLE = 32（nodes_minimax_h3.py:30），
# latent 网格 = width//16, height//16 —— 所以宽高必须是 32 的倍数。
VIDEO_SIZE_MULTIPLE = 32
VIDEO_SIZE_MIN = 32
VIDEO_SIZE_MAX = 16384

# H3 官方工作流模板里的总像素表（实测 video_minimax_h3_t2v.json 的 MarkdownNote）
#   0.2 -> 608x352 / 0.4 -> 864x480 / 0.8 -> 1216x672 / 0.98 -> 1344x768 (16:9)
# 默认取 0.4（864x480）：这是官方表里 H3 Turbo 4 步最常用的档，速度与画质平衡。
VIDEO_MP_TABLE = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98, 1.0, 1.2, 1.5, 1.8, 2.0]
VIDEO_DEFAULT_MP = 0.4

# ------------------------------------------------------------------
# 尺寸预设（面板的「分辨率下拉」）
# ------------------------------------------------------------------
# 【硬约束来源 —— 全部实测，不是拍脑袋】
#
#  1. **原生画布上限**：comfy_extras/nodes_minimax_h3.py 实测
#         CANVAS_MULTIPLE = 32
#         BASE_SHORT_EDGE = 768
#         MAX_PIXELS     = 768 * 1344   (= 1032192 px ≈ 0.984 MP)
#     官方模板 Note 也写着「H3's native canvas is a 768px short edge,
#     capped at 768x1344 pixels, rounded to a multiple of 32」。
#     => **任何超过 768*1344 面积的组合都超出 H3 原生画布**，不能作为预设。
#
#  2. **算法**：与 ComfyUI 自带 ResolutionSelector 完全一致
#     （comfy_extras/nodes_resolution.py 实测）：
#         total = megapixels * 1024 * 1024
#         scale = sqrt(total / (w_ratio * h_ratio))
#         width  = round(w_ratio * scale / multiple) * multiple
#     本仓库 video_dimensions() 是同一套公式，且已验证能**逐格复现**
#     官方模板 Note 里的 16:9 全表（0.2/0.3/…/0.98 九档全中）。
#
#  3. **短边 768**：官方画布的短边基准。16:9 / 21:9 / 9:16 在 0.98MP
#     时短边正好 768；1:1 这类方画布到 0.98MP 会变 1024x1024（超出画布），
#     所以 1:1 的上限档位是 0.8MP（928x928）。
#
# 【预设粒度的取舍】
#   不把 MP 表全量铺开（14 档 x 8 比例 = 112 项，下拉没法用），
#   而是挑**官方表里明确列出的档位**（0.2 / 0.3 / 0.4 / 0.5 / 0.6 /
#   0.7 / 0.8 / 0.9 / 0.98），再按 H3 原生画布裁掉超限的组合 ——
#   每个出现在下拉里的值都是「本机跑得动 + 官方背书」的，不是编出来的。
#
#   额外带一个 1.0（VIDEO_MP_TABLE 里也有）：它对 16:9 / 9:16 / 21:9
#   会算出 1376x768 / 768x1376 / 1568x672 —— 短边没超 768，
#   但**面积**超过了 768*1344 这个原生画布上限，正是「面积规则」该拦的。
#   保留它是为了让面积规则真的起作用（而不是被短边规则顺带覆盖）。
#
#   档位命名用「短边像素」而不是 MP：
#     - 352 / 480 / 608 / 672 / 768 —— 这些是 16:9 下官方表算出的短边
#       （608x352 / 864x480 / 1056x608 / 1216x672 / 1344x768），
#       用户一眼能对上「480p」「768p」的直觉。
#     - 但短边在不同比例下不一样（如 1:1 的 928x928 短边就是 928），
#       所以每条预设都同时带 mp 与 label，label 用「比例 + 实际像素」表述。
VIDEO_SIZE_PRESET_MPS = [0.2, 0.3, 0.4, 0.5, 0.6,
                         0.7, 0.8, 0.9, 0.98, 1.0]

#: H3 原生画布的面积上限（像素）。实测 comfy_extras/nodes_minimax_h3.py:32
VIDEO_MAX_CANVAS_PIXELS = 768 * 1344

# 画面比例（与 ResolutionSelector 的下拉项一一对应，实测 object_info）
VIDEO_ASPECTS = {
    "1:1": (1, 1),
    "2:3": (2, 3),
    "3:2": (3, 2),
    "3:4": (3, 4),
    "4:3": (4, 3),
    "9:16": (9, 16),
    "16:9": (16, 9),
    "21:9": (21, 9),
}

# ------------------------------------------------------------------
# 可用底模（实测：本机 ComfyUI UNETLoader 下拉里的 H3 权重）
# ------------------------------------------------------------------
# 每个条目都注明了：文件是否真在本机、体积、与加速 LoRA 的层结构兼容性
# （由 check_h3_lora_compat() 实测比对得出）。
VIDEO_MODELS: dict[str, dict] = {
    "fl2va_pruned_int8": {
        "label": "MiniMax-H3 FL2VA · 剪枝 int8（默认）",
        "unet": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "size_mb": 20970,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": True,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "剪枝版 int8（20.9 GB），H3 Turbo 4 步的常规选择。"
                "剪枝把 AdaLN 输入压到 8 维，Turbo LoRA 的 AdaLN 部分由"
                "ComfyUI-MiniMax-H3-Turbo 在运行期重新注入。",
        "warn": "权重在外接盘 /mnt/nvme1t_colorful —— 盘未挂载时 ComfyUI 下拉里"
                "根本没有这个名字，提交会被拒绝。",
    },
    "fl2va_int8": {
        "label": "MiniMax-H3 FL2VA · 完整 int8（保真最高）",
        "unet": "minimax_h3_fl2va_int8_convrot.safetensors",
        "size_mb": 34039,
        "location": "/home/ubuntu/comfyui/models/diffusion_models",
        "pruned": False,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "未剪枝 int8（34.0 GB），AdaLN 仍是完整的 2688 维，"
                "Turbo LoRA 的层结构 259/259 全部命中（实测），保真最高。",
        "warn": "体积接近完整版的一半，显存占用比剪枝版高约 12 GB。",
    },
    "fl2va_pruned_w4a8": {
        "label": "MiniMax-H3 FL2VA · 剪枝 w4a8（最省显存）",
        "unet": "minimax_h3_fl2va_pruned_w4a8_mixed.safetensors",
        "size_mb": 12541,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": True,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "剪枝 + 4bit 激活（12.5 GB），显存最省。"
                "注意它把隐藏维折成 2688 —— LightX2V 系加速 LoRA 的 qkv 层"
                "维度对不上（实测 200/208 形状不匹配），只能用 Turbo 家族。",
        "warn": "与 LightX2V / DMAD 系加速 LoRA 不兼容（实测形状不匹配），"
                "只能配 Turbo 家族加速 LoRA。",
    },
    "fl2va_bf16": {
        "label": "MiniMax-H3 FL2VA · BF16（官方原始精度）",
        "unet": "minimax_h3_fl2va_bf16.safetensors",
        "size_mb": 66280,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": False,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "官方原始 BF16 权重（66.3 GB），层结构与 Turbo LoRA 259/259 命中。",
        "warn": "48 GB 显存的卡单靠这个权重放不下（实测 66.3 GB），"
                "只适合多卡或换机器时选。",
    },
    "hybrid_fl2va_ref2va": {
        "label": "MiniMax-H3 混合版 · FL2VA+Ref2VA",
        "unet": "minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors",
        "size_mb": 20970,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": True,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "把文生视频与参考生成合进一个权重的剪枝 int8 版（20.9 GB）。"
                "Turbo LoRA 259 个层里 208 个直接命中、51 个 AdaLN 由运行期注入。",
        "warn": "外接盘权重；参考通道（Ref2VA）在文生视频面板里不暴露。",
    },
}
VIDEO_DEFAULT_MODEL = "fl2va_pruned_int8"

# ------------------------------------------------------------------
# 加速 LoRA
# ------------------------------------------------------------------
# 【固定加速 LoRA】—— 工作流硬性必需，始终接上，不出现在可勾选列表里。
#   ckpt850：H3 Turbo 4 步路径的标配（259 层，AdaLN 2688 维）。
#   v4step600：同族的另一枚 EMA 权重，官方推荐 4 步档。
# 「模型版本」里选的是它（与图像面板「模型版本 = 工作流固定挂什么加速 LoRA」
# 是同一套语义）；下面 VIDEO_ACCEL_LORAS 是**可叠加**的那一批。
VIDEO_ACCEL_LORAS: dict[str, dict] = {
    "ckpt850": {
        "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
        "display": "Turbo 4 步 · ckpt850（工作流固定）",
        "purpose": "H3 Turbo 4 步路径的固定加速 LoRA：把 20 步压到 4 步。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "fixed": True,
        "note": "259 个层全部命中（实测），AdaLN 输入 2688 维，"
                "与未剪枝 int8 / BF16 完全匹配。",
    },
    "v4step600": {
        "name": "minimax_h3_turbo_v4_step600_ema.safetensors",
        "display": "Turbo 4 步 · v4-step600（工作流固定）",
        "purpose": "同族的另一枚 EMA 权重，官方 4 步档的推荐版本。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "fixed": True,
        "note": "259 个层全部命中（实测），与 ckpt850 等价。",
    },
}
VIDEO_FIXED_ACCEL_LORA_DEFAULT = "ckpt850"

# 【可勾选的加速 LoRA 组合】—— 每一项都是「不同效果的加速 LoRA」，
# 可多选叠加。层结构兼容性由 check_h3_lora_compat() 对当前底模实测比对，
# 对不上的标成不可用并说明原因。
VIDEO_ACCEL_LORA_COMBOS: dict[str, dict] = {
    "lightx2v_4step_768p": {
        "name": "h3_lightx2v/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
        "display": "LightX2V 4 步（768p 档）",
        "purpose": "LightX2V 的 4 步蒸馏权重，针对 768p 短边训练，"
                   "在该分辨率下比 Turbo ckpt850 更稳。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "note": "208 个层全部命中（实测，形状也对得上），但不含 AdaLN —— "
                "它假设底模是完整的 BF16/INT8 版本。",
    },
    "lightx2v_4step_sla_768p": {
        "name": "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
        "display": "LightX2V SLA 4 步（768p 档）",
        "purpose": "LightX2V 的 SLA（Segment Level Attention）变体，"
                   "4 步同时保住长时一致性。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "note": "208 个层全部命中（实测）。本机的 "
                "MiniMaxH3LightX2VSLAKJSageComposerT8Advanced 节点默认就指向它。",
    },
    "lightx2v_8step": {
        # 【2026-11-11 实测修正】原来写的是
        #   "h3_lightx2v/minimax_h3_fl2v_turbo_8step_v1.0_bf16.safetensors"
        # 但那个文件**不是 H3 的 LoRA** —— 它的 312 个层名全是
        #   token_refiner.refiner_blocks.* / transformer_blocks.*
        # （Wan/SD 系命名），而 H3 底模里这两个前缀一个都没有：
        #   含 'refiner_blocks' 的底模层: 0
        #   含 'transformer_blocks' 的底模层: 0
        # 实测命中 0/312 —— ComfyUI 会**静默丢弃**，出片照跑但加速没生效。
        # 真正的 H3 8 步权重在 loras 根目录（官方模板 Note 里指向的也是
        # 这个路径）：minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors
        # 实测 208/208 全命中。
        "name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "display": "LightX2V 8 步",
        "purpose": "8 步档蒸馏：比 4 步慢一倍，但细节与运动更稳，"
                   "适合最终成片而不是快速试片。",
        "recommended_strength": 1.0,
        "recommended_steps": 8,
        "note": "208 个层全部命中（实测），训练 alpha=8 / scale=0.0625。"
                "这是官方模板 Note 指向的 8 步权重（根目录那一份）。",
    },
    "dmad_4step_critic": {
        "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
        "display": "DMAD 4 步（critic 版）",
        "purpose": "DMAD 蒸馏 + 判别器 critic：4 步里质量更接近多步采样。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "note": "208 个层全部命中（实测），不含 AdaLN。",
    },
    "speed_slider": {
        "name": "h3/h3_speed_slider_1.1.safetensors",
        "display": "速度滑杆（快/慢运动）",
        "purpose": "概念 LoRA：只 patch token_refiner 的 fc2，"
                   "可把画面运动整体调快或调慢（负向词 'slow motion.'）。",
        "recommended_strength": 1.0,
        "recommended_steps": 0,
        "note": "体积极小（9 MB），只动 1 个层（实测），与其它加速 LoRA 不冲突。",
    },
    "better_motion": {
        "name": "h3/h3_better_motion_720x1280_w0.4-0.8_s15-30_vpakarinen_v1.safetensors",
        "display": "更好运动（720x1280 档 / 15-30 步）",
        "purpose": "针对 720x1280 竖屏、15~30 步训练的运动增强 LoRA，"
                   "改善大动作与镜头运动。",
        "recommended_strength": 0.8,
        "recommended_steps": 20,
        "note": "208 个层全部命中（实测）。它训练时的步数区间是 15-30，"
                "配 4 步蒸馏档属于超出训练分布的用法。",
    },
}

# ------------------------------------------------------------------
# 注意力机制（实测本机 object_info 里存在的节点）
# ------------------------------------------------------------------
VIDEO_ATTENTION_MODES: dict[str, dict] = {
    "default": {
        "label": "默认（PyTorch 稠密注意力）",
        "node": None,
        "inputs": {},
        "note": "不做任何 patch。速度基准，最稳。",
    },
    "kitchen": {
        "label": "Comfy Kitchen INT8 量化注意力",
        "node": "ModelAttentionBackend",
        "inputs": {"attention": "comfy kitchen attention"},
        "note": "ComfyUI 内建的 INT8 量化稠密注意力，只支持 Nvidia / AMD。"
                "省显存、略掉精度，速度提升有限但最稳。",
    },
    "sage": {
        "label": "SageAttention 显存优化",
        "node": "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "inputs": {},
        "note": "KJNodes 的 SageAttention patch，覆盖 H3 自注意力，"
                "降峰值显存。需要较新的 sageattention（本机已装）。",
    },
    "sol": {
        "label": "Sol-Attn 近似路由",
        "node": "MiniMaxH3MemoryEfficientSolAttentionPatch",
        "inputs": {
            "enabled": True,
            "tau": 1.3,
            "min_tokens": 4096,
            "strict": False,
            "thresh_type": "diag",
            "int8_qk": False,
            "int8_pv": False,
            "sink_conditioning": "exact_kv",
            "dense_blocks": "",
        },
        "note": "按阈值把部分 block 走近似路径：tau 越高越快、保真越低"
                "（论文默认 1.0，本机调优值 1.3）。文本/条件 KV 仍走精确路径。",
    },
    "sol_scheduled": {
        "label": "Sol-Attn 渐进调度（首尾密度不同）",
        "node": "MiniMaxH3ScheduledSolAttentionPatch",
        "inputs": {
            "enabled": True,
            "tau_start": 1.3,
            "tau_end": 0.8,
            "curve": "linear",
            "min_tokens": 4096,
            "strict": False,
            "dense_percent": 0.0,
            "thresh_type": "diag",
            "int8_qk": False,
            "int8_pv": False,
            "sink_conditioning": "exact_kv",
            "dense_blocks": "",
        },
        "note": "Sol-Attn 的调度版：高噪步 tau 1.3（更近似）、低噪步 tau 0.8"
                "（更稠密，细节成形更准）。比固定 tau 更稳。",
    },
    "sparse": {
        "label": "稀疏视频注意力（预算制）",
        "node": "H3SparseAttention",
        "inputs": {"video_budget": 0.15, "denser_early_late_steps": True},
        "note": "只保留一部分视频注意力连接：video_budget 越低越快，"
                "但提示词贴合度与运动细节会掉。没有「安全值」，"
                "每条提示词要自己试。",
    },
}
VIDEO_DEFAULT_ATTENTION = "default"

# ------------------------------------------------------------------
# 二采（SelfLift 渐进式两段采样）
# ------------------------------------------------------------------
# 本机节点：SelfLiftH3Sampler（custom_nodes/comfyui-SelfLift）。
# 机制：低分辨率跑 transition_step 步 -> 一次性提升到目标分辨率 -> 高分辨率跑完剩余步。
# NFE 总数不变，提速来自低分辨率段的注意力面积变小。
#
# 硬约束（实测自 comfyui-SelfLift/nodes.py 的 _validate_sampling / _validate_schedule）：
#   * 只支持标准 Euler 采样器（s_churn=0）—— Turbo 的 4 步采样器不能用；
#   * transition_step 必须落在 [1, len(sigmas)-2]，且 sigmas[transition_step] < 1；
#   * rho=0 且 upscaler_model=none 会被节点直接拒绝（两条路都关掉了）。
VIDEO_TWOPASS = {
    "enabled_default": False,
    "transition_step_default": 3,
    "lowres_scale_default": 0.5,
    "lowres_scale_range": [0.25, 1.0],
    "rho_default": 0.0,
    "w_min_default": 0.5,
    "w_max_default": 1.0,
    "cfg_default": 1.0,
    # 外部 latent 放大器（models/latent_upscale_models，实测本机有 4 个 H3 相关）
    "upscalers": [
        "minimax_h3_latent_upscaler_3d_bf16.safetensors",
        "minimax_h3_latent_upscaler_3d_fp16.safetensors",
        "h3_upscaler_lms_v0.1_fp32.safetensors",
    ],
    "upscaler_default": "minimax_h3_latent_upscaler_3d_bf16.safetensors",
    "note": "二采 = 低分辨率一采 + 高分辨率二采（SelfLift）。"
            "总步数不变，提速来自一采的面积更小。"
            "H3 在 768p 短边下 lowres_scale=0.5 会掉到 384px，"
            "可能超出训练分布 —— 出糊就把它调大。",
}


# ------------------------------------------------------------------
# 采样参数（步数 / 采样器 / 调度器 / shift）
# ------------------------------------------------------------------
# 【2026-11-11 新增】文生视频面板补齐采样参数时引入。
#
# 取值**不是拍脑袋定的**，每一项都有本机实测依据：
#
#   * VIDEO_SAMPLERS / VIDEO_SCHEDULERS 来自 ComfyUI 核心节点
#     `KSamplerSelect.sampler_name`（68 项）与
#     `BasicScheduler.scheduler`（14 项）的 COMBO 清单
#     （/object_info 实测，2026-11-11）。
#     这两个节点**本来就在** H3 工作流里（h3studio/workflows.py
#     的 _video_sampler_block()），只是前端没有控件、也没传值。
#
#   * VIDEO_SHIFT_DEFAULT_VIDEO / _AUDIO 来自 **模型内生值**，不是随意取值：
#       - comfy/supported_models.py:964  class MiniMaxH3
#             sampling_settings = {"shift": 12.0, "audio_shift": 3.0}
#       - comfy/ldm/minimax/model.py:492  MiniMaxH3Model.__init__(
#             sigma_shift_video=12.0, sigma_shift_audio=3.0)
#       - ComfyUI-MiniMax-H3-Turbo/__init__.py:31  SHIFT_V, SHIFT_A = 12.0, 3.0
#     所以用默认 12.0 / 3.0 调 `MiniMaxH3SigmaShift` 与「不加这个节点」
#     **完全等价**（节点写的是同一组 model_sampling + transformer_options）。
#
#   * CFG：H3 标准采样路径**没有** CFG 概念 ——
#     `BasicGuider`（comfy_extras/nodes_custom_sampler.py:801）
#     只有 model + conditioning 两个输入，没有 cfg 槽位；
#     官方蓝图用的也是 BasicGuider 而不是 CFGGuider；
#     comfy/ldm/minimax/model.py 与 comfy_extras/nodes_minimax_h3.py
#     全程没有 negative / uncond / cfg 字样。
#     → 标准路径 CFG 固定 1.0，面板只做**只读说明**，不做假控件。
#     → 例外：二采 `SelfLiftH3Sampler` 有 cfg 槽位（default 5.0），
#       那条路径的 CFG 是真可调的（见 VIDEO_TWOPASS["cfg_default"]）。
VIDEO_SAMPLERS: list[str] = [
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp",
    "heun", "heunpp2", "exp_heun_2_x0", "exp_heun_2_x0_sde",
    "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast", "dpm_adaptive",
    "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde", "dpmpp_sde_gpu",
    "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu",
    "dpmpp_3m_sde", "dpmpp_3m_sde_gpu",
    "ddpm", "lcm", "ipndm", "ipndm_v", "deis",
    "cfgpp_ud10_ab", "res_multistep", "res_multistep_cfg_pp",
    "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp",
    "gradient_estimation", "gradient_estimation_cfg_pp",
    "er_sde", "seeds_2", "seeds_3", "sa_solver", "sa_solver_pece",
    "ddim", "uni_pc", "uni_pc_bh2", "legacy_rk", "rk", "rk_beta",
    "deis_3m_ode", "deis_2m_ode", "deis_3m", "deis_2m",
    "res_6s_ode", "res_5s_ode", "res_3s_ode", "res_2s_ode",
    "res_3m_ode", "res_2m_ode", "res_6s", "res_5s", "res_3s", "res_2s",
    "res_3m", "res_2m", "euler_extsig", "euler_extsig_ancestral",
    "euler_extsig_ancestral_cfg_pp", "euler_extsig_cfg_pp",
]

VIDEO_SCHEDULERS: list[str] = [
    "simple", "sgm_uniform", "karras", "exponential", "ddim_uniform",
    "beta", "normal", "linear_quadratic", "kl_optimal",
    "FlowMatchEulerDiscreteScheduler", "zipf_linear", "zeta",
    "bong_tangent", "beta57",
]

#: 步数范围（面板滑杆）。上界对齐 BasicScheduler 的 int(1~10000)，
#: 但实际给到 50 就已经远超任何 H3 场景（Turbo 4~8 步，官方蓝图 4/6/20）。
VIDEO_STEPS_MIN = 1
VIDEO_STEPS_MAX = 50
VIDEO_STEPS_DEFAULT = 4

#: shift（MiniMaxH3SigmaShift 的取值范围，节点 schema: 0.01~100, step 0.01）
VIDEO_SHIFT_MIN = 0.01
VIDEO_SHIFT_MAX = 100.0
VIDEO_SHIFT_DEFAULT_VIDEO = 12.0
VIDEO_SHIFT_DEFAULT_AUDIO = 3.0

#: 标准采样路径的 CFG —— H3 固定 1.0（BasicGuider 无 cfg 槽位）。
#: 面板用这个值做**只读说明**，不是可调控件。
VIDEO_CFG_FIXED = 1.0
VIDEO_CFG_FIXED_REASON = (
    "H3 标准采样路径固定 CFG=1.0：官方工作流用的是 BasicGuider"
    "（只有 model + conditioning，没有 cfg 槽位），"
    "H3 的音视频潜变量是单流条件化，没有负面分支。"
    "只有开启二采时 CFG 才可调（SelfLiftH3Sampler 自带 cfg 槽位）。"
)


def normalize_sampler(name: str | None, default: str = "euler") -> str:
    """采样器名合法性 —— 不在清单里就用默认值（不报错，避免用户被卡住）。"""
    value = (name or "").strip()
    return value if value in VIDEO_SAMPLERS else default


def normalize_scheduler(name: str | None, default: str = "beta") -> str:
    """调度器名合法性 —— 同上。"""
    value = (name or "").strip()
    return value if value in VIDEO_SCHEDULERS else default


def normalize_steps(value, default: int = VIDEO_STEPS_DEFAULT) -> int:
    """步数：整数，落在 [VIDEO_STEPS_MIN, VIDEO_STEPS_MAX]。"""
    try:
        steps = int(value)
    except (TypeError, ValueError):
        return default
    return max(VIDEO_STEPS_MIN, min(VIDEO_STEPS_MAX, steps))


def normalize_shift(value, default: float) -> float:
    """shift 值：浮点，落在 [0.01, 100.0]。"""
    try:
        shift = float(value)
    except (TypeError, ValueError):
        return default
    if shift != shift:  # NaN
        return default
    return max(VIDEO_SHIFT_MIN, min(VIDEO_SHIFT_MAX, shift))


# ------------------------------------------------------------------
# 尺寸吸附
# ------------------------------------------------------------------
def normalize_video_size(width, height,
                          multiple: int = VIDEO_SIZE_MULTIPLE,
                          max_pixels: float | None = None):
    """把用户手填的宽高吸附到 H3 能接受的值。

    返回 (生效宽, 生效高, 调整原因列表)。**不抛异常** —— 界面要把
    「输入值 → 实际使用值 → 原因」三样一起显示。

    吸附规则（实测依据）：
      1. 宽高必须是 32 的倍数
         （MiniMaxH3ImageToVideo 的 schema step=32；节点内
          CANVAS_MULTIPLE=32；latent 网格 = width//16）
      2. 单边 [32, 16384]（节点 schema min/max）
      3. 总像素超过 max_pixels 时按比例缩（可选）
    """
    reasons: list[str] = []
    try:
        w = int(round(float(width)))
        h = int(round(float(height)))
    except (TypeError, ValueError):
        return 0, 0, ["宽高必须是数字"]

    if w <= 0 or h <= 0:
        return 0, 0, ["宽高必须大于 0"]

    adj_w = max(multiple, int(round(w / multiple)) * multiple)
    adj_h = max(multiple, int(round(h / multiple)) * multiple)
    if adj_w != w:
        # 低于下限时优先说「低于下限」—— 用户填 4 时，
        # 说「不是 32 的倍数」虽然也对，但没告诉他最小能填多少。
        if w < VIDEO_SIZE_MIN:
            reasons.append(f"宽度 {w} 低于下限 {VIDEO_SIZE_MIN} → 已抬到 {VIDEO_SIZE_MIN}")
        else:
            reasons.append(f"宽度 {w} 不是 {multiple} 的倍数 → {adj_w}")
    if adj_h != h:
        if h < VIDEO_SIZE_MIN:
            reasons.append(f"高度 {h} 低于下限 {VIDEO_SIZE_MIN} → 已抬到 {VIDEO_SIZE_MIN}")
        else:
            reasons.append(f"高度 {h} 不是 {multiple} 的倍数 → {adj_h}")
    w, h = adj_w, adj_h

    if w > VIDEO_SIZE_MAX:
        reasons.append(f"宽度超过上限 {VIDEO_SIZE_MAX} → 已钳到上限")
        w = VIDEO_SIZE_MAX
    if h > VIDEO_SIZE_MAX:
        reasons.append(f"高度超过上限 {VIDEO_SIZE_MAX} → 已钳到上限")
        h = VIDEO_SIZE_MAX
    if w < VIDEO_SIZE_MIN:
        reasons.append(f"宽度低于下限 {VIDEO_SIZE_MIN} → 已抬到下限")
        w = VIDEO_SIZE_MIN
    if h < VIDEO_SIZE_MIN:
        reasons.append(f"高度低于下限 {VIDEO_SIZE_MIN} → 已抬到下限")
        h = VIDEO_SIZE_MIN

    if max_pixels:
        limit = max_pixels * 1024 * 1024
        if w * h > limit:
            scale = (limit / float(w * h)) ** 0.5
            nw = max(multiple, int(round(w * scale / multiple)) * multiple)
            nh = max(multiple, int(round(h * scale / multiple)) * multiple)
            reasons.append(
                f"总像素 {w * h / (1024 * 1024):.2f}MP 超过上限 {max_pixels}MP "
                f"→ 按比例缩到 {nw}×{nh}")
            w, h = nw, nh
    return w, h, reasons


def video_dimensions(aspect: str, megapixels: float):
    """比例 + 总像素 -> 像素宽高。

    算法与 ComfyUI 自带的 ResolutionSelector 完全一致（实测其源码
    comfy_extras/nodes_resolution.py）：
        total = megapixels * 1024 * 1024
        scale = sqrt(total / (w_ratio * h_ratio))
        width  = round(w_ratio * scale / multiple) * multiple
        height = round(hr * scale / multiple) * multiple
    multiple 用 32（H3 的 CANVAS_MULTIPLE）。
    """
    import math
    w_ratio, h_ratio = VIDEO_ASPECTS.get(aspect, (16, 9))
    mult = VIDEO_SIZE_MULTIPLE
    total = float(megapixels) * 1024 * 1024
    scale = math.sqrt(total / (w_ratio * h_ratio))
    w = max(mult, int(round(w_ratio * scale / mult)) * mult)
    h = max(mult, int(round(h_ratio * scale / mult)) * mult)
    return w, h


def size_presets_for_aspect(aspect: str) -> list[dict]:
    """某个比例下的**全部合法尺寸预设**（面板下拉的唯一真相源）。

    每条预设都是「官方 MP 档位 x 该比例」算出来的像素值，且必须
    满足 H3 原生画布的硬约束：
      * 宽高都是 32 的倍数（CANVAS_MULTIPLE）
      * 面积 <= 768*1344（MAX_PIXELS，原生画布上限）
      * 短边 <= 768（BASE_SHORT_EDGE）

    超限的组合**直接不出现**在下拉里 —— 不是列出来再置灰，
    因为那是「用户能选、选了必然被服务端改写」的假选项。

    返回的每条：
        key        "<aspect>_<mp>"，稳定标识（存配置用）
        label      "480p · 864×480"（短边像素 + 实际宽高）
        aspect     比例 key
        megapixels 总像素档位
        width/height 实际像素
        short_edge 短边像素（label 里的 "480p" 就是它）
    """
    out: list[dict] = []
    for mp in VIDEO_SIZE_PRESET_MPS:
        w, h = video_dimensions(aspect, mp)
        if w * h > VIDEO_MAX_CANVAS_PIXELS:
            continue                      # 超出原生画布面积上限
        if min(w, h) > 768:
            continue                      # 短边超过 768 基准
        short = min(w, h)
        out.append({
            "key": f"{aspect}_{mp}",
            "label": f"{short}p · {w}×{h}",
            "aspect": aspect,
            "megapixels": mp,
            "width": w,
            "height": h,
            "short_edge": short,
        })
    return out


def all_size_presets() -> list[dict]:
    """所有比例的尺寸预设（扁平清单）。

    面板按当前比例过滤出子集 —— 服务端一次下发全部，
    前端切比例时不用再发请求（与「切模型才重拉兼容性」不同：
    尺寸预算是纯算术，没有磁盘 IO）。
    """
    out: list[dict] = []
    for aspect in VIDEO_ASPECTS:
        out.extend(size_presets_for_aspect(aspect))
    return out


# ------------------------------------------------------------------
# 本机权重探测（模型 / LoRA 是否真在盘上）
# ------------------------------------------------------------------
def _model_dirs() -> list[Path]:
    """本机 ComfyUI 的 diffusion_models 目录（与 extra_model_paths.yaml 同源）。"""
    dirs: list[Path] = []
    for base in (Path("/home/ubuntu/comfyui/models"),
                 Path("/root/ComfyUI/models")):
        dirs.append(base)
    for extra in (Path("/home/ubuntu/comfyui/extra_model_paths.yaml"),
                  Path("/root/ComfyUI/extra_model_paths.yaml")):
        if not extra.is_file():
            continue
        try:
            raw = extra.read_text(encoding="utf-8")
        except OSError:
            continue
        base_path = None
        for raw_line in raw.splitlines():
            line = raw_line.split("#")[0].rstrip()
            if not line.strip():
                continue
            indented = line.startswith((" ", "\t"))
            key, sep, val = line.strip().partition(":")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if not indented:
                base_path = None
                continue
            if key == "base_path" and val:
                base_path = val
            elif key in ("diffusion_models", "unet") and base_path:
                candidate = Path(base_path)
                if candidate.is_dir():
                    dirs.append(candidate)
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _lora_dirs() -> list[Path]:
    """本机 ComfyUI 的 loras 目录（与 extra_model_paths.yaml 同源）。"""
    dirs: list[Path] = [Path("/home/ubuntu/comfyui/models/loras"),
                        Path("/root/ComfyUI/models/loras")]
    for extra in (Path("/home/ubuntu/comfyui/extra_model_paths.yaml"),
                  Path("/root/ComfyUI/extra_model_paths.yaml")):
        if not extra.is_file():
            continue
        try:
            raw = extra.read_text(encoding="utf-8")
        except OSError:
            continue
        base_path = None
        for raw_line in raw.splitlines():
            line = raw_line.split("#")[0].rstrip()
            if not line.strip():
                continue
            indented = line.startswith((" ", "\t"))
            key, sep, val = line.strip().partition(":")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if not indented:
                base_path = None
                continue
            if key == "base_path" and val:
                base_path = val
            elif key == "loras" and base_path:
                candidate = Path(base_path)
                if candidate.is_dir():
                    dirs.append(candidate)
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def find_weight(name: str, kind: str = "diffusion_models") -> Path | None:
    """按 ComfyUI 的目录约定找一个权重文件（找不到返回 None）。"""
    if not name:
        return None
    roots = _model_dirs() if kind == "diffusion_models" else _lora_dirs()
    for root in roots:
        candidate = root / kind / name if kind == "diffusion_models" \
            else root / name
        if candidate.is_file():
            return candidate
    # lora 允许带子目录（h3_lightx2v/xxx.safetensors）
    if kind != "diffusion_models":
        for root in roots:
            for candidate in root.rglob(name):
                if candidate.is_file():
                    return candidate
    return None


def model_available(model_key: str) -> bool:
    model = VIDEO_MODELS.get(model_key)
    if not model:
        return False
    return find_weight(model["unet"]) is not None


def lora_available(name: str) -> bool:
    return find_weight(name, kind="loras") is not None


# ------------------------------------------------------------------
# 权重结构比对（LoRA 与底模能不能对上）
# ------------------------------------------------------------------
_HEADER_CACHE: dict[tuple, dict] = {}


def _read_header(path: Path) -> dict:
    """只读 safetensors 头部（不加载权重本体，快）。"""
    key = (str(path), path.stat().st_mtime_ns, path.stat().st_size)
    cached = _HEADER_CACHE.get(key)
    if cached is not None:
        return cached
    header: dict = {}
    try:
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(size))
    except Exception:
        header = {}
    _HEADER_CACHE[key] = header
    return header


_LORA_MARKERS = (".lora_A.default.weight", ".lora_B.default.weight",
                 ".lora_A.weight", ".lora_B.weight",
                 ".lora_down.weight", ".lora_up.weight")


def _lora_bases(header: dict) -> dict:
    """LoRA header -> {模块名: {"A": shape, "B": shape}}"""
    out: dict[str, dict] = {}
    for key, value in header.items():
        if key == "__metadata__":
            continue
        for marker in _LORA_MARKERS:
            if key.endswith(marker):
                name = key[: -len(marker)]
                slot = "A" if ".lora_A" in marker or ".lora_down" in marker else "B"
                out.setdefault(name, {})[slot] = value.get("shape")
                break
    return out


def _unet_modules(header: dict) -> dict:
    """UNET header -> {模块名: 权重 shape}"""
    out: dict[str, list] = {}
    for key, value in header.items():
        if key == "__metadata__":
            continue
        for suffix in (".weight_scale", ".weight", ".bias"):
            if key.endswith(suffix):
                out[key[: -len(suffix)]] = value.get("shape")
                break
    return out


def _strip_prefix(name: str) -> str:
    for prefix in ("diffusion_model.", "lora_unet_"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _match_module(base: str, unet_modules: dict) -> str | None:
    """LoRA 侧模块名 -> UNET 侧模块名（处理前缀与 to_q/to_k/to_v -> qkv 融合）。"""
    bare = _strip_prefix(base)
    candidates = [bare, "diffusion_model." + bare]
    if bare.endswith(".to_q"):
        stem = bare[:-4]
        for suffix in (".to_k", ".to_v"):
            candidates.append(stem + suffix)
            candidates.append("diffusion_model." + stem + suffix)
    for candidate in candidates:
        if candidate in unet_modules:
            return candidate
    return None


def check_h3_lora_compat(lora_name: str, unet_name: str) -> dict:
    """实测某个加速 LoRA 与某个 H3 底模的层结构能不能对上。

    返回：
      {
        "compatible": bool,      # 能不能用
        "matched": int,          # 层名+形状都对上的层数
        "runtime_injected": int, # AdaLN 层：底模折叠成曲线，由 Turbo 节点运行期重注入
        "shape_mismatch": int,   # 层名对得上但形状对不上（真不兼容）
        "missing": int,          # 底模里根本没这个层
        "total": int,
        "reason": str,           # 不兼容原因（给界面显示）
      }

    **AdaLN 要单独算**（2026-11-10 实测踩坑）：
    Turbo 系加速 LoRA 的 adaln_proj 层是 2688 维输入，而剪枝底模把它折叠成
    8 维曲线 —— 形状对不上，但**这不是不兼容**：
    ComfyUI-MiniMax-H3-Turbo 的 `MiniMaxH3TurboLoRA` 节点检测到
    `use_adaln_curves` 后，会用 `_inject_adaln_egrid()` 把 AdaLN 更新在
    运行期重新注入（custom_nodes/ComfyUI-MiniMax-H3-Turbo/__init__.py:378）。
    把这些层算成「不兼容」会把剪枝底模整条路封死。
    """
    lora_path = find_weight(lora_name, kind="loras")
    unet_path = find_weight(unet_name)
    empty = {"compatible": False, "matched": 0, "runtime_injected": 0,
             "shape_mismatch": 0, "missing": 0, "total": 0, "reason": ""}
    if lora_path is None:
        empty["reason"] = f"本机找不到 LoRA 文件 {lora_name}"
        return empty
    if unet_path is None:
        empty["reason"] = f"本机找不到底模 {unet_name}"
        return empty

    bases = _lora_bases(_read_header(lora_path))
    if not bases:
        # 有些概念 LoRA 用别的键名（lora_down/lora_up 已覆盖，这里兜底）
        empty["reason"] = "该 LoRA 的权重结构无法解析（未识别到标准 LoRA 键名）"
        return empty
    modules = _unet_modules(_read_header(unet_path))
    matched = injected = mismatch = missing = 0
    for base, shapes in bases.items():
        target = _match_module(base, modules)
        if target is None:
            missing += 1
            continue
        weight = modules.get(target) or []
        a_shape = shapes.get("A")
        if (a_shape and len(weight) == 2 and len(a_shape) == 2
                and a_shape[1] != weight[1]):
            # AdaLN 在剪枝底模上是 8 维曲线 -> 由 Turbo 节点运行期重注入
            if "adaln_proj" in target and len(weight) == 2 and weight[1] == 8:
                injected += 1
                continue
            mismatch += 1
            continue
        matched += 1
    total = len(bases)
    result = {
        "compatible": False,
        "matched": matched,
        "runtime_injected": injected,
        "shape_mismatch": mismatch,
        "missing": missing,
        "total": total,
        "reason": "",
    }
    if total and (matched + injected + mismatch) == total \
            and mismatch == 0 and missing == 0:
        result["compatible"] = True
        return result
    if missing:
        result["reason"] = (f"底模里没有该 LoRA 要 patch 的层（缺 {missing}/{total}）"
                            "—— ComfyUI 会静默丢弃，出片照跑但加速 LoRA 没生效。")
    elif mismatch:
        result["reason"] = (f"{mismatch}/{total} 个层的形状对不上"
                            "（底模把隐藏维折叠过）—— 加载了也不会按预期生效。")
    else:
        result["reason"] = "无法确认该 LoRA 与底模兼容。"
    return result


def accel_lora_compat_matrix(model_key: str) -> dict[str, dict]:
    """某个底模下，所有可勾选加速 LoRA 的**实测**兼容性。

    返回 {lora_key: {compatible, reason, matched, runtime_injected, total}}。

    这是「按主模型过滤加速 LoRA 下拉」的**唯一判据** —— 全部来自
    check_h3_lora_compat() 对本机权重头的逐层实测，不是查表硬编。

    缓存：兼容性只跟「权重文件的字节」有关，不随时间变，
    所以按 (model_key, lora_name, 文件 mtime/size) 缓存，
    切来切去模型不会反复读盘。
    """
    model = VIDEO_MODELS.get(model_key)
    if model is None:
        return {}
    out: dict[str, dict] = {}
    for key, meta in VIDEO_ACCEL_LORA_COMBOS.items():
        check = check_h3_lora_compat(meta["name"], model["unet"])
        out[key] = {
            "compatible": bool(check["compatible"]),
            "reason": check["reason"],
            "matched": check["matched"],
            "runtime_injected": check["runtime_injected"],
            "shape_mismatch": check["shape_mismatch"],
            "missing": check["missing"],
            "total": check["total"],
        }
    return out


# ------------------------------------------------------------------
# 下发给前端的清单
# ------------------------------------------------------------------
def list_models() -> list[dict]:
    out = []
    for key, model in VIDEO_MODELS.items():
        item = dict(model)
        item["key"] = key
        item["available"] = model_available(key)
        out.append(item)
    return out


def list_accel_loras() -> tuple[list[dict], list[dict]]:
    """固定加速 LoRA（不进可勾选列表）+ 可勾选的加速 LoRA 组合。"""
    fixed = []
    for key, meta in VIDEO_ACCEL_LORAS.items():
        item = dict(meta)
        item["key"] = key
        item["available"] = lora_available(meta["name"])
        fixed.append(item)
    combos = []
    for key, meta in VIDEO_ACCEL_LORA_COMBOS.items():
        item = dict(meta)
        item["key"] = key
        item["available"] = lora_available(meta["name"])
        combos.append(item)
    return fixed, combos


def list_attention_modes() -> list[dict]:
    out = []
    for key, meta in VIDEO_ATTENTION_MODES.items():
        item = dict(meta)
        item["key"] = key
        out.append(item)
    return out


def twopass_defaults() -> dict:
    cfg = dict(VIDEO_TWOPASS)
    cfg["upscalers"] = list(VIDEO_TWOPASS["upscalers"])
    return cfg


def options_payload(node_available=None) -> dict:
    """GET /api/video-options 的响应体。

    node_available: 可选的回调 class_type -> bool，用来把「本机 ComfyUI
    里没有这个节点」的注意力选项标成不可用（而不是让用户选完才报错）。
    """
    fixed, combos = list_accel_loras()
    payload = {
        "models": list_models(),
        "default_model": VIDEO_DEFAULT_MODEL,
        "fixed_accel_loras": fixed,
        "accel_lora_combos": combos,
        "default_fixed_accel_lora": VIDEO_FIXED_ACCEL_LORA_DEFAULT,
        "attention_modes": list_attention_modes(),
        "default_attention": VIDEO_DEFAULT_ATTENTION,
        "twopass": twopass_defaults(),
        "aspects": [{"key": k, "ratio": list(v)} for k, v in VIDEO_ASPECTS.items()],
        "size_multiple": VIDEO_SIZE_MULTIPLE,
        "size_min": VIDEO_SIZE_MIN,
        "size_max": VIDEO_SIZE_MAX,
        "mp_table": list(VIDEO_MP_TABLE),
        "default_mp": VIDEO_DEFAULT_MP,
        # 【2026-11-11 新增】尺寸预设下拉的清单 —— 每条都是
        # 「官方 MP 档位 x 比例」算出来的像素值，且已按 H3 原生画布
        # （面积 <= 768*1344、短边 <= 768）裁掉超限组合。
        "size_presets": all_size_presets(),
        # 加速 LoRA 现在是**单选**（一个主模型只能挑一个加速 LoRA）。
        "accel_lora_single_select": True,
        "accel_lora_default": "",
        # 各底模下每个加速 LoRA 的实测兼容性（前端按当前模型过滤下拉）。
        "accel_lora_compat": {
            key: accel_lora_compat_matrix(key) for key in VIDEO_MODELS
        },
        # 【2026-11-11 新增】采样参数清单 —— 步数 / 采样器 / 调度器 / shift。
        # 取值来源见本文件 VIDEO_SAMPLERS 上方的实测依据注释。
        "sampling": {
            "steps_min": VIDEO_STEPS_MIN,
            "steps_max": VIDEO_STEPS_MAX,
            "steps_default": VIDEO_STEPS_DEFAULT,
            "samplers": list(VIDEO_SAMPLERS),
            "schedulers": list(VIDEO_SCHEDULERS),
            "shift_min": VIDEO_SHIFT_MIN,
            "shift_max": VIDEO_SHIFT_MAX,
            "shift_default_video": VIDEO_SHIFT_DEFAULT_VIDEO,
            "shift_default_audio": VIDEO_SHIFT_DEFAULT_AUDIO,
            # CFG：标准路径固定 1.0，**不是**可调控件，前端做只读说明。
            "cfg_fixed": VIDEO_CFG_FIXED,
            "cfg_fixed_reason": VIDEO_CFG_FIXED_REASON,
            # 二采路径的 CFG 是真可调的（SelfLiftH3Sampler 自带 cfg 槽位）。
            "cfg_twopass_default": VIDEO_TWOPASS["cfg_default"],
        },
        # shift 节点名（ComfyUI 核心节点）。是否本机可用见下方
        # node_available 分支 —— 面板按「节点真在」的原则处理，
        # 和注意力机制 / 二采同一套做法。
        "sigma_shift_node": "MiniMaxH3SigmaShift",
    }
    if node_available:
        for item in payload["attention_modes"]:
            node = item.get("node")
            item["available"] = True if not node else bool(node_available(node))
        payload["twopass"]["node_available"] = bool(
            node_available("SelfLiftH3Sampler"))
        # 【2026-11-12】shift 节点本机可用性 —— 前端据此禁用 shift 滑杆，
        # 避免出现「能拖但服务端会丢掉」的假控件。
        payload["sigma_shift_available"] = bool(
            node_available("MiniMaxH3SigmaShift"))
    else:
        # 没传回调（单测直接调用 options_payload()）-> 按「可用」，
        # 因为该节点是 ComfyUI 核心节点，恒在。
        payload["sigma_shift_available"] = True
    return payload
