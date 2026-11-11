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
#   1.0 -> 1376x768 / 1.2 -> 1504x832 / 1.5 -> 1664x928 / 1.8 -> 1824x1024
#   2.0 -> 1920x1088   <-- 官方表里**明确列出的最大档**
# 默认取 0.4（864x480）：这是官方表里 H3 Turbo 4 步最常用的档，速度与画质平衡。
VIDEO_MP_TABLE = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98, 1.0, 1.2, 1.5, 1.8, 2.0]
VIDEO_DEFAULT_MP = 0.4

# ------------------------------------------------------------------
# 尺寸预设（面板的「分辨率下拉」）
# ------------------------------------------------------------------
# 【硬约束来源 —— 全部实测，不是拍脑袋】
#
#  1. **倍数 32**：comfy_extras/nodes_minimax_h3.py 实测
#         CANVAS_MULTIPLE = 32
#     节点 schema 的 width/height 也是 step=32。
#     => 宽高必须吸附到 32 的倍数（官方表标题就写着 "Output (multiple=32)"）。
#
#  2. **算法**：与 ComfyUI 自带 ResolutionSelector 完全一致
#     （comfy_extras/nodes_resolution.py 实测）：
#         total = megapixels * 1024 * 1024
#         scale = sqrt(total / (w_ratio * h_ratio))
#         width  = round(w_ratio * scale / multiple) * multiple
#     本仓库 video_dimensions() 是同一套公式，且已验证能**逐格复现**
#     官方模板 Note 里的 16:9 全表（0.2/0.3/…/2.0 十四档全中）。
#
#  3. **面积上限 2.0M 像素**（2026-11-11 放宽）：
#     官方模板 Note 的表**明确列到 2.0 MP -> 1920x1088**，
#     说明 H3 文生视频链路本身支持这个尺寸。
#     旧版把上限钉在 768*1344 (= 1032192 px ≈ 0.98 MP) 是**错的** ——
#     那个常量来自 comfy_extras/nodes_minimax_h3.py 的
#         BASE_SHORT_EDGE = 768
#         MAX_PIXELS     = 768 * 1344
#     但实测这两个常量**只被 adapt_canvas() 用**，而 adapt_canvas()
#     是给**参考视频**（ref_videos）做尺寸适配用的，
#     **生成路径 MiniMaxH3ImageToVideo 根本不查它们** ——
#     生成路径只查 schema 的 min=32 / max=16384 / step=32。
#     （MiniMaxH3ReferenceToVideo 里也是同样：宽度只喂给
#       _empty_av_latent()，不查 MAX_PIXELS。）
#     所以 768*1344 是「参考视频画布」的上限，被误当成「生成上限」了。
#     现在按需求把生成上限放宽到 2.0M 像素（2000000 px）。
#
# 【预设粒度的取舍】
#   不把 MP 表全量铺开（14 档 x 8 比例 = 112 项，下拉没法用），
#   而是挑**官方表里明确列出的档位**（0.2 / 0.3 / 0.4 / 0.5 / 0.6 /
#   0.7 / 0.8 / 0.9 / 0.98 / 1.0 / 1.2 / 1.5 / 1.8 / 2.0），
#   再按 2.0M 面积上限裁掉超限的组合 ——
#   每个出现在下拉里的值都是「官方背书 + 本机跑得动」的，不是编出来的。
#
#   档位命名用「短边像素」而不是 MP：
#     - 352 / 480 / 608 / 672 / 768 —— 这些是 16:9 下官方表算出的短边
#       （608x352 / 864x480 / 1056x608 / 1216x672 / 1344x768），
#       用户一眼能对上「480p」「768p」的直觉。
#     - 但短边在不同比例下不一样（如 1:1 的 928x928 短边就是 928），
#       所以每条预设都同时带 mp 与 label，label 用「比例 + 实际像素」表述。
VIDEO_SIZE_PRESET_MPS = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98,
                         1.0, 1.2, 1.5, 1.8, 2.0]

#: **文生视频（t2v）生成路径的面积上限**：2.0M 像素。
#:
#: 与 ComfyUI 节点常量 MAX_PIXELS 的关系（务必读懂再改）：
#:   * 节点里 MAX_PIXELS = 768*1344 只约束 adapt_canvas()（参考视频适配），
#:     **不约束生成路径** —— 生成路径只查 schema 的 step=32 / min=32 / max=16384。
#:   * 官方模板 Note 的像素表明确列到 2.0 MP -> 1920x1088。
#:   所以这里的 2000000 是**工作流侧**的真实上限，与节点常量无关。
#:
#: 【为什么是 2*1024*1024 而不是字面的 2000000】
#: 需求写「最大 2.0M 像素（2000000 像素）上限」，同时又要求
#: 「16:9 应能取到 1920x1088」。这两个数对不上：
#:     1920 * 1088 = 2,088,960 px  >  2,000,000 px
#: 也就是说按字面 2000000 拦，**需求点名要能取到的 1920x1088 自己就被拦了**。
#:
#: 本仓库的「MP」语义自始至终是 ComfyUI ResolutionSelector 的语义
#: （1.0 MP ≈ 1024x1024），官方模板 Note 的表头也写着 megapixels。
#: 2.0 MP 在这套语义下 = 2 * 1024 * 1024 = 2,097,152 px，
#: 而 1920x1088 = 2,088,960 px 正好落在里面 ——
#: **官方表里 2.0 MP 那一行算出来的就是 1920x1088**（本仓库
#: video_dimensions() 已逐格复现官方表十四档，见 tests）。
#:
#: 所以上限取 2 * 1024 * 1024 = 2,097,152 px：
#:   * 满足「上限 2.0M 像素」（按本项目与官方一致的 MP 语义）；
#:   * 满足「1920x1088 必可达」（硬需求，且是官方表背书的尺寸）；
#:   * 且不会把 1920x1088 之外更大的尺寸放进来（如 2048x1088 = 2,211,584 px）。
#:
#: 若将来要按字面 2000000 收口，只需改这一个常量，
#: 但那时 1920x1088 必须同步降级为不可达 —— 二者不可兼得。
VIDEO_MAX_CANVAS_PIXELS = 2 * 1024 * 1024

#: 需求里写的字面数值（2,000,000），保留供提示文案与测试引用。
VIDEO_MAX_PIXELS_LITERAL = 2_000_000

#: 保留旧名别名（旧代码 / 旧测试引用它），值已改为 2.0M。
#: 【注意】它**不再等于** ComfyUI 源码里的 MAX_PIXELS —— 那是参考视频画布上限。
VIDEO_MAX_PIXELS = VIDEO_MAX_CANVAS_PIXELS

#: 超限时给用户的可执行提示（需求：说明上限与当前请求值，不静默截断）。
VIDEO_MAX_PIXELS_MESSAGE = (
    "文生视频分辨率上限为 2.0M 像素（{limit} px）。"
    "当前请求 {pixels} px（{width}×{height}），超出上限。"
    "请把宽度或高度调小，或改用更小的画面比例。"
)

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
# 尺寸吸附
# ------------------------------------------------------------------
def normalize_video_size(width, height,
                          multiple: int = VIDEO_SIZE_MULTIPLE,
                          max_pixels: float | None = None,
                          strict_over_limit: bool = False):
    """把用户手填的宽高吸附到 H3 能接受的值。

    返回 (生效宽, 生效高, 调整原因列表)。**不抛异常** —— 界面要把
    「输入值 → 实际使用值 → 原因」三样一起显示。

    吸附规则（实测依据）：
      1. 宽高必须是 32 的倍数
         （MiniMaxH3ImageToVideo 的 schema step=32；节点内
          CANVAS_MULTIPLE=32；latent 网格 = width//16）
      2. 单边 [32, 16384]（节点 schema min/max）
      3. 总像素超过 max_pixels 时按比例缩（可选）

    【strict_over_limit】(2026-11-11 新增)
      False（默认，向后兼容）：超限时按比例缩到上限，并在 reasons 里说明。
      True：**不静默截断** —— 超限时**不缩放**，返回生效宽高 = 0，
            reasons 里给出「上限是多少 + 当前请求是多少 + 怎么改」的可执行提示。
      需求要求「超限时给出明确可执行的错误/提示，不要静默截断」，
      提交链路（normalize_t2v_params）用 True，前端预览用 False。
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
            if strict_over_limit:
                # 不静默截断：原样保留请求值，把可执行的提示交给上层。
                reasons.append(VIDEO_MAX_PIXELS_MESSAGE.format(
                    limit=f"{int(limit):,}",
                    pixels=f"{w * h:,}",
                    width=w, height=h))
                return 0, 0, reasons
            scale = (limit / float(w * h)) ** 0.5
            nw = max(multiple, int(round(w * scale / multiple)) * multiple)
            nh = max(multiple, int(round(h * scale / multiple)) * multiple)
            # 【2026-11-11 修】吸附到 32 的倍数后**可能又超回上限**
            # （例：1920x1088 缩到 0.8MP -> 1216x672 = 817,152 <= 838,860 ✓，
            #  但 1218x676 之类的四舍五入结果会溢出）。
            # 逐次收一档，直到真正落进上限内 —— 不能给用户一个「还是超限」的尺寸。
            while nw * nh > limit and (nw > multiple or nh > multiple):
                if nw >= nh and nw > multiple:
                    nw -= multiple
                elif nh > multiple:
                    nh -= multiple
                else:
                    break
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


def size_presets_for_aspect(aspect: str,
                             max_pixels: int | None = None) -> list[dict]:
    """某个比例下的**全部合法尺寸预设**（面板下拉的唯一真相源）。

    每条预设都是「官方 MP 档位 x 该比例」算出来的像素值，且必须
    满足两条硬约束：
      * 宽高都是 32 的倍数（CANVAS_MULTIPLE，节点 schema step=32）
      * 面积 <= max_pixels（默认 VIDEO_MAX_CANVAS_PIXELS = 2.0M）

    【短边 <= 768 这条规则已经去掉】
    旧版有「短边 <= 768（BASE_SHORT_EDGE）」，那是把 ComfyUI 节点里
    专管**参考视频**适配的常量误当成了生成侧约束。
    官方模板 Note 的表里 2.0 MP 那一行是 1920x1088 —— 短边 1088 > 768，
    官方明确背书，所以短边不能作为生成侧的上限。

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
    cap = VIDEO_MAX_CANVAS_PIXELS if max_pixels is None else int(max_pixels)
    out: list[dict] = []
    for mp in VIDEO_SIZE_PRESET_MPS:
        w, h = video_dimensions(aspect, mp)
        if w * h > cap:
            continue                      # 超出面积上限
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


def all_size_presets(max_pixels: int | None = None) -> list[dict]:
    """所有比例的尺寸预设（扁平清单）。

    面板按当前比例过滤出子集 —— 服务端一次下发全部，
    前端切比例时不用再发请求（与「切模型才重拉兼容性」不同：
    尺寸预算是纯算术，没有磁盘 IO）。
    """
    out: list[dict] = []
    for aspect in VIDEO_ASPECTS:
        out.extend(size_presets_for_aspect(aspect, max_pixels=max_pixels))
    return out


# ------------------------------------------------------------------
# 各生成模式的尺寸策略（**按各自工作流的真实约束分别处理**）
# ------------------------------------------------------------------
# 【需求原文】「其它模型/模式（首尾帧、参考生视频、长片拼接等）若有自己的
#  尺寸约束，按各自工作流实际约束分别处理，不要一刀切套用 2.0M。」
#
# 【各模式的实测约束】
#  * t2v（新面板）：MiniMaxH3ImageToVideo 节点的 width/height 直接吃用户值，
#    节点只查 schema 的 min=32 / max=16384 / step=32。
#    上限是**工作流侧**的策略 -> 本轮放宽到 2.0M（官方模板 Note 背书
#    1920x1088 = 2.0 MP 那一行）。
#
#  * flf / r2v（老面板）：走 ResolutionSelector（aspect_ratio + megapixels
#    + multiple=32），档位只有 RES_MP = {544p: 0.4, 768p: 0.8}，
#    实测最大 0.8 MP -> 1216x672 = 817,152 px。
#    它们**没有**「手填宽高」这条交互，所以 2.0M 对它们没有意义；
#    保持原有档位不变（不误放宽）。
#
#  * longfilm：拼片模式，不产生新画面 —— 尺寸由被拼的视频决定，
#    没有独立尺寸约束。
VIDEO_MODE_SIZE_POLICY: dict[str, dict] = {
    "t2v": {
        "max_pixels": VIDEO_MAX_CANVAS_PIXELS,
        "max_pixels_mp": VIDEO_MAX_CANVAS_PIXELS / (1024 * 1024),
        "multiple": VIDEO_SIZE_MULTIPLE,
        "min": VIDEO_SIZE_MIN,
        "max": VIDEO_SIZE_MAX,
        "custom_size": True,
        "source": "官方模板 Note 的 2.0 MP 行 = 1920x1088（2,088,960 px）",
    },
    "flf": {
        "max_pixels": int(0.8 * 1024 * 1024),
        "max_pixels_mp": 0.8,
        "multiple": VIDEO_SIZE_MULTIPLE,
        "min": VIDEO_SIZE_MIN,
        "max": VIDEO_SIZE_MAX,
        "custom_size": False,
        "source": "ResolutionSelector 档位 768p = 0.8 MP（未放宽）",
    },
    "r2v": {
        "max_pixels": int(0.8 * 1024 * 1024),
        "max_pixels_mp": 0.8,
        "multiple": VIDEO_SIZE_MULTIPLE,
        "min": VIDEO_SIZE_MIN,
        "max": VIDEO_SIZE_MAX,
        "custom_size": False,
        "source": "ResolutionSelector 档位 768p = 0.8 MP（未放宽）",
    },
    "longfilm": {
        "max_pixels": None,
        "max_pixels_mp": None,
        "multiple": VIDEO_SIZE_MULTIPLE,
        "min": VIDEO_SIZE_MIN,
        "max": VIDEO_SIZE_MAX,
        "custom_size": False,
        "source": "拼片模式，尺寸由被拼视频决定",
    },
}


def size_policy_for_mode(mode: str) -> dict:
    """某个生成模式的尺寸策略（未知模式回落到 t2v 的宽松策略）。"""
    policy = VIDEO_MODE_SIZE_POLICY.get((mode or "").strip())
    if not policy:
        policy = VIDEO_MODE_SIZE_POLICY["t2v"]
        result = dict(policy)
        result["source"] = "未知模式，回落到 t2v 策略"
        return result
    return dict(policy)


def max_pixels_for_mode(mode: str) -> float | None:
    """某模式的面积上限（单位：MP）。None = 不限。"""
    policy = VIDEO_MODE_SIZE_POLICY.get((mode or "").strip())
    if not policy:
        return VIDEO_MAX_CANVAS_PIXELS / (1024 * 1024)
    return policy.get("max_pixels_mp")


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
        # 【2026-11-11 放宽】文生视频生成分辨率上限：2.0M 像素。
        # 上限从 768*1344（0.98MP）放宽到 2.0 MP = 2*1024*1024 px，
        # 官方模板 Note 的表明确列到 2.0 MP -> 1920x1088。
        "max_pixels": VIDEO_MAX_CANVAS_PIXELS,
        "max_pixels_mp": VIDEO_MAX_CANVAS_PIXELS / (1024 * 1024),
        # 各模式各自的尺寸策略（前端按当前类别/模式取，不一刀切）。
        "mode_size_policy": {
            mode: size_policy_for_mode(mode)
            for mode in VIDEO_MODE_SIZE_POLICY
        },
        # 【2026-11-11 新增】尺寸预设下拉的清单 —— 每条都是
        # 「官方 MP 档位 x 比例」算出来的像素值，且已按 2.0M 面积上限
        # 裁掉超限组合（短边不再设限，见 size_presets_for_aspect 的说明）。
        "size_presets": all_size_presets(),
        # 加速 LoRA 现在是**单选**（一个主模型只能挑一个加速 LoRA）。
        "accel_lora_single_select": True,
        "accel_lora_default": "",
        # 各底模下每个加速 LoRA 的实测兼容性（前端按当前模型过滤下拉）。
        "accel_lora_compat": {
            key: accel_lora_compat_matrix(key) for key in VIDEO_MODELS
        },
    }
    if node_available:
        for item in payload["attention_modes"]:
            node = item.get("node")
            item["available"] = True if not node else bool(node_available(node))
        payload["twopass"]["node_available"] = bool(
            node_available("SelfLiftH3Sampler"))
    return payload
