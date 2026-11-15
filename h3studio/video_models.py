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

import asyncio
import json
import os
import struct
from pathlib import Path

from . import comfy_models

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

def _format_limit_mp(max_pixels) -> str:
    """把上限像素数格式化成「2.0」「0.8」这样的**一位小数**文案。

    【为什么不能用 f"{max_pixels:g}"]
    `:g` 会在去掉尾随零的同时把整数值的浮点写法也压成整数 ——
    `f"{2.0:g}"` 得到 `"2"`（不是 `"2.0"`），于是错误文案变成
    「文生视频分辨率上限为 2M 像素」而不是「2.0M 像素」，
    与产品既有文案（2.0M / 0.8M）不一致。
    这里统一保留一位小数：2.0 -> "2.0"、0.8 -> "0.8"。
    """
    try:
        value = float(max_pixels)
    except (TypeError, ValueError):
        return str(max_pixels)
    text = f"{value:.1f}"
    # 去掉 0.80 这种多余尾零，但仍保留 "2.0" 这类整数值的小数位
    if text.endswith(".0") and value != int(value):
        text = text[:-2]
    return text

#: 各生成模式的**中文名**（用于错误文案）。
#: 【为什么放在这里】错误文案要与「用户正在做的那个模式」对上 ——
#: 模式名散落在各处时，文案就会说错模式（本次审查实测到）。
VIDEO_MODE_LABELS = {
    "t2v": "文生视频",
    "flf": "首尾帧生视频",
    "r2v": "参考生视频",
    "longfilm": "长片拼接",
}

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
        # 【2026-11-11 改造】原来这里写死了一句
        #   「权重在外接盘 /mnt/nvme1t_colorful —— 盘未挂载时 ComfyUI 下拉里
        #     根本没有这个名字，提交会被拒绝。」
        # 那是个**条件句**（"盘未挂载时"），但因为是写死的常量，它在盘
        # **已经挂载、ComfyUI 早就收录了该权重**的情况下依然整句红字显示，
        # 让用户以为「获取不到模型」（用户 2026-11-11 报的就是这个）。
        #
        # 现在：静态字段只放**与可用性无关**的固有属性；
        # 「能不能选」一律由 ComfyUI 的模型列表 API 现算（见 list_models()
        # 里的 status 字段），盘没挂载时才会显示「外置未挂载，当前不可选」。
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
    # ---- Ref2VA 专用权重（参考生视频用）----
    #
    # 【为什么必须有】参考生视频（r2v）走 Ref2VA 通道 ——
    # FL2VA 权重没有 Ref2VA 那条路径，用它做参考生成等于没有参考。
    # 本机实测 ComfyUI 的 UNETLoader 下拉里有 3 个 ref2va 权重
    # （/models/diffusion_models），之前没登记进清单，
    # 于是参考生视频只能拿 fl2va 权重凑合 —— 能力被悄悄阉割了。
    "ref2va_pruned_int8": {
        "label": "MiniMax-H3 Ref2VA · 剪枝 int8（参考生视频）",
        "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "size_mb": 20970,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": True,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "参考生视频（Ref2VA）专用权重，剪枝 int8（20.9 GB）。"
                "层结构与 FL2VA 同族，Turbo 系加速 LoRA 全部命中。",
        "supports_ref2va": True,
    },
    "ref2va_int8": {
        "label": "MiniMax-H3 Ref2VA · 完整 int8（参考生视频·保真最高）",
        "unet": "minimax_h3_ref2va_int8_convrot.safetensors",
        "size_mb": 34039,
        "location": "/home/ubuntu/comfyui/models/diffusion_models",
        "pruned": False,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "未剪枝 int8（34.0 GB），AdaLN 完整 2688 维，"
                "Turbo 系加速 LoRA 259/259 全部命中。",
        "supports_ref2va": True,
        "warn": "体积接近完整版的一半，显存占用比剪枝版高约 12 GB。",
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
        "supports_ref2va": True,
        "warn": "参考通道（Ref2VA）在文生视频面板里不暴露。",
    },
}
VIDEO_DEFAULT_MODEL = "fl2va_pruned_int8"

# ------------------------------------------------------------------
# 加速 LoRA
# ------------------------------------------------------------------
# 【2026-11-14 重构 —— 加速 LoRA 槽位由前端选择驱动】
#
# 【旧设计的问题（用户实测报出）】
#   1. 工作流里把 Turbo 加速 LoRA「ckpt850」**写死**了：
#      _resolve_accel_chain() 无条件把 fixed_accel_lora（或默认值 ckpt850）
#      塞进链首，前端在下拉里选的那枚是**额外叠加**在它之后的第二枚。
#      用户在前端选了 LightX2V / DMAD，工作流里仍然是「ckpt850 + 选的那枚」。
#   2. 更严重的是**加载节点选错了**：
#      MiniMaxH3TurboLoRA 节点把 LoRA 的模块名原样当模块路径用
#      （key_map = {"<module>": "diffusion_model.<module>.weight"}），
#      所以它**只认裸键名**（`blocks.*` / `token_refiner.*`）。
#      而 LightX2V / DMAD / better_motion 这一族的键名带 `diffusion_model.`
#      前缀 —— 节点会把模块名当成 `diffusion_model.diffusion_model.xxx`，
#      **一个权重都 patch 不上**（实测：416 backbone modules, 0 weights patched）。
#      出片照跑，但那枚 LoRA 根本没生效 —— 而界面完全看不出来。
#   3. 结论：「前端选了加速 LoRA，实际不生效」是**两个 bug 叠加**的结果，
#      光改文案没用，必须同时改「选哪枚」和「用哪个节点加载」。
#
# 【新设计】
#   * 加速 LoRA 变成**一个槽位**（不是固定枚 + 叠加枚）：
#     前端选哪枚，工作流里就**只挂哪枚**；选「不挂」就不挂。
#   * 每枚 LoRA 按自己的**键名形态**选加载节点：
#       - 裸键名（Turbo 系 ckpt850 / v4step600）-> MiniMaxH3TurboLoRA
#         （它额外处理剪枝底模的 AdaLN 运行期重注入，必须用它）
#       - 带 diffusion_model. 前缀（LightX2V / DMAD / better_motion …）
#         -> 核心 LoraLoaderModelOnly
#         （comfy/lora.py 的 model_lora_keys_unet 对 MiniMaxH3 专门注册了
#           `key_map[bare] = "diffusion_model.<bare>.weight"`，
#           所以它**正好**认这一族前缀权重）
#   * 兼容：老的 params 仍可能带 fixed_accel_lora / accel_loras，
#     归一时会换算到新槽位（见 resolve_accel_lora_slot）。
#
# 【为什么不能一律用 LoraLoaderModelOnly】
#   剪枝底模（fl2va_pruned_int8 / hybrid）的 adaln_proj 被折叠成 8 维曲线，
#   Turbo LoRA 的 AdaLN 更新（2688 维）没法做成权重 patch ——
#   MiniMaxH3TurboLoRA 节点会用 _inject_adaln_egrid() 在运行期重注入。
#   核心 LoraLoaderModelOnly 没有这个机制，用它挂 ckpt850 会丢掉 51 个
#   AdaLN 层。所以**按形态分流**是必须的，不是偷懒。
VIDEO_ACCEL_LORAS: dict[str, dict] = {
    "ckpt850": {
        "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
        "display": "Turbo 4 步 · ckpt850",
        "purpose": "H3 Turbo 4 步路径的标配：把 20 步压到 4 步。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "fixed": True,
        "note": "259 个层全部命中（实测），AdaLN 输入 2688 维，"
                "与未剪枝 int8 / BF16 完全匹配。",
    },
    "v4step600": {
        "name": "minimax_h3_turbo_v4_step600_ema.safetensors",
        "display": "Turbo 4 步 · v4-step600",
        "purpose": "同族的另一枚 EMA 权重，官方 4 步档的推荐版本。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "208 个层全部命中（实测），训练 alpha=8 / scale=0.0625。"
                "这是官方模板 Note 指向的 8 步权重（根目录那一份）。",
    },
    "dmad_4step_critic": {
        "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
        "display": "DMAD 4 步（critic 版）",
        "purpose": "DMAD 蒸馏 + 判别器 critic：4 步里质量更接近多步采样。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
    # ---- 【2026-11-15 新增】本机实测存在、但此前未登记的加速 LoRA ----
    #
    # 【为什么加这一批】用户指出「LoRA 与 UNET 都应该可自由定义」，
    # 并给了两个参考工作流：
    #   /home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/
    #     minimax_h3_v1.json  -> UNET=minimax_h3_fl2va_int8_convrot.safetensors
    #                              LORA=minimax_h3_turbo_v4_step600_ema.safetensors
    #     minimax_h3_v2.json  -> UNET=minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors
    #                              LORA=dmad_minimax_h3_4step_full_critic_comfyui.safetensors
    # 两个参考工作流的 LORA 节点都是**核心 LoraLoaderModelOnly**，
    # 而本产品此前把加速 LoRA 固定成 Turbo 节点。
    #
    # 本机实测（2026-11-15，ComfyUI /models/loras 共 735 条）还有这些
    # H3 加速权重此前没进清单，用户无法选：
    #   * minimax_h3_turbo_4步加速_comfyui.safetensors        （中文名 Turbo 4 步）
    #   * minimax_h3_turbo_4步加速ema_comfyui.safetensors      （中文名 Turbo 4 步 EMA）
    #   * minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors
    #   * minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors
    #     （根目录那一份；h3_lightx2v/ 下还有同名的 768p 版本）
    #
    # 【键名形态 —— 决定用哪个加载节点】
    #   中文名两份是 diffusion_model. 前缀 -> 核心 LoraLoaderModelOnly
    #   LightX2V 两份也是 diffusion_model. 前缀 -> 核心 LoraLoaderModelOnly
    # （判据是 safetensors 头部实测，不是文件名硬编 —— 见
    #   detect_accel_lora_loader）
    "turbo_4step_cn": {
        "name": "minimax_h3_turbo_4步加速_comfyui.safetensors",
        "display": "Turbo 4 步（中文名加速版）",
        "purpose": "官方 Turbo 4 步蒸馏权重的**中文名发行版**，"
                   "键名带 diffusion_model. 前缀 —— 与 ckpt850 是同族，"
                   "但必须用核心 LoraLoaderModelOnly 加载。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "518 个层全部命中（实测，键名带 diffusion_model. 前缀）。"
                "剪枝底模的 AdaLN 层（51 个）**不会**被核心节点重注入 —— "
                "核心节点没有 Turbo 节点那套运行期 AdaLN 机制，"
                "所以它配**未剪枝**底模（int8 / bf16）才是完整命中。",
    },
    "turbo_4step_cn_ema": {
        "name": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "display": "Turbo 4 步（中文名 EMA 版）",
        "purpose": "Turbo 4 步的中文名 EMA 权重，"
                   "键名带 diffusion_model. 前缀。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "518 个层全部命中（实测，键名带 diffusion_model. 前缀）。"
                "同 turbo_4step_cn：剪枝底模的 AdaLN 不会被运行期重注入。",
    },
    "lightx2v_4step_v01_comfy": {
        "name": "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
        "display": "LightX2V 4 步（v0.1 comfy 版）",
        "purpose": "LightX2V Turbo 4 步 v0.1 的 comfy 打包版，"
                   "键名带 diffusion_model. 前缀。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "416 个层全部命中（实测）。它不含 AdaLN，"
                "配剪枝底模时 AdaLN 更新不会生效（核心节点无重注入机制）。",
    },
    "lightx2v_sla_768p_root": {
        "name": "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
        "display": "LightX2V SLA 4 步（768p · 根目录版）",
        "purpose": "与 h3_lightx2v/ 目录里那份同名的 SLA 权重"
                   "（键名形态一致），此处登记**根目录**那一份，"
                   "它才是 h3_lightx2v/ 目录版之外真实存在的第二份。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "624 个层全部命中（实测）。它不含 AdaLN。",
    },
}

# ------------------------------------------------------------------
# 注意力机制（实测本机 object_info 里存在的节点）
# ------------------------------------------------------------------
# 【2026-11-12 修 —— 两处按用户实测反馈调整】
#
#  1. **移除「Sol-Attn 渐进调度」（sol_scheduled）**
#     用户实测画面有**叠影**（渐进 tau 调度让高噪步过于近似，
#     运动主体糊成两层）。选项已从清单里**整个删掉** ——
#     不是「列出来再置灰」（置灰等于「能选但会被服务端改写」，
#     是假选项）。服务端清单是唯一真相源，前端只是渲染它。
#
#  2. **加速度标注（speed_rank / speed_label）**
#     用户要求在下拉里直接看出快慢：
#       稀疏视频注意力 = 速度最快
#       sol-attn        = 速度很快
#       comfy kitchen   = 速度一般
#       sageattention   = 较慢
#       pytorch         = 最慢
#     排序 = speed_rank 升序（1 = 最快）。
#
# 【速度标注的依据】
#   * 稀疏注意力（H3SparseAttention）直接砍掉视频注意力连接 ——
#     计算量本身最少，节点自述「Lower video attention budgets are faster」。
#   * sol-attn 走 Triton 近似 kernel（H3 自注意力专用路径），
#     比稠密 PyTorch SDPA 快，但每个 block 仍有 kernel launch 开销。
#   * comfy kitchen 是 INT8 **稠密**量化 —— 计算量与稠密相同，
#     只是把 matmul 换成量化算子，**没有稀疏化**，所以只是「一般」。
#   * sageattention 是**稠密**注意力换 kernel（省显存），计算量不变，
#     且额外有 patch 开销 -> 较慢。
#   * pytorch 稠密 SDPA 是基准 -> 最慢。
#   （用户给定的排序与上述机制事实一致，故直接采用。）
VIDEO_ATTENTION_MODES: dict[str, dict] = {
    "default": {
        "label": "默认（PyTorch 稠密注意力）",
        "node": None,
        "inputs": {},
        "note": "不做任何 patch。速度基准，最稳。",
        "speed_rank": 5,
        "speed_label": "速度最慢",
    },
    "kitchen": {
        "label": "Comfy Kitchen INT8 量化注意力",
        "node": "ModelAttentionBackend",
        "inputs": {"attention": "comfy kitchen attention"},
        "note": "ComfyUI 内建的 INT8 量化稠密注意力，只支持 Nvidia / AMD。"
                "省显存、略掉精度，速度提升有限但最稳。",
        "speed_rank": 3,
        "speed_label": "速度一般",
    },
    "sage": {
        "label": "SageAttention 显存优化",
        "node": "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "inputs": {},
        "note": "KJNodes 的 SageAttention patch，覆盖 H3 自注意力，"
                "降峰值显存。需要较新的 sageattention（本机已装）。",
        "speed_rank": 4,
        "speed_label": "速度较慢",
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
        "speed_rank": 2,
        "speed_label": "速度很快",
    },
    "sparse": {
        "label": "稀疏视频注意力（预算制）",
        "node": "H3SparseAttention",
        "inputs": {"video_budget": 0.15, "denser_early_late_steps": True},
        "note": "只保留一部分视频注意力连接：video_budget 越低越快，"
                "但提示词贴合度与运动细节会掉。没有「安全值」，"
                "每条提示词要自己试。",
        "speed_rank": 1,
        "speed_label": "速度最快",
    },
}

#: **被移除**的注意力机制 key（2026-11-12 用户实测画面有叠影）。
#:
#: 保留这个常量（而不是彻底忘掉）是刻意的：
#:   * 老任务落库的 params 里可能还有 `attention: "sol_scheduled"`；
#:   * 面板配置（/api/video-config）里也可能存着这个 key；
#:   * 这两处都要**明确降级**（回落到 default）而不是报错或静默失败，
#:     否则老任务点「复用参数」会得到一个下拉里根本不存在的值。
#:
#: 节点名也一并登记 —— 服务端归一层遇到这个 key 时明确报错说明，
#: 而不是当成「未知注意力机制」。
VIDEO_REMOVED_ATTENTION_MODES: dict[str, dict] = {
    "sol_scheduled": {
        "label": "Sol-Attn 渐进调度（首尾密度不同）",
        "node": "MiniMaxH3ScheduledSolAttentionPatch",
        "reason": "用户实测该调度会让画面出现叠影（高噪步过于近似，"
                  "运动主体糊成两层），已从选项中移除。",
    },
}
#: 视频时长（秒）区间 —— **2026-11-12 放宽**。
#:
#: 旧值是 [2, 15]（server.normalize_t2v_params 里 `min(15.0, duration)`），
#: 用户实测反馈「15 秒太短，一分钟都行，最多 OOM」。
#:
#: 【为什么放到 60】
#:   * ComfyUI 的 MiniMaxH3ImageToVideo 节点 schema：
#:       length = INT(min=5, max=3600, step=17)
#:     节点本身允许 3600 帧（150 秒）—— 15 秒是**我们自己的**限制，
#:     不是模型/节点的能力上限。
#:   * 帧数表达式 `max(5, round(a*24)) + (5 - (... % 17)) % 17`
#:     在任何秒数下都能算出合法帧数。
#:   * 真正的瓶颈是显存 —— 长时长 = 线性增长的 latent 与注意力序列。
#:     所以上限放宽到 60 秒（1440 帧），超显存时由 ComfyUI 抛 OOM，
#:     **明确报错**（任务失败 + 错误文案），不做静默截断。
#:
#: 【为什么不静默截断】
#: 用户明确要求「如超显存应给出明确报错而非静默截断」。
#: 静默截断会让用户以为自己生成了 60 秒、实际只拿到 15 秒 —— 那是撒谎。
#: 现在超上限**直接拒绝**并说明上限值。
VIDEO_DURATION_MIN = 2.0
VIDEO_DURATION_MAX = 60.0
VIDEO_DURATION_DEFAULT = 5.0

#: 视频时长超上限时的**可执行**提示（不静默截断）。
VIDEO_DURATION_MAX_MESSAGE = (
    "文生视频时长上限为 {limit:g} 秒。当前请求 {value:g} 秒，超出上限。"
    "请把时长调小；显存不足时请改用更小的分辨率或更短的时长。"
)


#: 超限时给用户的可执行提示（需求：说明上限与当前请求值，不静默截断）。
#:
#: 【2026-11-15 代码审查修 —— 文案与实际上限对不上】
#: 原来这里把「2.0M」「文生视频」**写死**在文案里，但这个函数是通用的，
#: 首尾帧 / 参考生视频的实际上限是 **0.8MP**（max_pixels_for_mode("flf")=0.8）。
#: 结果用户做首尾帧时看到的是：
#:   「文生视频分辨率上限为 2.0M 像素（838,860 px）。当前请求 1,032,192 px…」
#: —— 括号里是 838,860（=0.8MP），句首却说 2.0M，还称「文生视频」。
#: 三个信息互相矛盾，用户没法据此判断该调小到多少。
#:
#: 【修法】上限值与模式名都从参数来，由调用方按实际 max_pixels 填。
VIDEO_MAX_PIXELS_MESSAGE = (
    "{mode_label}分辨率上限为 {limit_mp}M 像素（{limit} px）。"
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
        # 【2026-11-11 改造】原来这里写死了一句
        #   「权重在外接盘 /mnt/nvme1t_colorful —— 盘未挂载时 ComfyUI 下拉里
        #     根本没有这个名字，提交会被拒绝。」
        # 那是个**条件句**（"盘未挂载时"），但因为是写死的常量，它在盘
        # **已经挂载、ComfyUI 早就收录了该权重**的情况下依然整句红字显示，
        # 让用户以为「获取不到模型」（用户 2026-11-11 报的就是这个）。
        #
        # 现在：静态字段只放**与可用性无关**的固有属性；
        # 「能不能选」一律由 ComfyUI 的模型列表 API 现算（见 list_models()
        # 里的 status 字段），盘没挂载时才会显示「外置未挂载，当前不可选」。
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
    # ---- Ref2VA 专用权重（参考生视频用）----
    #
    # 【为什么必须有】参考生视频（r2v）走 Ref2VA 通道 ——
    # FL2VA 权重没有 Ref2VA 那条路径，用它做参考生成等于没有参考。
    # 本机实测 ComfyUI 的 UNETLoader 下拉里有 3 个 ref2va 权重
    # （/models/diffusion_models），之前没登记进清单，
    # 于是参考生视频只能拿 fl2va 权重凑合 —— 能力被悄悄阉割了。
    "ref2va_pruned_int8": {
        "label": "MiniMax-H3 Ref2VA · 剪枝 int8（参考生视频）",
        "unet": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "size_mb": 20970,
        "location": "/mnt/nvme1t_colorful/comfyui-models/diffusion_models",
        "pruned": True,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "参考生视频（Ref2VA）专用权重，剪枝 int8（20.9 GB）。"
                "层结构与 FL2VA 同族，Turbo 系加速 LoRA 全部命中。",
        "supports_ref2va": True,
    },
    "ref2va_int8": {
        "label": "MiniMax-H3 Ref2VA · 完整 int8（参考生视频·保真最高）",
        "unet": "minimax_h3_ref2va_int8_convrot.safetensors",
        "size_mb": 34039,
        "location": "/home/ubuntu/comfyui/models/diffusion_models",
        "pruned": False,
        "steps": 4,
        "sampler": "euler",
        "scheduler": "beta",
        "cfg": 1.0,
        "note": "未剪枝 int8（34.0 GB），AdaLN 完整 2688 维，"
                "Turbo 系加速 LoRA 259/259 全部命中。",
        "supports_ref2va": True,
        "warn": "体积接近完整版的一半，显存占用比剪枝版高约 12 GB。",
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
        "supports_ref2va": True,
        "warn": "参考通道（Ref2VA）在文生视频面板里不暴露。",
    },
}
VIDEO_DEFAULT_MODEL = "fl2va_pruned_int8"

# ------------------------------------------------------------------
# 加速 LoRA
# ------------------------------------------------------------------
# 【2026-11-14 重构 —— 加速 LoRA 槽位由前端选择驱动】
#
# 【旧设计的问题（用户实测报出）】
#   1. 工作流里把 Turbo 加速 LoRA「ckpt850」**写死**了：
#      _resolve_accel_chain() 无条件把 fixed_accel_lora（或默认值 ckpt850）
#      塞进链首，前端在下拉里选的那枚是**额外叠加**在它之后的第二枚。
#      用户在前端选了 LightX2V / DMAD，工作流里仍然是「ckpt850 + 选的那枚」。
#   2. 更严重的是**加载节点选错了**：
#      MiniMaxH3TurboLoRA 节点把 LoRA 的模块名原样当模块路径用
#      （key_map = {"<module>": "diffusion_model.<module>.weight"}），
#      所以它**只认裸键名**（`blocks.*` / `token_refiner.*`）。
#      而 LightX2V / DMAD / better_motion 这一族的键名带 `diffusion_model.`
#      前缀 —— 节点会把模块名当成 `diffusion_model.diffusion_model.xxx`，
#      **一个权重都 patch 不上**（实测：416 backbone modules, 0 weights patched）。
#      出片照跑，但那枚 LoRA 根本没生效 —— 而界面完全看不出来。
#   3. 结论：「前端选了加速 LoRA，实际不生效」是**两个 bug 叠加**的结果，
#      光改文案没用，必须同时改「选哪枚」和「用哪个节点加载」。
#
# 【新设计】
#   * 加速 LoRA 变成**一个槽位**（不是固定枚 + 叠加枚）：
#     前端选哪枚，工作流里就**只挂哪枚**；选「不挂」就不挂。
#   * 每枚 LoRA 按自己的**键名形态**选加载节点：
#       - 裸键名（Turbo 系 ckpt850 / v4step600）-> MiniMaxH3TurboLoRA
#         （它额外处理剪枝底模的 AdaLN 运行期重注入，必须用它）
#       - 带 diffusion_model. 前缀（LightX2V / DMAD / better_motion …）
#         -> 核心 LoraLoaderModelOnly
#         （comfy/lora.py 的 model_lora_keys_unet 对 MiniMaxH3 专门注册了
#           `key_map[bare] = "diffusion_model.<bare>.weight"`，
#           所以它**正好**认这一族前缀权重）
#   * 兼容：老的 params 仍可能带 fixed_accel_lora / accel_loras，
#     归一时会换算到新槽位（见 resolve_accel_lora_slot）。
#
# 【为什么不能一律用 LoraLoaderModelOnly】
#   剪枝底模（fl2va_pruned_int8 / hybrid）的 adaln_proj 被折叠成 8 维曲线，
#   Turbo LoRA 的 AdaLN 更新（2688 维）没法做成权重 patch ——
#   MiniMaxH3TurboLoRA 节点会用 _inject_adaln_egrid() 在运行期重注入。
#   核心 LoraLoaderModelOnly 没有这个机制，用它挂 ckpt850 会丢掉 51 个
#   AdaLN 层。所以**按形态分流**是必须的，不是偷懒。
VIDEO_ACCEL_LORAS: dict[str, dict] = {
    "ckpt850": {
        "name": "minimax_h3_turbo_4step_ema_ckpt850.safetensors",
        "display": "Turbo 4 步 · ckpt850",
        "purpose": "H3 Turbo 4 步路径的标配：把 20 步压到 4 步。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "fixed": True,
        "note": "259 个层全部命中（实测），AdaLN 输入 2688 维，"
                "与未剪枝 int8 / BF16 完全匹配。",
    },
    "v4step600": {
        "name": "minimax_h3_turbo_v4_step600_ema.safetensors",
        "display": "Turbo 4 步 · v4-step600",
        "purpose": "同族的另一枚 EMA 权重，官方 4 步档的推荐版本。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "208 个层全部命中（实测），训练 alpha=8 / scale=0.0625。"
                "这是官方模板 Note 指向的 8 步权重（根目录那一份）。",
    },
    "dmad_4step_critic": {
        "name": "dmad_minimax_h3_4step_full_critic_comfyui.safetensors",
        "display": "DMAD 4 步（critic 版）",
        "purpose": "DMAD 蒸馏 + 判别器 critic：4 步里质量更接近多步采样。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
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
    # ---- 【2026-11-15 新增】本机实测存在、但此前未登记的加速 LoRA ----
    #
    # 【为什么加这一批】用户指出「LoRA 与 UNET 都应该可自由定义」，
    # 并给了两个参考工作流：
    #   /home/ubuntu/nonebotqq/plugins/nonebot_plugin_novelai/workflows/
    #     minimax_h3_v1.json  -> UNET=minimax_h3_fl2va_int8_convrot.safetensors
    #                              LORA=minimax_h3_turbo_v4_step600_ema.safetensors
    #     minimax_h3_v2.json  -> UNET=minimax_h3_hybrid_fl2va_ref2va_b30-49-int8.safetensors
    #                              LORA=dmad_minimax_h3_4step_full_critic_comfyui.safetensors
    # 两个参考工作流的 LORA 节点都是**核心 LoraLoaderModelOnly**，
    # 而本产品此前把加速 LoRA 固定成 Turbo 节点。
    #
    # 本机实测（2026-11-15，ComfyUI /models/loras 共 735 条）还有这些
    # H3 加速权重此前没进清单，用户无法选：
    #   * minimax_h3_turbo_4步加速_comfyui.safetensors        （中文名 Turbo 4 步）
    #   * minimax_h3_turbo_4步加速ema_comfyui.safetensors      （中文名 Turbo 4 步 EMA）
    #   * minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors
    #   * minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors
    #     （根目录那一份；h3_lightx2v/ 下还有同名的 768p 版本）
    #
    # 【键名形态 —— 决定用哪个加载节点】
    #   中文名两份是 diffusion_model. 前缀 -> 核心 LoraLoaderModelOnly
    #   LightX2V 两份也是 diffusion_model. 前缀 -> 核心 LoraLoaderModelOnly
    # （判据是 safetensors 头部实测，不是文件名硬编 —— 见
    #   detect_accel_lora_loader）
    "turbo_4step_cn": {
        "name": "minimax_h3_turbo_4步加速_comfyui.safetensors",
        "display": "Turbo 4 步（中文名加速版）",
        "purpose": "官方 Turbo 4 步蒸馏权重的**中文名发行版**，"
                   "键名带 diffusion_model. 前缀 —— 与 ckpt850 是同族，"
                   "但必须用核心 LoraLoaderModelOnly 加载。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "518 个层全部命中（实测，键名带 diffusion_model. 前缀）。"
                "剪枝底模的 AdaLN 层（51 个）**不会**被核心节点重注入 —— "
                "核心节点没有 Turbo 节点那套运行期 AdaLN 机制，"
                "所以它配**未剪枝**底模（int8 / bf16）才是完整命中。",
    },
    "turbo_4step_cn_ema": {
        "name": "minimax_h3_turbo_4步加速ema_comfyui.safetensors",
        "display": "Turbo 4 步（中文名 EMA 版）",
        "purpose": "Turbo 4 步的中文名 EMA 权重，"
                   "键名带 diffusion_model. 前缀。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "518 个层全部命中（实测，键名带 diffusion_model. 前缀）。"
                "同 turbo_4step_cn：剪枝底模的 AdaLN 不会被运行期重注入。",
    },
    "lightx2v_4step_v01_comfy": {
        "name": "minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors",
        "display": "LightX2V 4 步（v0.1 comfy 版）",
        "purpose": "LightX2V Turbo 4 步 v0.1 的 comfy 打包版，"
                   "键名带 diffusion_model. 前缀。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "416 个层全部命中（实测）。它不含 AdaLN，"
                "配剪枝底模时 AdaLN 更新不会生效（核心节点无重注入机制）。",
    },
    "lightx2v_sla_768p_root": {
        "name": "minimax_h3_fl2v_turbo_4step_v0.1_768p_sla_comfyui_bf16.safetensors",
        "display": "LightX2V SLA 4 步（768p · 根目录版）",
        "purpose": "与 h3_lightx2v/ 目录里那份同名的 SLA 权重"
                   "（键名形态一致），此处登记**根目录**那一份，"
                   "它才是 h3_lightx2v/ 目录版之外真实存在的第二份。",
        "recommended_strength": 1.0,
        "recommended_steps": 4,
        "recommended_scheduler": "beta",
        "recommended_sampler": "euler",
        "note": "624 个层全部命中（实测）。它不含 AdaLN。",
    },
}

# ------------------------------------------------------------------
# 注意力机制（实测本机 object_info 里存在的节点）
# ------------------------------------------------------------------
# 【2026-11-12 修 —— 两处按用户实测反馈调整】
#
#  1. **移除「Sol-Attn 渐进调度」（sol_scheduled）**
#     用户实测画面有**叠影**（渐进 tau 调度让高噪步过于近似，
#     运动主体糊成两层）。选项已从清单里**整个删掉** ——
#     不是「列出来再置灰」（置灰等于「能选但会被服务端改写」，
#     是假选项）。服务端清单是唯一真相源，前端只是渲染它。
#
#  2. **加速度标注（speed_rank / speed_label）**
#     用户要求在下拉里直接看出快慢：
#       稀疏视频注意力 = 速度最快
#       sol-attn        = 速度很快
#       comfy kitchen   = 速度一般
#       sageattention   = 较慢
#       pytorch         = 最慢
#     排序 = speed_rank 升序（1 = 最快）。
#
# 【速度标注的依据】
#   * 稀疏注意力（H3SparseAttention）直接砍掉视频注意力连接 ——
#     计算量本身最少，节点自述「Lower video attention budgets are faster」。
#   * sol-attn 走 Triton 近似 kernel（H3 自注意力专用路径），
#     比稠密 PyTorch SDPA 快，但每个 block 仍有 kernel launch 开销。
#   * comfy kitchen 是 INT8 **稠密**量化 —— 计算量与稠密相同，
#     只是把 matmul 换成量化算子，**没有稀疏化**，所以只是「一般」。
#   * sageattention 是**稠密**注意力换 kernel（省显存），计算量不变，
#     且额外有 patch 开销 -> 较慢。
#   * pytorch 稠密 SDPA 是基准 -> 最慢。
#   （用户给定的排序与上述机制事实一致，故直接采用。）
VIDEO_ATTENTION_MODES: dict[str, dict] = {
    "default": {
        "label": "默认（PyTorch 稠密注意力）",
        "node": None,
        "inputs": {},
        "note": "不做任何 patch。速度基准，最稳。",
        "speed_rank": 5,
        "speed_label": "速度最慢",
    },
    "kitchen": {
        "label": "Comfy Kitchen INT8 量化注意力",
        "node": "ModelAttentionBackend",
        "inputs": {"attention": "comfy kitchen attention"},
        "note": "ComfyUI 内建的 INT8 量化稠密注意力，只支持 Nvidia / AMD。"
                "省显存、略掉精度，速度提升有限但最稳。",
        "speed_rank": 3,
        "speed_label": "速度一般",
    },
    "sage": {
        "label": "SageAttention 显存优化",
        "node": "MiniMaxH3MemoryEfficientSageAttentionPatch",
        "inputs": {},
        "note": "KJNodes 的 SageAttention patch，覆盖 H3 自注意力，"
                "降峰值显存。需要较新的 sageattention（本机已装）。",
        "speed_rank": 4,
        "speed_label": "速度较慢",
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
        "speed_rank": 2,
        "speed_label": "速度很快",
    },
    "sparse": {
        "label": "稀疏视频注意力（预算制）",
        "node": "H3SparseAttention",
        "inputs": {"video_budget": 0.15, "denser_early_late_steps": True},
        "note": "只保留一部分视频注意力连接：video_budget 越低越快，"
                "但提示词贴合度与运动细节会掉。没有「安全值」，"
                "每条提示词要自己试。",
        "speed_rank": 1,
        "speed_label": "速度最快",
    },
}

#: **被移除**的注意力机制 key（2026-11-12 用户实测画面有叠影）。
#:
#: 保留这个常量（而不是彻底忘掉）是刻意的：
#:   * 老任务落库的 params 里可能还有 `attention: "sol_scheduled"`；
#:   * 面板配置（/api/video-config）里也可能存着这个 key；
#:   * 这两处都要**明确降级**（回落到 default）而不是报错或静默失败，
#:     否则老任务点「复用参数」会得到一个下拉里根本不存在的值。
#:
#: 节点名也一并登记 —— 服务端归一层遇到这个 key 时明确报错说明，
#: 而不是当成「未知注意力机制」。
VIDEO_REMOVED_ATTENTION_MODES: dict[str, dict] = {
    "sol_scheduled": {
        "label": "Sol-Attn 渐进调度（首尾密度不同）",
        "node": "MiniMaxH3ScheduledSolAttentionPatch",
        "reason": "用户实测该调度会让画面出现叠影（高噪步过于近似，"
                  "运动主体糊成两层），已从选项中移除。",
    },
}
#: 视频时长（秒）区间 —— **2026-11-12 放宽**。
#:
#: 旧值是 [2, 15]（server.normalize_t2v_params 里 `min(15.0, duration)`），
#: 用户实测反馈「15 秒太短，一分钟都行，最多 OOM」。
#:
#: 【为什么放到 60】
#:   * ComfyUI 的 MiniMaxH3ImageToVideo 节点 schema：
#:       length = INT(min=5, max=3600, step=17)
#:     节点本身允许 3600 帧（150 秒）—— 15 秒是**我们自己的**限制，
#:     不是模型/节点的能力上限。
#:   * 帧数表达式 `max(5, round(a*24)) + (5 - (... % 17)) % 17`
#:     在任何秒数下都能算出合法帧数。
#:   * 真正的瓶颈是显存 —— 长时长 = 线性增长的 latent 与注意力序列。
#:     所以上限放宽到 60 秒（1440 帧），超显存时由 ComfyUI 抛 OOM，
#:     **明确报错**（任务失败 + 错误文案），不做静默截断。
#:
#: 【为什么不静默截断】
#: 用户明确要求「如超显存应给出明确报错而非静默截断」。
#: 静默截断会让用户以为自己生成了 60 秒、实际只拿到 15 秒 —— 那是撒谎。
#: 现在超上限**直接拒绝**并说明上限值。
VIDEO_DURATION_MIN = 2.0
VIDEO_DURATION_MAX = 60.0
VIDEO_DURATION_DEFAULT = 5.0

#: 视频时长超上限时的**可执行**提示（不静默截断）。
VIDEO_DURATION_MAX_MESSAGE = (
    "文生视频时长上限为 {limit:g} 秒。当前请求 {value:g} 秒，超出上限。"
    "请把时长调小；显存不足时请改用更小的分辨率或更短的时长。"
)


def normalize_video_duration(value, default: float = VIDEO_DURATION_DEFAULT
                             ) -> tuple[float, str]:
    """把时长归一到 [VIDEO_DURATION_MIN, VIDEO_DURATION_MAX]。

    返回 (生效时长, 错误信息)。**错误信息为空表示通过**。

    非法输入（不是数字）-> 回落到默认时长，不报错（老任务/老客户端兼容）。
    超过上限 -> **不截断**，返回错误信息（调用方据此拒绝提交）。
    """
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return default, ""
    if duration != duration:  # NaN
        return default, ""
    if duration > VIDEO_DURATION_MAX:
        return duration, VIDEO_DURATION_MAX_MESSAGE.format(
            limit=VIDEO_DURATION_MAX, value=duration)
    return max(VIDEO_DURATION_MIN, duration), ""


#: 保留旧名别名（旧代码 / 旧测试引用它）。
VIDEO_DURATION_MIN_OLD = 2.0
VIDEO_DURATION_MAX_OLD = 15.0

VIDEO_DEFAULT_ATTENTION = "default"

# ------------------------------------------------------------------
# 「不挂加速 LoRA」时的标准采样档（2026-11-14）
# ------------------------------------------------------------------
# 【为什么需要这三个常量】
# 加速 LoRA 从「恒定存在」变成「用户可选」之后，采样档位必须跟着变：
#   挂加速 LoRA -> 蒸馏档（4~8 步 / beta / euler）
#   不挂        -> 标准多步采样（20 步 / simple / res_multistep）
#
# 旧实现恒取 model["steps"] = 4（蒸馏档的步数），
# 于是「不挂加速 LoRA」也会跑 4 步 —— 那是**蒸馏档**的步数，
# 配非蒸馏权重必然糊（旧实现之所以没暴露这个问题，
# 是因为加速 LoRA 恒定存在）。
#
# 取值依据：官方非蒸馏 H3 模板的默认档
# （comfy/supported_models.py 的 shift=12 / audio_shift=3 是模型内生值，
#  调度器与采样器取社区通用组合 simple + res_multistep）。
VIDEO_STEPS_WITHOUT_ACCEL = 20
VIDEO_SCHEDULER_WITHOUT_ACCEL = "simple"
VIDEO_SAMPLER_WITHOUT_ACCEL = "res_multistep"

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
    # 【2026-11-13 新增 —— 借鉴 nonebot H3 工作流的 H3SigmaRefiner】
    #
    # 在 sigma 表的**低噪段局部加密采样步**，针对「高速运动边缘像素颗粒 / 闪烁」。
    #
    # 【为什么默认关闭】nonebot 模板里用的是 start_at_sigma=0.7，
    # 但在本项目默认档（steps=4 / beta）下 0.7 落在 no-op 区 ——
    # beta 档 sigma 尾段只有 0.7207 一档 > 0.7，然后直接跳到 0.0，
    # 节点判定 idx == len-1 -> 直接原样返回（**完全无效**）。
    # 节点自己的 tooltip 明确写「H3 推荐设在 2.0 ~ 3.5 之间」，
    # 所以这里的默认值取 2.5（落在推荐区间内）。
    #
    # 【风险】插入后 sigma 表变长 -> NFE +extra_steps，
    # 且 transition_step 是**索引**（指向 sigma 表里的位置），
    # 语义会随表长变化 —— 见 video_workflow._twopass_block 的重标定。
    "sigma_refine_enabled_default": False,
    "sigma_refine_extra_steps_default": 1,
    "sigma_refine_start_at_sigma_default": 2.5,
    "sigma_refine_end_at_sigma_default": 0.0,
    "sigma_refine_spacing_default": "cosine",
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
                          max_pixels: float | None = None,
                          strict_over_limit: bool = False,
                          mode: str | None = None):
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

    【mode】(2026-11-15 代码审查新增)
      只用于**错误文案**：让提示说「首尾帧生视频分辨率上限为 0.8M …」
      而不是把「文生视频 / 2.0M」写死（首尾帧的实际上限是 0.8MP）。
      不影响任何计算结果。
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
                    mode_label=VIDEO_MODE_LABELS.get(mode) or "视频",
                    limit_mp=_format_limit_mp(max_pixels),
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
    """底模在本机是否可用（**以 ComfyUI 的模型列表为准**）。

    【2026-11-11 改造】原来这里只扫盘（`find_weight`），跟 ComfyUI 的
    下拉框**没有必然关系** —— 磁盘上有的文件，ComfyUI 未必扫得到
    （extra_model_paths.yaml 没配 / 目录类型不匹配 / 缓存未刷新），
    反之 ComfyUI 扫到的也未必在我们写死的这几个目录里。

    现在：**先问 ComfyUI**（`/models/diffusion_models` + `UNETLoader`
    的 `unet_name` 枚举，两者交叉验证），ComfyUI 不可达时才退回扫盘。

    实现上是 `model_status()` 的薄封装 —— **只有一个判据**，
    面板（list_models）与提交校验（normalize_t2v_params）都走它，
    所以不会出现「下拉显示 A、提交用 B」。

    见 h3studio/comfy_models.py 的模块说明。
    """
    return model_status(model_key) == "available"


def lora_available(name: str) -> bool:
    """加速 LoRA 在本机是否可用（同样以 ComfyUI 的 `/models/loras` 为准）。"""
    return _lora_available(name, comfy_snapshot())


# ------------------------------------------------------------------
# ComfyUI 模型清单快照（availability 的**权威来源**）
# ------------------------------------------------------------------
#
# 【为什么要一个「快照」而不是每次都问】
#   list_models() 要给 5 个底模 + 8 个 LoRA 各判一次可用性。
#   逐个问 ComfyUI 就是几十次 HTTP —— 面板每次打开都打一遍太浪费。
#   所以：一次拉，缓存成快照，全部分析共用同一份事实。
#
# 【快照里有什么】
#   * unets   —— `/models/diffusion_models` 的全部条目（目录视角）
#   * unet_enum —— `UNETLoader.unet_name` 的可选值（节点视角）
#   * loras   —— `/models/loras` 的全部条目
#   两个 unet 视角做**交叉验证**：任一视角认这个名字就算在。
#   （实测本机两者完全一致，各 151 条。）
#
# 【拿不到时怎么办】
#   返回 None -> 上面的 model_available / lora_available 退回扫盘，
#   并且 list_models() 会在 payload 里标 model_list_source=
#   "filesystem_fallback" + model_list_error=原因，让前端如实显示。


class ComfyModelSnapshot:
    """ComfyUI 当前可用模型清单的一次快照。"""

    __slots__ = ("unets", "unet_enum", "loras", "error", "fetched_at")

    def __init__(self,
                 unets: list[str] | None = None,
                 unet_enum: list[str] | None = None,
                 loras: list[str] | None = None,
                 error: str = "",
                 fetched_at: float = 0.0):
        self.unets = list(unets or [])
        self.unet_enum = list(unet_enum or [])
        self.loras = list(loras or [])
        self.error = error
        self.fetched_at = fetched_at

    @property
    def ok(self) -> bool:
        return not self.error

    def has_unet(self, name: str) -> bool:
        """这个名字在 ComfyUI 的 UNET 下拉框里吗？

        两个视角取**并集** —— 只要任一视角认，就算 ComfyUI 能选。
        （目录视角漏了但节点枚举有 -> 仍可选，反之亦然。）
        """
        if not name:
            return False
        return name in set(self.unets) or name in set(self.unet_enum)

    def has_lora(self, name: str) -> bool:
        if not name:
            return False
        return name in set(self.loras)

    def missing_unets(self) -> list[str]:
        """ComfyUI 两个视角都没认的底模权重名（诊断用）。"""
        pool = set(self.unets) | set(self.unet_enum)
        out = []
        for meta in VIDEO_MODELS.values():
            name = meta["unet"]
            if name not in pool and name not in out:
                out.append(name)
        return out


#: 快照缓存（模块级）。面板每次打开 /api/video-options 时刷新。
_SNAPSHOT: ComfyModelSnapshot | None = None
_SNAPSHOT_TS = 0.0


def comfy_base_url() -> str:
    """ComfyUI 地址（与 server.py 的 COMFY 同一来源）。

    优先读环境变量 H3STUDIO_COMFY_URL，其次读 H3STUDIO_CONFIG 指向的
    config.json 的 comfyui_url，最后退回本机默认 8188。

    注意：server.py 的 COMFY 常量读的是**同目录**的 config.json
    （CONFIG_PATH = BASE / "config.json"），而不是 H3STUDIO_CONFIG ——
    所以这里也优先读同目录的 config.json，保持两边一致。
    """
    env = os.environ.get("H3STUDIO_COMFY_URL")
    if env:
        return env.rstrip("/")
    # 候选配置：H3STUDIO_CONFIG 环境变量指向的，其次同目录的 config.json
    candidates = []
    env_cfg = os.environ.get("H3STUDIO_CONFIG")
    if env_cfg:
        candidates.append(Path(env_cfg))
    candidates.append(Path(__file__).resolve().parent.parent / "config.json")
    for path in candidates:
        try:
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8")
            url = (json.loads(raw) or {}).get("comfyui_url")
            if url:
                return str(url).rstrip("/")
        except Exception:
            continue
    return "http://127.0.0.1:8188"


def refresh_comfy_snapshot(base_url: str | None = None,
                           *,
                           ttl: float = 20.0,
                           force: bool = False,
                           now: float | None = None) -> ComfyModelSnapshot:
    """重新拉一次 ComfyUI 模型清单快照。

    ComfyUI 不可达时**不抛异常** —— 返回一个带 error 的快照，
    调用方据此退回扫盘并把原因如实带给前端。
    """
    global _SNAPSHOT, _SNAPSHOT_TS
    import time as _time
    stamp = _time.time() if now is None else now
    if not force and _SNAPSHOT is not None and (stamp - _SNAPSHOT_TS) < ttl:
        return _SNAPSHOT
    url = (base_url or comfy_base_url()).rstrip("/")
    unets: list[str] = []
    unet_enum: list[str] = []
    loras: list[str] = []
    errors: list[str] = []
    try:
        unets = comfy_models.fetch_model_list(url, "diffusion_models")
    except comfy_models.ComfyModelListError as exc:
        errors.append(str(exc))
    try:
        loras = comfy_models.fetch_model_list(url, "loras")
    except comfy_models.ComfyModelListError as exc:
        errors.append(str(exc))
    try:
        unet_enum = comfy_models.fetch_object_info_enum(url, "UNETLoader",
                                                        "unet_name")
    except comfy_models.ComfyModelListError as exc:
        errors.append(str(exc))
    # 全军覆没才算「拿不到」；只挂了一个视角仍然可用（降级但有事实）
    if not unets and not unet_enum and not loras:
        snapshot = ComfyModelSnapshot(error="；".join(errors) or "ComfyUI 模型清单为空")
    else:
        snapshot = ComfyModelSnapshot(unets=unets, unet_enum=unet_enum,
                                      loras=loras, error="", fetched_at=stamp)
    _SNAPSHOT = snapshot
    _SNAPSHOT_TS = stamp
    return snapshot


def comfy_snapshot(base_url: str | None = None,
                   *,
                   ttl: float = 20.0,
                   force: bool = False) -> ComfyModelSnapshot | None:
    """拿当前快照（必要时刷新）。拿不到 ComfyUI 时返回 None。"""
    snapshot = refresh_comfy_snapshot(base_url, ttl=ttl, force=force)
    return snapshot if snapshot.ok else None


def comfy_snapshot_error() -> str:
    """最近一次拿 ComfyUI 清单失败的原因（没有则空串）。"""
    return _SNAPSHOT.error if _SNAPSHOT is not None else ""


def reset_comfy_snapshot() -> None:
    """清掉快照缓存（测试用）。"""
    global _SNAPSHOT, _SNAPSHOT_TS
    _SNAPSHOT = None
    _SNAPSHOT_TS = 0.0
    comfy_models.clear_cache()


async def refresh_comfy_snapshot_async(base_url: str | None = None,
                                       *,
                                       ttl: float = 20.0,
                                       force: bool = False,
                                       timeout: float = 60.0) -> ComfyModelSnapshot:
    """异步版快照刷新 —— **服务端必须用这个**。

    【为什么不能直接用同步版】
    同步版用 urllib 发 HTTP，在 aiohttp 的事件循环里**会阻塞整个
    服务端**（直到 ComfyUI 回应）。而 ComfyUI 的
    `/models/diffusion_models` 首次调用实测要 **43 秒**（它要递归扫
    所有 extra_model_paths 目录）—— 那 43 秒里整个 Studio 的
    状态流、其它 API 全都卡住。

    所以服务端一律走 aiohttp（异步），并且**三个请求并发**发出去，
    总耗时 = 最慢的那个，而不是三个加起来。

    ComfyUI 不可达时**不抛异常** —— 返回带 error 的快照，
    调用方据此退回扫盘并把原因如实带给前端。
    """
    global _SNAPSHOT, _SNAPSHOT_TS
    import time as _time
    stamp = _time.time()
    if not force and _SNAPSHOT is not None and (stamp - _SNAPSHOT_TS) < ttl:
        return _SNAPSHOT
    url = (base_url or comfy_base_url()).rstrip("/")

    from aiohttp import ClientSession, ClientTimeout

    async def _one(coro, label):
        """跑一个请求；失败返回 ([], 原因)，**不抛**（降级要拿到原因）。"""
        try:
            return await coro, ""
        except comfy_models.ComfyModelListError as exc:
            return [], f"{label}: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 任何意外都不该炸掉面板
            return [], f"{label}: {type(exc).__name__}: {exc}"

    async with ClientSession(timeout=ClientTimeout(total=timeout)) as session:
        results = await asyncio.gather(
            _one(comfy_models.fetch_model_list_async(
                session, url, "diffusion_models"), "diffusion_models"),
            _one(comfy_models.fetch_model_list_async(
                session, url, "loras"), "loras"),
            _one(comfy_models.fetch_object_info_enum_async(
                session, url, "UNETLoader", "unet_name"), "UNETLoader"),
        )
    unets = results[0][0]
    loras = results[1][0]
    unet_enum = results[2][0]
    errors = [results[0][1], results[1][1], results[2][1]]
    errors = [e for e in errors if e]
    # 全军覆没才算「拿不到」；只挂了一个视角仍然可用（降级但有事实）
    if not unets and not unet_enum and not loras:
        snapshot = ComfyModelSnapshot(
            error="；".join(errors) or "ComfyUI 模型清单为空")
    else:
        snapshot = ComfyModelSnapshot(unets=unets, unet_enum=unet_enum,
                                      loras=loras, error="", fetched_at=stamp)
    _SNAPSHOT = snapshot
    _SNAPSHOT_TS = stamp
    return snapshot


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


def check_h3_lora_compat(lora_name: str, unet_name: str,
                         loader: str | None = None) -> dict:
    """实测某个加速 LoRA 与某个 H3 底模的层结构能不能对上。

    返回：
      {
        "compatible": bool,      # 能不能用
        "matched": int,          # 层名+形状都对上的层数
        "runtime_injected": int, # AdaLN 层：底模折叠成曲线，由 Turbo 节点运行期重注入
        "shape_mismatch": int,   # 层名对得上但形状对不上（真不兼容）
        "missing": int,          # 底模里根本没这个层
        "total": int,
        "loader": str,           # 实际会用哪个加载节点（判定 AdaLN 能否生效的依据）
        "reason": str,           # 不兼容原因（给界面显示）
      }

    **AdaLN 要单独算**（2026-11-10 实测踩坑）：
    Turbo 系加速 LoRA 的 adaln_proj 层是 2688 维输入，而剪枝底模把它折叠成
    8 维曲线 —— 形状对不上，但**这不是不兼容**：
    ComfyUI-MiniMax-H3-Turbo 的 `MiniMaxH3TurboLoRA` 节点检测到
    `use_adaln_curves` 后，会用 `_inject_adaln_egrid()` 把 AdaLN 更新在
    运行期重新注入（custom_nodes/ComfyUI-MiniMax-H3-Turbo/__init__.py:378）。
    把这些层算成「不兼容」会把剪枝底模整条路封死。

    【2026-11-15 修 —— AdaLN 的运行期重注入是「Turbo 节点专属」能力】
    上面那段「不算不兼容」的前提是**用 Turbo 节点加载**。实测（ComfyUI
    0.45.3 / comfy/weight_adapter/lora.py:268-283）核心节点的权重数学是：

        lora_diff = torch.mm(mat1, mat2).reshape(weight.shape)

    其中 mat1 = lora_B [96768, 16]、mat2 = lora_A [16, 2688]。
    对剪枝底模（AdaLN 权重 [96768, 8]）这一步直接抛
    「shape '[96768, 8]' is invalid for input of size 260112384」，
    而 comfy/weight_adapter/lora.py:284 用 try/except 吞掉异常 ->
    **AdaLN 的贡献被静默丢弃**，只留一条 ERROR 日志。

    也就是说：带 AdaLN 的 LoRA 用**核心 LoraLoaderModelOnly** 加载到
    剪枝底模上时，那 51 个 AdaLN 层**不会生效**，但兼容性检查此前仍
    把它们记成「runtime_injected」并判 compatible=True —— 这正是
    「静默失效」的另一种形态。

    所以这里必须**按加载节点分别判定**：
      * Turbo 节点（裸键名族）-> AdaLN 记 runtime_injected，compatible
      * 核心节点（diffusion_model. 前缀族）-> AdaLN 记 shape_mismatch，
        并给出明确原因（而不是默默当作可用）
    loader 传 None 时按实测键名形态自动判定（与
    detect_accel_lora_loader 同一判据）。
    """
    lora_path = find_weight(lora_name, kind="loras")
    unet_path = find_weight(unet_name)
    if loader is None:
        loader = detect_accel_lora_loader(lora_name)
    empty = {"compatible": False, "matched": 0, "runtime_injected": 0,
             "shape_mismatch": 0, "missing": 0, "total": 0,
             "loader": loader, "reason": ""}
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
    # AdaLN 的运行期重注入是 **Turbo 节点专属**能力 —— 核心节点做不到
    # （见上面 docstring 的实测结论）。所以判定必须按加载节点分流。
    turbo_loader = (loader == ACCEL_LOADER_TURBO)
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
            if ("adaln_proj" in target and len(weight) == 2 and weight[1] == 8
                    and turbo_loader):
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
        "loader": loader,
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
        # 【2026-11-15】区分两种「形状对不上」：
        #   * AdaLN 层 + 核心加载节点 -> 运行期重注入做不到（静默失效）
        #   * 其它形状不匹配          -> 底模把隐藏维折叠过
        adaln_mismatch = 0
        for base, shapes in bases.items():
            if "adaln_proj" not in base:
                continue
            s = shapes.get("A")
            if not (s and len(s) == 2):
                continue
            target = _match_module(base, modules)
            if target is None:
                continue
            w = modules.get(target) or []
            if len(w) == 2 and w[1] != s[1]:
                adaln_mismatch += 1
        if adaln_mismatch and not turbo_loader:
            result["reason"] = (
                f"该 LoRA 带 {adaln_mismatch} 个 AdaLN 层，而当前底模把它们折叠成 "
                "8 维曲线；这只能由 Turbo 节点（MiniMaxH3TurboLoRA）在运行期重注入，"
                "而本 LoRA 的权重键名形态（diffusion_model. 前缀）会走核心 "
                "LoraLoaderModelOnly 加载 —— 核心节点做不到，AdaLN 的贡献会被"
                "静默丢弃。请改配未剪枝底模（int8 / bf16），或改用 Turbo 系权重。")
        else:
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

    【2026-11-14】覆盖**全部**加速 LoRA（Turbo 系 + 组合枚）。
    旧实现只遍历 VIDEO_ACCEL_LORA_COMBOS，Turbo 系那两枚永远查不到
    兼容性 —— 于是前端无法按底模过滤它们。

    缓存：兼容性只跟「权重文件的字节」有关，不随时间变，
    所以按 (model_key, lora_name, 文件 mtime/size) 缓存，
    切来切去模型不会反复读盘。
    """
    model = VIDEO_MODELS.get(model_key)
    if model is None:
        return {}
    out: dict[str, dict] = {}
    for key in all_accel_lora_keys():
        meta = accel_lora_meta(key)
        if not meta:
            continue
        # 【2026-11-15】把**实际会用的加载节点**传进去 ——
        # AdaLN 的运行期重注入只有 Turbo 节点做得到，核心节点做不到。
        # 不传 loader 的话，核心节点加载带 AdaLN 的 LoRA 会被误判成兼容。
        loader = detect_accel_lora_loader(meta["name"])
        check = check_h3_lora_compat(meta["name"], model["unet"], loader=loader)
        out[key] = {
            "compatible": bool(check["compatible"]),
            "reason": check["reason"],
            "loader": loader,
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
#
# 【可用性判定的唯一真相源：ComfyUI 的模型列表 API】
#
# 用户 2026-11-11 报缺陷：「文生视频」面板下方红字说
# 「权重在外硬盘 /mnt/nvme1t_colorful —— 尚未挂载或 ComfyUI 下拉框确实没有
#  这个单卡，提交会被拒绝」—— 这条结论是**错的**，因为：
#
#   1. 那行字是 `VIDEO_MODELS[...]["warn"]` 里**写死的常量**，
#      跟实际可用性判定（`model_available()`）**没有任何关系**；
#   2. `model_available()` 只扫盘，从没问过 ComfyUI「你下拉框里到底有啥」；
#   3. 本机实测：外置盘**已挂载**（/dev/nvme2n1p1 -> /mnt/nvme1t_colorful），
#      ComfyUI `/models/diffusion_models` 有 **151 条**，5 个 H3 FL2VA 权重
#      **全在里面**（curl 证据见交付报告）。
#
# 所以现在改成：**问 ComfyUI 本人**。每个底模带三种状态之一：
#
#   status = "available"   ComfyUI 的下拉框里有这个权重 -> 可选
#   status = "external_missing"
#                          权重在外置盘但 ComfyUI 没扫到（盘没挂 / 没配
#                          extra_model_paths / 缓存没刷新）-> **禁用**，
#                          提示「外置未挂载，当前不可选」
#   status = "missing"     文件在本机默认目录都找不到 -> 禁用
#
# 另加 status = "unknown"：ComfyUI 连不上（降级到扫盘），此时如实告诉
# 用户「未能连上 ComfyUI 模型列表」，而不是谎称「获取不到模型」。

#: 外置盘根目录（用来判断「这个权重是不是在外置盘上」）
_EXTERNAL_ROOTS = ("/mnt/nvme1t_colorful", "/mnt/nvme232g",
                   "/mnt/usb2t_toshiba", "/mnt/usb2t")


def _is_external_location(location: str) -> bool:
    """权重位置是否在外置盘上。"""
    if not location:
        return False
    return any(location.startswith(root) for root in _EXTERNAL_ROOTS)


def _model_status(key: str, meta: dict, snapshot: ComfyModelSnapshot | None) -> str:
    """单个底模的可用状态。

    返回：
      available        —— ComfyUI 认它（权威），或降级时磁盘上有它
      external_missing —— ComfyUI 不认，但磁盘上有（在**外置盘**上）-> 禁用
      missing          —— ComfyUI 不认，磁盘上也没有 -> 禁用
      unknown          —— ComfyUI 连不上且磁盘上也没有 -> 禁用
    """
    name = meta["unet"]
    if snapshot is not None:
        # ComfyUI 说有 -> 有（这是唯一权威）
        if snapshot.has_unet(name):
            return "available"
        # ComfyUI 说没有 -> 看看文件在不在盘上，区分「外置没挂载」与「真没有」
        if find_weight(name) is None:
            return "missing"
        return "external_missing"
    # ComfyUI 不可达：退回扫盘。文件在 -> 仍可选（ComfyUI 恢复后照样能跑），
    # 但**不能**声称「已确认在 ComfyUI 模型列表里」。
    return "available" if find_weight(name) is not None else "unknown"


def _status_note(key: str, meta: dict, status: str,
                 snapshot: ComfyModelSnapshot | None) -> str:
    """按状态给出**如实**的提示文案。

    【硬约束】绝不能在 ComfyUI 已经收录该权重时显示「获取不到模型」
    之类的断言 —— 那正是本次要修的 bug。
    """
    if status == "available":
        if snapshot is not None and snapshot.has_unet(meta["unet"]):
            return "ComfyUI 模型列表已收录该权重，可直接选用。"
        return "本机磁盘扫描到该权重（当前未能连上 ComfyUI 模型列表，可用性待确认）。"
    if status == "external_missing":
        if _is_external_location(meta.get("location", "")):
            return (f"权重位于外置盘 {meta.get('location') or ''}，"
                    "但 ComfyUI 的模型列表里没有它 —— 外置盘可能未挂载或未扫描到，"
                    "当前不可选。")
        return (f"权重在 {meta.get('location') or '本机目录'}，"
                "但 ComfyUI 的模型列表里没有它 —— 当前不可选。")
    if status == "missing":
        return (f"本机找不到权重 {meta['unet']}"
                f"（{meta.get('location') or '未知目录'}）。")
    return "未能连上 ComfyUI 模型列表，且本机磁盘也找不到该权重 —— 当前不可选。"


def list_models(snapshot: ComfyModelSnapshot | None = None) -> list[dict]:
    """下发给前端的底模清单（**数据驱动**：可用性来自 ComfyUI API）。

    每条除原有的静态字段外，新增：
      * status         —— available / external_missing / missing / unknown
      * available      —— status == "available"（前端据此决定禁用与否）
      * status_note    —— 按 status 现算的如实提示（**不是写死的常量**）
      * comfy_listed   —— ComfyUI 的下拉框里是否有这个名字（布尔）
      * on_external_disk —— 权重位置是否在外置盘
    """
    if snapshot is None:
        snapshot = comfy_snapshot()
    out = []
    for key, model in VIDEO_MODELS.items():
        item = dict(model)
        item["key"] = key
        status = _model_status(key, model, snapshot)
        item["status"] = status
        item["available"] = status == "available"
        item["comfy_listed"] = bool(snapshot.has_unet(model["unet"])) \
            if snapshot is not None else None
        item["on_external_disk"] = _is_external_location(model.get("location", ""))
        item["status_note"] = _status_note(key, model, status, snapshot)
        # 静态 warn 只保留**与可用性无关**的固有提醒（显存/兼容性）
        item["warn"] = model.get("warn", "")
        out.append(item)
    return out


def list_accel_loras(snapshot: ComfyModelSnapshot | None = None
                     ) -> tuple[list[dict], list[dict]]:
    """加速 LoRA 清单 —— **统一成一组可选项**（2026-11-14 重构）。

    【为什么不再分「固定枚 / 组合枚」两栏】
    旧设计里固定枚（ckpt850 / v4step600）不在下拉里、永远生效，
    下拉里的那枚是**额外叠加**的第二枚 —— 于是「选什么」不决定
    「加载什么」，用户报的「选了不生效」就是这么来的。
    现在两个集合合并成一个可选项列表：
      * 每项带 loader（用哪个节点加载）—— 按实测键名形态分流；
      * 每项带 available / compatible —— 前端据此过滤；
      * 「不挂加速 LoRA」是一个显式选项（原来没有）。

    返回 (accel_loras, combos)：
      accel_loras —— 全部可选项（含 loader / 可用性 / 兼容性）
      combos      —— 空列表（保留字段形状，老前端读它不会炸）
    """
    if snapshot is None:
        snapshot = comfy_snapshot()
    out = []
    for key in all_accel_lora_keys():
        meta = accel_lora_meta(key) or {}
        item = dict(meta)
        item["key"] = key
        item["available"] = _lora_available(meta["name"], snapshot)
        # 加载节点路由：按权重的实测键名形态决定（Turbo / 核心 LoraLoader）
        item["loader"] = detect_accel_lora_loader(meta["name"])
        item["loader_label"] = (
            "Turbo 节点（支持剪枝底模 AdaLN 运行期重注入）"
            if item["loader"] == ACCEL_LOADER_TURBO
            else "核心 LoraLoaderModelOnly（认 diffusion_model. 前缀权重）")
        out.append(item)
    return out, []


def _lora_available(name: str, snapshot: ComfyModelSnapshot | None) -> bool:
    if snapshot is not None:
        return snapshot.has_lora(name)
    return find_weight(name, kind="loras") is not None


# ------------------------------------------------------------------
# 加速 LoRA 的加载节点路由（2026-11-14）
# ------------------------------------------------------------------
#
# 【为什么需要路由】
# 不同的加速 LoRA 权重**键名形态不同**，而 ComfyUI 的两个加载节点
# 各自只认一种形态：
#
#   MiniMaxH3TurboLoRA（ComfyUI-MiniMax-H3-Turbo 插件）
#     key_map = {"<module>": "diffusion_model.<module>.weight"}
#     -> 只认**裸键名**（blocks.* / token_refiner.* / final_layer.*）
#     -> 额外能力：剪枝底模的 AdaLN 运行期重注入（_inject_adaln_egrid）
#
#   LoraLoaderModelOnly（ComfyUI 核心）
#     key_map 里对 MiniMaxH3 专门注册了
#       key_map[bare] = "diffusion_model.<bare>.weight"
#     -> 认**带 diffusion_model. 前缀**的权重（LightX2V / DMAD / better_motion）
#     -> 没有 AdaLN 运行期重注入能力
#
# 用错节点的后果**不是报错，是静默失效**：Turbo 节点拿到前缀权重时
# 会把模块名当成 `diffusion_model.diffusion_model.xxx`，一个都 patch 不上
# （实测日志 "416 backbone modules, 0 weights patched"），
# 出片照跑但 LoRA 根本没生效 —— 用户完全看不出来。
#
# 所以这里按**实测键名形态**分流，而不是按「哪个更常用」拍脑袋。
# 判据来自 safetensors 头部（前缀 + 模块名），不是写死的文件名清单。

#: Turbo 节点（裸键名族）
ACCEL_LOADER_TURBO = "MiniMaxH3TurboLoRA"
#: 核心 LoraLoaderModelOnly（diffusion_model. 前缀族）
ACCEL_LOADER_CORE = "LoraLoaderModelOnly"


def detect_accel_lora_loader(lora_name: str) -> str:
    """按权重的**实测键名形态**决定用哪个加载节点。

    返回 ACCEL_LOADER_TURBO 或 ACCEL_LOADER_CORE。

    读不到文件时**保守回落**到 Turbo 节点（Turbo 系两枚权重都在本机
    默认目录，形态已知）；读到了但两种形态都不像时也回落 Turbo ——
    因为「Turbo 节点加载失败」会打日志，而核心节点加载失败是静默的，
    宁可要一个能看见的失败。
    """
    path = find_weight(lora_name, kind="loras")
    if path is None:
        return ACCEL_LOADER_TURBO
    header = _read_header(path)
    if not header:
        return ACCEL_LOADER_TURBO
    prefixes = set()
    for key in header:
        if key == "__metadata__":
            continue
        prefixes.add(key.split(".", 1)[0])
    if "diffusion_model" in prefixes:
        return ACCEL_LOADER_CORE
    return ACCEL_LOADER_TURBO


def accel_lora_meta(key: str) -> dict | None:
    """按 key 取加速 LoRA 的元信息（固定枚与组合枚合并成一个表）。"""
    return VIDEO_ACCEL_LORAS.get(key) or VIDEO_ACCEL_LORA_COMBOS.get(key)


def all_accel_lora_keys() -> list[str]:
    """全部可选的加速 LoRA key（Turbo 系 + 组合枚）。

    顺序：Turbo 系在前（它们是 H3 的原生加速路径），组合枚在后。
    """
    return list(VIDEO_ACCEL_LORAS.keys()) + list(VIDEO_ACCEL_LORA_COMBOS.keys())


def _is_known_accel_lora_key(key: str) -> bool:
    """key 是不是本机**真实存在**的加速 LoRA 标识符。

    【为什么需要这道闸 —— 2026-11-15】
    `params["lora"]` 这条历史链路**落库时可能被塞进任意字符串**
    （Agent 把提示词误填进去）。读取侧如果不过白名单，
    这段提示词就会被：
      * 当成 LoRA 返回给工作流（污染工作流参数）；
      * 原样印在卡片「LoRA」行 ——
        用户报出的「根本就没这个 LORA」正是这个。

    【判定口径】必须在本机真实清单里：
      * VIDEO_ACCEL_LORAS / VIDEO_ACCEL_LORA_COMBOS —— 视频加速 LoRA；
      * workflows.LORAS —— 旧路径视频 LoRA（ckpt850 / v4step600）。

    清单是**权威**（不是靠形态猜）—— 所以带空格的真实 LoRA 名不会被误杀。
    """
    text = str(key or "").strip()
    if not text:
        return False
    if text in VIDEO_ACCEL_LORAS or text in VIDEO_ACCEL_LORA_COMBOS:
        return True
    try:
        from . import workflows as wf
        if text in (getattr(wf, "LORAS", {}) or {}):
            return True
    except Exception:
        pass
    return False


def resolve_accel_lora_slot(params: dict,
                            include_fixed_fallback: bool = True
                            ) -> tuple[str, float]:
    """从任务参数里解析出「加速 LoRA 槽位」。

    返回 (key, strength)。key 为空串 = 不挂加速 LoRA。

    【include_fixed_fallback 的两种用法】
      * True（工作流构建 / 提交归一用）：老任务里只有 fixed_accel_lora
        时，把那一枚当成槽位值 —— 老工作流本来就是恒挂它，
        这样老任务重放的行为不变。
      * False（**显示层**用）：老记录里 fixed_accel_lora 的语义是
        「恒生效的那枚」，而 accel_loras 才是「用户选的那枚」。
        显示层要分开呈现两者，不能把恒生效的那枚说成用户的选择 ——
        否则卡片会显示成「用户选了 ckpt850」，而用户实际选的是别的。

    【为什么需要这个函数 —— 兼容三套历史字段】
    新面板提交 `accel_lora`（单选 key）+ `accel_lora_strength`；
    老任务落库的是 `accel_loras`（数组）+ `fixed_accel_lora`（固定枚 key）；
    更老的视频路径提交的是 `lora`（"ckpt850" / "none"）。
    三者语义不同（叠加 vs 替换），必须在这里**一次性换算**，
    否则工作流构建器要同时理解三套语义 —— 那是 bug 温床。

    换算规则（**替换**语义，不是叠加）：
      * 新字段 accel_lora **存在** -> 用它（空串 = 显式「不挂加速 LoRA」）
      * 老字段 accel_loras 数组里有可用项 -> 取第一项（老面板本来就是单选）
      * 再退到 fixed_accel_lora（老任务里这是「工作流固定的那枚」）
      * 再退到 lora（"none" -> 不挂）
      * 都没有 -> 默认 ckpt850（保持老行为：新任务默认还是 4 步 Turbo）

    【为什么用「键存在」而不是「值非空」判断 accel_lora】
    空串在旧语义里表示「不挂加速 LoRA」，是一个**有效选择**。
    如果按「值非空」判断，`accel_lora=""` 会掉到兜底分支 ->
    反而挂上 ckpt850 —— 用户明明选了「不挂」，工作流里却挂着 LoRA。
    所以这里用 `"accel_lora" in params` 判断「用户显式表达过意图」。
    """
    # 【显式槽位】键存在即为用户显式选择（空串 = 不挂）
    if "accel_lora" in params:
        raw = params.get("accel_lora")
        key = str(raw or "").strip()
        if not key:
            return "", 1.0
        meta = accel_lora_meta(key) or {}
        strength = params.get("accel_lora_strength")
        if strength is None:
            strength = meta.get("recommended_strength", 1.0)
        try:
            strength = float(strength)
        except (TypeError, ValueError):
            strength = float(meta.get("recommended_strength", 1.0))
        return key, strength

    # 老格式：accel_loras 数组（取第一个启用的）
    for item in params.get("accel_loras") or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        meta = accel_lora_meta(key) or {}
        try:
            strength = float(item.get("strength",
                                meta.get("recommended_strength", 1.0)))
        except (TypeError, ValueError):
            strength = float(meta.get("recommended_strength", 1.0))
        return key, strength

    # 老格式：fixed_accel_lora（老任务里这枚是「工作流固定的那枚」）
    #
    # 【2026-11-15 修 —— 白名单闸门】与 legacy 分支同理：
    # fixed_accel_lora 同样可能被塞进脏值。
    # 脏值 -> 明确「不挂」（旧行为是挂一个不存在的文件 = 等于没挂）。
    fixed = str(params.get("fixed_accel_lora") or "").strip()
    if fixed and not _is_known_accel_lora_key(fixed):
        return "", 1.0
    if fixed and include_fixed_fallback:
        meta = VIDEO_ACCEL_LORAS.get(fixed) or {}
        strength = params.get("fixed_accel_strength")
        if strength is None:
            strength = meta.get("recommended_strength", 1.0)
        try:
            strength = float(strength)
        except (TypeError, ValueError):
            strength = float(meta.get("recommended_strength", 1.0))
        return fixed, strength

    # 更老格式：lora（"none" = 不挂）
    #
    # 【2026-11-15 修 —— 白名单闸门】
    # 旧实现：legacy 非空就返回（`if legacy and include_fixed_fallback:`），
    # **完全没过白名单**。于是 Agent 把提示词误填进 params["lora"] 时
    # （`integrated_multimodal_description: [Shot 1] ...`），
    #   * 这里把它当成一枚 LoRA 返回 ->
    #   * 工作流真的去挂它（ComfyUI 会静默丢弃，但工作流参数已被污染）
    #   * 卡片「LoRA」行原样印出这段提示词 ——
    #     这正是用户 2026-11-13 / 2026-11-15 两次报出的
    #     「卡片显示的 LoRA 根本就没这个 LORA」。
    #
    # 【脏值 -> 明确「不挂」，而不是回落默认】
    # 这条链路上「传了脏值」在语义上等于「用户没选加速 LoRA」。
    # 旧行为（脏值被当 LoRA 挂上去，ComfyUI 静默丢弃）**实际效果就是没挂**，
    # 所以显示层必须说「没挂」—— 既有测试
    # tests/test_media_card_detail.py::test_prompt_as_lora_yields_none
    # 守的就是这条。
    #
    # 【为什么不回落 ckpt850】回落会让「传了脏值」与「什么都没传」变成同一件事，
    # 但两者在旧任务里**实际跑出来是不同的**：
    #   * 什么都没传 -> 工作流挂默认 ckpt850（真的挂了）；
    #   * 传了脏值   -> 工作流挂那个不存在的文件（等于没挂）。
    # 混为一谈就是在撒谎。
    legacy = str(params.get("lora") or "").strip()
    if legacy == "none":
        return "", 1.0
    if legacy:
        if not _is_known_accel_lora_key(legacy):
            # 脏值 -> 明确「不挂」（与旧行为实际效果一致）
            return "", 1.0
        if include_fixed_fallback:
            meta = (VIDEO_ACCEL_LORAS.get(legacy)
                    or VIDEO_ACCEL_LORA_COMBOS.get(legacy) or {})
            strength = params.get("lora_strength")
            if strength is None:
                strength = meta.get("recommended_strength", 1.0)
            try:
                strength = float(strength)
            except (TypeError, ValueError):
                strength = float(meta.get("recommended_strength", 1.0))
            return legacy, strength

    # 都没有 -> 默认 Turbo ckpt850（与老行为一致）
    #
    # 【include_fixed_fallback=False 时不能给默认值】
    # 显示层用它区分「用户选了哪枚」与「工作流恒挂哪枚」：
    # 老记录里只有 fixed_accel_lora（恒生效的那枚），
    # 若这里返回默认值 ckpt850，卡片就会显示成「用户选了 ckpt850」——
    # 而用户其实什么都没选。那是谎报。
    if not include_fixed_fallback:
        return "", 1.0
    return VIDEO_FIXED_ACCEL_LORA_DEFAULT, 1.0


def model_for_mode(mode: str, preferred_key: str | None = None) -> dict:
    """按生成模式挑底模。

    【为什么需要】参考生视频必须用 **Ref2VA 权重** —— FL2VA 权重没有
    Ref2VA 通道，用它做参考生成等于「参考没生效」。
    首尾帧用 FL2VA 权重（它有 I2VA / FL2VA / L2VA 任务类型）。

    preferred_key 优先（如果它支持该模式）；否则按模式挑默认权重。
    """
    if preferred_key:
        meta = VIDEO_MODELS.get(preferred_key)
        if meta:
            if mode == "r2v" and not meta.get("supports_ref2va"):
                # 用户选的是 FL2VA 权重 -> 参考模式改用 Ref2VA 权重
                # （静默换权重 = 静默改行为，必须在摘要里说明）
                return _default_ref2va_model()
            return meta
    if mode == "r2v":
        return _default_ref2va_model()
    return VIDEO_MODELS[VIDEO_DEFAULT_MODEL]


def _default_ref2va_model() -> dict:
    """默认的 Ref2VA 权重（优先本机可用的剪枝 int8）。"""
    for key in ("ref2va_pruned_int8", "ref2va_int8",
                "hybrid_fl2va_ref2va"):
        meta = VIDEO_MODELS.get(key)
        if meta and model_status(key) == "available":
            return meta
    # 都不在 ComfyUI 列表里 -> 仍返回第一个登记项（提交时会报错说明）
    return VIDEO_MODELS.get("ref2va_pruned_int8") \
        or VIDEO_MODELS[VIDEO_DEFAULT_MODEL]


def model_status(model_key: str,
                 snapshot: ComfyModelSnapshot | None = None) -> str:
    """某个底模的当前状态（available / external_missing / missing / unknown）。

    **面板与提交共用这一个判据** —— 保证「下拉里显示的」和「提交时用的」
    是同一个结论，不会出现「下拉显示 A、提交被拒」。
    """
    meta = VIDEO_MODELS.get(model_key)
    if meta is None:
        return "unknown"
    if snapshot is None:
        snapshot = comfy_snapshot()
    return _model_status(model_key, meta, snapshot)


def model_status_note(model_key: str,
                      snapshot: ComfyModelSnapshot | None = None) -> str:
    """某个底模的如实状态说明（给界面显示）。"""
    meta = VIDEO_MODELS.get(model_key)
    if meta is None:
        return "未知的视频模型。"
    if snapshot is None:
        snapshot = comfy_snapshot()
    status = _model_status(model_key, meta, snapshot)
    return _status_note(model_key, meta, status, snapshot)


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


def options_payload(node_available=None,
                    snapshot: ComfyModelSnapshot | None = None) -> dict:
    """GET /api/video-options 的响应体。

    node_available: 可选的回调 class_type -> bool，用来把「本机 ComfyUI
    里没有这个节点」的注意力选项标成不可用（而不是让用户选完才报错）。

    【2026-11-11 新增 —— 模型清单来源的可追溯性】
    payload 里多三个字段，前端据此如实告诉用户「清单从哪来」：
      * model_list_source     "comfyui_api" | "filesystem_fallback"
      * model_list_error      连不上 ComfyUI 时的原因（空串 = 一切正常）
      * model_list_counts     {"diffusion_models": n, "loras": n}
    这样「下拉里为什么没有某个模型」永远有答案，不会再出现
    「界面说没有、API 说有」这种自相矛盾。
    """
    if snapshot is None:
        snapshot = comfy_snapshot()
    accels, combos = list_accel_loras(snapshot)
    payload = {
        "models": list_models(snapshot),
        "default_model": VIDEO_DEFAULT_MODEL,
        # 【2026-11-14 重构】加速 LoRA 统一成一组可选项。
        # fixed_accel_loras 保留这个键名（老前端读它不会炸），但内容
        # 现在是**全部**可选项（含 ckpt850 / v4step600）。
        # 另给 accel_loras 这个新键，前端优先读它。
        "accel_loras": accels,
        "fixed_accel_loras": accels,
        "accel_lora_combos": combos,
        "default_fixed_accel_lora": VIDEO_FIXED_ACCEL_LORA_DEFAULT,
        # 「不挂加速 LoRA」是显式选项，默认空（= 不挂）。
        # 【为什么默认空而不是 ckpt850】新面板不挂加速 LoRA 时走 20 步
        # 标准采样；默认给 ckpt850 会让「不挂」这个语义永远选不出来。
        "default_accel_lora": "",
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
        # ---- 模型清单来源（可追溯）----
        # 合并说明：model-list-fix 分支新增的模型清单来源信息 ——
        # 与上面的采样参数清单互不冲突，两段都保留。
        "model_list_source": "comfyui_api" if snapshot is not None
                             else "filesystem_fallback",
        "model_list_error": "" if snapshot is not None else comfy_snapshot_error(),
        "model_list_counts": {
            "diffusion_models": len(snapshot.unets) if snapshot else 0,
            "unet_enum": len(snapshot.unet_enum) if snapshot else 0,
            "loras": len(snapshot.loras) if snapshot else 0,
        },
        "comfyui_url": comfy_base_url(),
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
        # 【2026-11-13 新增 —— 借鉴 nonebot H3 工作流的 sigma 精修节点】
        # H3SigmaRefiner 本机可用性 —— 前端据此决定是否显示/启用该控件。
        payload["twopass"]["sigma_refine_node_available"] = bool(
            node_available("H3SigmaRefiner"))
    else:
        # 没传回调（单测直接调用 options_payload()）-> 按「可用」，
        # 因为该节点是 ComfyUI 核心节点，恒在。
        payload["sigma_shift_available"] = True
        # 没传回调 -> 节点可用性按「可用」处理（单测路径）。
        payload["twopass"]["sigma_refine_node_available"] = True
    return payload
