# -*- coding: utf-8 -*-
"""MiniMax-H3-Image 的模型清单、可用性探测与工作流构建。

【为什么单独成文件】
H3-Image 是**第四套**图像模型，规格与前三套都不同：
  - Z-Image      —— VAE 8 倍下采样，Turbo 蒸馏 8 步，CFG=1 无负面
  - Qwen-Image 2.1 —— VAE 16 倍下采样，40/8 步，官方 PE 扩写，10 张参考图
  - MOSS-Image   —— Flux2 latent，50 步，SLG，单文件 diffusers 目录底模
  - **H3-Image** —— VAE 16 倍下采样，**6 步 PDD 蒸馏**，CFG=1 无负面，
                    宽高必须 32 的倍数，原生 ~16MP，**最多 16 张参考图**

把它的规格 / 探测 / 构建集中在这里，而不是散进 workflows.py，
是为了让「本机能跑什么、缺什么」有一个单一真源 —— 前端拿到的
available 标记与后端跑图时的校验用的是同一份数据。

【规格来源 —— 全部来自官方文档 + 官方模板，不是推测】
1) ComfyUI 官方文档 docs.comfy.org/tutorials/image/minimax/minimax-h3-image
   - 单 checkpoint 同时做文生图与图片编辑
   - 6 步蒸馏采样，guidance 固定 1 -> 无 CFG pass -> **无负面提示词**
   - 原生 ~16MP（4096x4096 单次直出，不是放大链）
   - 每条边都是 32px 的倍数，任意宽高都接受
   - 最多 16 张参考图，提示词里用 `<Picture 1>` 起头引用
   - 官方模板采样档：euler / simple / steps=6 / cfg=1 / denoise=1
2) Comfy-Org/workflow_templates 官方模板
   - image_minimax_h3_image_t2i.json
   - image_minimax_h3_image_edit.json
3) 本机 ComfyUI 实测（2026-11-13）
   - 本机 ComfyUI 是 **0.37.0**，**没有** MiniMaxH3ImageConditioning 节点、
     没有 MiniMaxH3Image 模型类型、也没有 pdd_sigmas 的模型探测分支
     -> H3-Image 需要 ComfyUI **>= 0.45.0**（上游才有这套核心支持）
   - 权重需 4 个文件（见 H3_IMAGE_WEIGHTS）
"""
from __future__ import annotations

import math
import random
import re
from pathlib import Path

# ------------------------------------------------------------------
# 权重清单（文件名 = ComfyUI 加载器下拉里能看到的名字）
# ------------------------------------------------------------------
# 官方文档给的 4 个文件（全部来自 Comfy-Org 镜像仓库）：
#   diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors  21GB
#   text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors       27GB
#   text_encoders/qwen3.5_9b_w4a8_gptq.safetensors                    7.3GB（PE 扩写器，可选）
#   vae/minimax_h3_image_vae_fp16.safetensors                        1.17GB
#
# 【PE 扩写器是可选的】—— 官方文生图模板默认**关闭** PE（提示词原样进模型），
# 图片编辑模板默认打开。本面板默认**关闭** PE：提示词原样送进模型，
# 用户想要扩写可以自己在「增强」里做（与本项目其它模型的语义一致）。
H3_IMAGE_UNET = "minimax_h3_image_pruned_int8_convrot.safetensors"
H3_IMAGE_CLIP = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
H3_IMAGE_VAE = "minimax_h3_image_vae_fp16.safetensors"
H3_IMAGE_PE_CLIP = "qwen3.5_9b_w4a8_gptq.safetensors"

#: 权威字节数（HF x-linked-size 实测值）—— 下载中断会留下「头完整但数据没下完」
#: 的半截文件，只查 safetensors 头判定不出来（头在最前面），必须比对总字节数。
H3_IMAGE_EXPECTED_SIZES = {
    H3_IMAGE_UNET: 20979314802,
    H3_IMAGE_CLIP: 27141342152,
    H3_IMAGE_VAE: 1168505312,
    H3_IMAGE_PE_CLIP: 7343591088,
}

#: 本机 ComfyUI **必须**有的节点（H3-Image 的条件编码入口）。
#: 这个节点在 ComfyUI >= 0.45.0 才有 —— 0.37.0 上跑不了。
H3_IMAGE_REQUIRED_NODES = ("MiniMaxH3ImageConditioning",)

# ------------------------------------------------------------------
# 模型版本
# ------------------------------------------------------------------
# 【为什么只有一个版本】官方目前只发布了一个 H3-Image checkpoint
# （pruned int8 convrot），没有 bf16 / turbo / 其它量化版本。
# 所以模型清单只有一个条目，但**保留 dict 结构** —— 以后出别的版本时
# 只需往这里加一行，前端与后端都不用改结构。
H3_IMAGE_MODELS = {
    "h3_image_pruned_int8": {
        "label": "MiniMax-H3-Image · 官方 6 步蒸馏版（INT8）",
        "unet": H3_IMAGE_UNET,
        "clip": H3_IMAGE_CLIP,
        "vae": H3_IMAGE_VAE,
        "weight_dtype": "default",
        # 官方模板采样档（image_minimax_h3_image_t2i.json 的 KSampler）
        "sampler": "euler",
        "scheduler": "simple",
        "steps": 6,
        # guidance 固定 1 -> 没有 CFG pass -> 负面提示词数学上无效
        "cfg": 1.0,
        "shift": 1.0,
        "negative": "zero",
        "supports_negative": False,
        "samplers": ["euler"],
        "schedulers": ["simple"],
        "note": "官方模板：6 步 / cfg 1 / euler / simple / denoise 1。",
        "warn": "H3-Image 是 6 步蒸馏版：步数与 PDD sigma 表绑定，"
                "且 guidance 固定 1（无 CFG pass），**负面提示词不生效** —— "
                "要把「不要什么」写进正面提示词里。",
    },
}
H3_IMAGE_DEFAULT_MODEL = "h3_image_pruned_int8"

# ------------------------------------------------------------------
# 分辨率档位
# ------------------------------------------------------------------
# 【关键差异】H3-Image 的宽高必须是 **32 的倍数**（不是 Z-Image / Qwen 的 16）。
# 官方文档原文："Every edge is generated on a multiple of 32 px"。
# 官方模板的 ResizeImageMaskNode 也是 "scale to multiple", 32。
# 落错倍数会让 ComfyUI 拒绝或静默改写尺寸。
H3_IMAGE_SIZE_MULTIPLE = 32

#: 原生 ~16MP（官方文档：4096x4096 单次直出，不走放大链）。
#: 官方模板默认开 **4MP**（1:1 时 2048x2048）—— 官方注释原文：
#: "The template opens at 4 MP on a square canvas, deliberately below the
#:  model's native resolution so low-VRAM machines do not run out of memory."
#: 本面板默认也走 4MP 档（低显存机器跑得动），并把 16MP 作为可选上沿。
H3_IMAGE_RES_TIERS = {
    "4mp": 4.0,         # 官方模板默认档（1:1 -> 2048x2048）
    "8mp": 8.0,
    "16mp": 16.0,       # 原生上限（1:1 -> 4096x4096，单次直出）
}
H3_IMAGE_DEFAULT_TIER = "4mp"

#: 原生直出上限：16MP。超过就是「超出模型原生能力」——
#: 官方没有给放大链路径（不像 Z-Image 有官方 2K 放大模板）。
H3_IMAGE_MAX_DIRECT_MP = 16.0
#: 下限：官方模板 4MP 起；但 1MP 也能跑（只是不在「原生区间」）。
H3_IMAGE_MIN_MP = 1.0

#: 官方文档给的「原生 16MP 各比例」尺寸表（宽x高，32 的倍数）。
#: 参考图编辑时官方模板按**源图尺寸**走，不强制套这个表。
H3_IMAGE_NATIVE_SIZES = {
    "1:1": (4096, 4096),
    "4:3": (4736, 3552),
    "3:4": (3552, 4736),
    "3:2": (5024, 3360),
    "2:3": (3360, 5024),
    "16:9": (5472, 3072),
    "9:16": (3072, 5472),
    "21:9": (6272, 2688),
}

# ------------------------------------------------------------------
# 参考图
# ------------------------------------------------------------------
#: 官方文档："up to 16 input images, addressed as `<Picture 1>` upwards"。
#: ComfyUI 节点侧是 io.Autogrow（min=0, max=100），官方模板接了 5 个槽。
#: 落地上限取官方能力 16 张。
H3_IMAGE_MAX_REF_IMAGES = 16
H3_IMAGE_MIN_REF_IMAGES = 1

#: 官方占位标记是 `<Picture N>`（**不是** Qwen 的 `<imageN>`）。
#: ComfyUI 侧 comfy/text_encoders/minimax.py 的 MiniMaxH3Tokenizer
#: tokenize_with_weights 会自动为每张图插入 "<Picture %d>: " 前缀，
#: 用户在提示词里写 `<Picture 1>` 即可引用。
H3_IMAGE_PLACEHOLDER = "<Picture {n}>"

#: 占位标记的正则（用于校验提示词里引用了哪几张参考图）。
H3_IMAGE_PLACEHOLDER_RE = re.compile(r"<Picture\s*(\d+)\s*>", re.IGNORECASE)


def h3_image_placeholders(text: str) -> list[int]:
    """解析提示词里出现的 `<Picture N>` 序号（1 基，已去重并升序）。"""
    found = {int(m) for m in H3_IMAGE_PLACEHOLDER_RE.findall(text or "")}
    return sorted(found)


def validate_h3_image_refs(refs: list[dict], prompt: str = "") -> dict:
    """H3-Image 参考图一致性校验（与 ref_imgs.validate_refs 同结构）。

    【为什么不能直接复用 ref_imgs.validate_refs】
    Qwen-Image 2.1 的官方 PE-I2I 模板用 `<image1>..<imageN>`；
    H3-Image 的官方文档用 `<Picture 1>..<Picture N>`。两套标记语法不同，
    拿 Qwen 那套来查 H3-Image，会把「用户写了 <Picture 1>」的合法提示词
    判成「缺少 <image1> 占位标记」-> 误拒用户的合法提交。

    检查项：
      1. 数量在 [1, 16] 内
      2. 每项都锚定到素材库 asset_id
      3. 没有失效素材（missing）
      4. 提示词里的 `<Picture N>` 编号与参考图数量一致（1..N 连续）

    返回 {"ok": bool, "errors": [...], "warnings": [...]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    count = len(refs)
    if count < H3_IMAGE_MIN_REF_IMAGES:
        errors.append(f"参考图至少需要 {H3_IMAGE_MIN_REF_IMAGES} 张，当前 {count} 张")
    if count > H3_IMAGE_MAX_REF_IMAGES:
        errors.append(f"参考图最多 {H3_IMAGE_MAX_REF_IMAGES} 张，当前 {count} 张")

    for index, ref in enumerate(refs):
        if not ref.get("asset_id"):
            errors.append(f"第 {index + 1} 张参考图没有素材库 ID（参考图必须来自素材库）")
        if ref.get("missing"):
            errors.append(
                f"第 {index + 1} 张参考图对应的素材已从素材库删除"
                f"（{ref.get('name') or ref.get('asset_id')}），请移除后再生成")

    # 【占位标记是「建议」不是「必须」—— 只拦悬空引用】
    # 官方文档原文：the prompt addresses each one by its label。
    # 也就是说 `<Picture N>` 是**推荐**的引用方式，不是硬性语法要求。
    # 用户完全可能写「把图1里的猫换成雪豹」这种自然语言 ——
    # 强制要求写全 <Picture 1>..<Picture N> 会把这类**合法提交**误拒。
    #
    # 【与 Qwen 的区别】Qwen-Image 2.1 的官方 PE-I2I system prompt 明确写
    # "the rewritten instruction MUST use <image1>, <image2>, ..."，
    # 那是**强制**的（多图时），所以 Qwen 走 ref_imgs.validate_refs 的严格版。
    # 两套语义不同，这里保持隔离。
    placeholders = h3_image_placeholders(prompt)
    if count >= 2:
        expected = set(range(1, count + 1))
        # 只拦「引用了不存在的第 N 张」（悬空引用）
        extra = sorted(set(placeholders) - expected)
        if extra:
            detail = "、".join(H3_IMAGE_PLACEHOLDER.format(n=m) for m in extra)
            errors.append(
                f"提示词里引用了不存在的参考图：{detail}"
                f"（当前只有 {count} 张参考图）")
        elif not placeholders:
            warnings.append(
                f"有 {count} 张参考图，提示词里没有写 "
                + H3_IMAGE_PLACEHOLDER.format(n=1)
                + " 这样的引用标记 —— 可以用，但建议写上，"
                  "模型才能明确知道每张图对应哪个对象")
    elif count == 1 and placeholders:
        warnings.append("只有 1 张参考图，可以直接用自然表述描述它"
                        "（<Picture 1> 标记是可选的）")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ------------------------------------------------------------------
# 尺寸计算
# ------------------------------------------------------------------
def h3_image_dimensions(aspect: str, tier: str, width=None, height=None):
    """比例 + 分辨率档位 -> 实际像素宽高（H3-Image）。

    算法与 z_image_dimensions / qwen_image_dimensions 一致（同一套
    ResolutionSelector 语义），差别只有两点：
      1. 档位表是 H3-Image 官方推荐区间（4 / 8 / 16MP）
      2. 倍数是 **32**（不是 16）
    """
    aspect_num = {
        "1:1": (1, 1), "2:3": (2, 3), "3:2": (3, 2), "3:4": (3, 4),
        "4:3": (4, 3), "9:16": (9, 16), "16:9": (16, 9), "21:9": (21, 9),
    }
    w_ratio, h_ratio = aspect_num.get(aspect, (16, 9))
    megapixels = H3_IMAGE_RES_TIERS.get(
        tier, H3_IMAGE_RES_TIERS[H3_IMAGE_DEFAULT_TIER])
    mult = H3_IMAGE_SIZE_MULTIPLE

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


def normalize_h3_image_size(width, height, multiple=H3_IMAGE_SIZE_MULTIPLE,
                            min_megapixels=H3_IMAGE_MIN_MP,
                            max_megapixels=H3_IMAGE_MAX_DIRECT_MP):
    """把用户手填的宽高吸附到 H3-Image 的合法值。

    返回 (生效宽, 生效高, 调整原因列表)。**不抛异常** —— 界面要拿
    「原始值 / 生效值 / 调整原因」三样东西一起显示。

    吸附规则（依据全部来自官方文档 + 官方模板）：
      1. 宽高必须是 32 的倍数
         （官方文档 "Every edge is generated on a multiple of 32 px"；
          官方模板 ResizeImageMaskNode 的 multiple 也是 32）
      2. 单边 32 ~ 16384（节点 schema 的 min / nodes.MAX_RESOLUTION）
      3. 总像素不得低于 1MP
      4. 总像素不得高于原生直出上限 16MP

    原因列表是给人看的短句，直接显示在界面上。
    """
    reasons: list[str] = []
    try:
        w = int(round(float(width)))
        h = int(round(float(height)))
    except (TypeError, ValueError):
        return 0, 0, ["宽高必须是数字"]
    if w <= 0 or h <= 0:
        return 0, 0, ["宽高必须大于 0"]

    # -- 1. 倍数吸附 --
    adj_w = max(multiple, int(round(w / multiple)) * multiple)
    adj_h = max(multiple, int(round(h / multiple)) * multiple)
    if adj_w != w:
        reasons.append(f"宽度 {w} 不是 {multiple} 的倍数 -> {adj_w}")
    if adj_h != h:
        reasons.append(f"高度 {h} 不是 {multiple} 的倍数 -> {adj_h}")
    w, h = adj_w, adj_h

    # -- 2. 范围钳制 --
    size_max = 16384
    if w > size_max:
        reasons.append(f"宽度超过上限 {size_max} -> 已钳到上限")
        w = size_max
    if h > size_max:
        reasons.append(f"高度超过上限 {size_max} -> 已钳到上限")
        h = size_max
    if w < multiple:
        reasons.append(f"宽度低于下限 {multiple} -> 已抬到下限")
        w = multiple
    if h < multiple:
        reasons.append(f"高度低于下限 {multiple} -> 已抬到下限")
        h = multiple

    # -- 3. 总像素下限 --
    if min_megapixels:
        min_pixels = min_megapixels * 1024 * 1024
        if w * h < min_pixels:
            scale = math.sqrt(min_pixels / float(w * h))
            nw = max(multiple, int(round(w * scale / multiple)) * multiple)
            nh = max(multiple, int(round(h * scale / multiple)) * multiple)
            reasons.append(
                f"总像素 {w * h / (1024 * 1024):.2f}MP 低于下限 "
                f"{min_megapixels}MP -> 按比例放大到 {nw}×{nh}")
            w, h = nw, nh

    # -- 4. 总像素上限（原生 16MP 直出上限） --
    if max_megapixels:
        max_pixels = max_megapixels * 1024 * 1024
        if w * h > max_pixels:
            scale = math.sqrt(max_pixels / float(w * h))
            nw = max(multiple, int(round(w * scale / multiple)) * multiple)
            nh = max(multiple, int(round(h * scale / multiple)) * multiple)
            reasons.append(
                f"总像素 {w * h / (1024 * 1024):.2f}MP 超过原生直出上限 "
                f"{max_megapixels}MP -> 按比例缩到 {nw}×{nh}")
            w, h = nw, nh
    return w, h, reasons


# ------------------------------------------------------------------
# 可用性探测
# ------------------------------------------------------------------
def _model_dirs():
    """本机 ComfyUI 的模型目录列表（含 extra_model_paths.yaml 里的外接盘）。"""
    dirs = []
    try:
        from . import loras as lora_registry
        dirs = list(lora_registry._model_dirs())
    except Exception:
        dirs = []
    if not dirs:
        dirs = [Path("/home/ubuntu/comfyui/models")]
    return [Path(d) for d in dirs]


def _safetensors_complete(path: Path) -> bool:
    """safetensors 文件是否完整（头自洽 + 已知文件比对权威字节数）。"""
    try:
        size = path.stat().st_size
        if size < 16:
            return False
        with open(path, "rb") as fh:
            head = fh.read(8)
        if len(head) < 8:
            return False
        header_len = int.from_bytes(head, "little")
        if header_len <= 0 or header_len > size - 8:
            return False
        expected = H3_IMAGE_EXPECTED_SIZES.get(path.name)
        if expected is not None and size != expected:
            return False
        return True
    except OSError:
        return False


def h3_image_weight_available(filename: str) -> bool:
    """权重文件在本机是否存在**且完整**。

    「存在但没下完」是最坑的一种情况 —— ComfyUI 加载器能选到它，
    但跑图时才炸。所以这里必须比对总字节数。
    """
    if not filename:
        return False
    for root in _model_dirs():
        for sub in ("diffusion_models", "unet", "checkpoints", "vae",
                    "text_encoders", "clip"):
            try:
                path = root / sub / filename
                if not path.exists():
                    continue
                if _safetensors_complete(path):
                    return True
            except OSError:
                continue
    return False


def missing_weights(model: dict) -> list[str]:
    """列出缺失（或没下完）的权重文件名 + 人类可读的原因。"""
    missing = []
    for key, label in (("unet", "扩散模型 DiT"),
                       ("clip", "文本编码器 Qwen3-VL-32B"),
                       ("vae", "图像 VAE")):
        name = model.get(key) or ""
        if not name:
            missing.append(f"{label}：未在模型清单中登记")
        elif not h3_image_weight_available(name):
            missing.append(f"{label}：{name}（未在本机 ComfyUI 模型目录找到，或文件未下完）")
    return missing


def h3_image_capability(nodes_available, model_available: bool,
                         missing: list[str]) -> dict:
    """给前端的「能不能用」结论。

    两道独立的门：
      * 节点门 —— 本机 ComfyUI 有没有 MiniMaxH3ImageConditioning
      * 权重门 —— 权重文件在不在（且下完）

    【nodes_available 可以是 None】
    None = **探测没问全**（ComfyUI 忙时 /object_info 超时）。
    这与 False（确认不在）必须区分开：
      * False -> 确实缺节点，可以断言「本机 ComfyUI 版本过低」
      * None  -> 不知道，不能断言 —— 界面应显示「暂时无法探测」

    但**权重结论不受影响**：权重探测是纯磁盘操作（比对字节数），
    永远可靠。所以节点探测失败时，权重那部分照样如实报告。

    任一不过就**明确告诉用户缺什么**，而不是让前端静默失败。
    """
    reasons = []
    node_confirmed_missing = (nodes_available is False)
    if node_confirmed_missing:
        reasons.append(
            "本机 ComfyUI 缺少 H3-Image 核心节点 MiniMaxH3ImageConditioning"
            "（该节点需要 ComfyUI >= 0.45.0；本机版本过低）")
    reasons.extend(missing)
    return {
        # None = 没问全；False = 确认不在；True = 在
        "nodes_available": nodes_available,
        "weights_available": bool(model_available),
        # 只有「节点确认缺失」或「权重缺失」才算不可用。
        # 节点探测没问全时不能断言不可用 —— 但也不能说可用。
        "available": bool(model_available and nodes_available is True),
        "missing": reasons,
    }


# ------------------------------------------------------------------
# 工作流构建
# ------------------------------------------------------------------
def build_h3_image_workflow(params: dict, ref_files: list[str] | None = None,
                             job_id: str = "") -> dict:
    """H3-Image 文生图 / 图片编辑的工作流（ComfyUI API 格式）。

    结构对齐官方 image_minimax_h3_image_edit.json：

        UNETLoader(h3_image DiT) ──────────────────────────┐
        CLIPLoader(qwen3vl_32b, type=minimax) ──┐         │
        VAELoader(h3_image vae) ──┐             │         │
                                          │   │         │
        MiniMaxH3ImageConditioning(clip, vae, prompt, width, height, images.image_N)
              ├─ positive(CONDITIONING) ───────┴─────────> KSampler.positive
              └─ LATENT ─────────────────────────────────> KSampler.latent_image
        ConditioningZeroOut(positive) ────────────────────> KSampler.negative
        KSampler → VAEDecode → SaveImage

    【为什么 negative 接 ConditioningZeroOut 而不是 CLIPTextEncode】
    官方模板就是这么接的：H3-Image 的 guidance 固定 1，没有 CFG pass，
    负面分支只是占位（ComfyUI 在 cfg=1 时根本不会跑它）。
    用 ConditioningZeroOut（零条件）而不是 CLIPTextEncode（真去编码一段
    负面文本）—— 后者会白跑一次 Qwen3-VL 的 32B 编码，纯浪费。

    【参考图接线：扁平点号键】
    images 是 io.Autogrow（prefix="image_", min=0, max=100），
    ComfyUI 在 execution.py 里按 schema 的 dynamic_paths 把**扁平点号键**
    还原成嵌套 dict 传给 execute()。所以必须写
    `"images.image_1": ["node", 0]` 而不是 `"images": {"image_1": [...]}`。
    写错不报错、不告警，只是参考图被静默丢弃（与 Qwen-Image 2.1 同款坑，
    见 workflows.build_qwen_i2i 的注释）。
    """
    model_key = params.get("model") or H3_IMAGE_DEFAULT_MODEL
    if model_key not in H3_IMAGE_MODELS:
        model_key = H3_IMAGE_DEFAULT_MODEL
    model = H3_IMAGE_MODELS[model_key]

    # 【前置校验：节点缺失时给中文错误，而不是让 ComfyUI 报英文原文】
    # 没有这道闸的话，用户看到的是：
    #   ComfyUI 提交失败: {"error": {"type": "missing_node_type",
    #   "message": "Node 'MiniMaxH3ImageConditioning' not found..."}}
    # —— 英文 + JSON 噪音，用户根本不知道「这是 ComfyUI 版本不够」，
    # 更不知道该做什么（升级 ComfyUI 还是装插件）。
    #
    # params["_h3image_nodes_available"] 由服务端在提交前探测并注入
    # （worker 的 refresh_comfy_node_names 机制）；缺这个键 = 没探测过
    # -> 不拦（宁可让 ComfyUI 自己报错，也不要误报）。
    if params.get("_h3image_nodes_available") is False:
        raise ValueError(
            "本机 ComfyUI 缺少 H3-Image 核心节点 "
            "MiniMaxH3ImageConditioning，无法运行 H3-Image。\n"
            "原因：该节点需要 ComfyUI >= 0.45.0（本机版本过低）。\n"
            "解决：升级 ComfyUI 到 0.45.0+，或安装提供该节点的插件，"
            "然后重试。")

    seed = params.get("seed") or random.randint(0, 2 ** 48)
    aspect = params.get("aspect", "16:9")
    if aspect not in H3_IMAGE_NATIVE_SIZES:
        aspect = "16:9"
    tier = params.get("resolution")
    if tier not in H3_IMAGE_RES_TIERS:
        tier = H3_IMAGE_DEFAULT_TIER

    raw_w, raw_h = params.get("width"), params.get("height")
    if raw_w and raw_h:
        width, height, _ = normalize_h3_image_size(raw_w, raw_h)
    else:
        width, height = h3_image_dimensions(aspect, tier)

    sampler = params.get("sampler") or model["sampler"]
    scheduler = params.get("scheduler") or model["scheduler"]
    steps = int(params.get("steps") or model["steps"])
    cfg = float(params.get("cfg") if params.get("cfg") is not None else model["cfg"])

    prompt = params.get("prompt", "") or ""

    p: dict = {
        "h3i:1": {"inputs": {"unet_name": model["unet"],
                             "weight_dtype": model["weight_dtype"]},
                  "class_type": "UNETLoader"},
        "h3i:2": {"inputs": {"clip_name": model["clip"], "type": "minimax",
                             "device": "default"},
                  "class_type": "CLIPLoader"},
        "h3i:3": {"inputs": {"vae_name": model["vae"]},
                  "class_type": "VAELoader"},
        # 条件编码：提示词 + 参考图 -> 正面条件 + 图像 latent
        "h3i:4": {"inputs": {"clip": ["h3i:2", 0], "vae": ["h3i:3", 0],
                             "prompt": prompt,
                             "width": width, "height": height},
                  "class_type": "MiniMaxH3ImageConditioning"},
        # 负面分支：guidance=1 时不跑，这里用零条件占位（官方模板同款）
        "h3i:5": {"inputs": {"conditioning": ["h3i:4", 0]},
                  "class_type": "ConditioningZeroOut"},
        "h3i:6": {"inputs": {"seed": int(seed), "steps": steps, "cfg": cfg,
                             "sampler_name": sampler, "scheduler": scheduler,
                             "denoise": 1,
                             "model": ["h3i:1", 0],
                             "positive": ["h3i:4", 0],
                             "negative": ["h3i:5", 0],
                             "latent_image": ["h3i:4", 1]},
                  "class_type": "KSampler"},
        "h3i:7": {"inputs": {"samples": ["h3i:6", 0], "vae": ["h3i:3", 0]},
                  "class_type": "VAEDecode"},
        "h3i:8": {"inputs": {"filename_prefix": f"h3studio/h3image_{job_id}",
                             "images": ["h3i:7", 0]},
                  "class_type": "SaveImage"},
    }

    # ---- 参考图接线（图片编辑）----
    # 顺序即语义：列表下标 i 对应 images.image_{i+1}，也对应提示词里的
    # <Picture i+1> —— 三者必须严格一致。
    refs = list(ref_files or [])
    for index, filename in enumerate(refs[:H3_IMAGE_MAX_REF_IMAGES]):
        node_id = f"h3i:ref{index}"
        p[node_id] = {"inputs": {"image": filename}, "class_type": "LoadImage"}
        p["h3i:4"]["inputs"][f"images.image_{index + 1}"] = [node_id, 0]

    return p


def build_h3_image_t2i(params: dict) -> dict:
    """H3-Image 文生图（无参考图）。"""
    return build_h3_image_workflow(params, ref_files=None,
                                    job_id=params.get("job_id", ""))


def build_h3_image_i2i(params: dict) -> dict:
    """H3-Image 图片编辑（1~16 张参考图）。

    参考图来源：params["ref_images"] = ["h3studio/x.png", ...]
    文件名必须已存在于 ComfyUI input（服务端先把素材库文件上传过去）。
    """
    refs = params.get("ref_images") or []
    if not isinstance(refs, list):
        refs = []
    # 两种形态都收：文件名列表 / {"file": name} 字典列表
    files = []
    for ref in refs:
        if isinstance(ref, dict):
            filename = str(ref.get("file") or ref.get("filename") or "")
        else:
            filename = str(ref or "")
        if filename:
            files.append(filename)

    if len(files) < H3_IMAGE_MIN_REF_IMAGES:
        raise ValueError(
            f"H3-Image 图片编辑至少需要 {H3_IMAGE_MIN_REF_IMAGES} 张参考图，"
            f"当前 {len(files)} 张")
    if len(files) > H3_IMAGE_MAX_REF_IMAGES:
        raise ValueError(
            f"H3-Image 参考图最多 {H3_IMAGE_MAX_REF_IMAGES} 张，"
            f"当前 {len(files)} 张（官方能力上限 16 张）")

    return build_h3_image_workflow(params, ref_files=files,
                                    job_id=params.get("job_id", ""))
