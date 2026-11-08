"""Z-Image LoRA 的「context_refiner 阻尼」派生器。

为什么需要这个（2026-11-08 实测定位）：

「瑶光」Kook_Zimage_瑶光.safetensors 是**合法的 Z-Image LoRA**（238/238 模块
attach 成功、0 漏载、0 报错），但在强度 1.0 时配合长中文结构化提示词，
出图会变成**彩色噪点**。定位到的真正原因是：

    该 LoRA 对 **context_refiner** 模块的相对扰动远高于其它模块：

        瑶光     context_refiner  max 12.85% / median 9.62%
        如梦似幻 context_refiner  max  1.36% / median 0.74%

    Z-Image 的 context_refiner 是**联合处理「文本条件 + 潜变量」**的模块
    （attention 拿到的是文本条件，feed_forward 拿到的是拼接后的联合表示）。
    文本条件越复杂/越长，这个模块的输入越偏离训练分布；再叠加 10% 量级的
    权重扰动，采样就会发散成彩色噪点。

    消融实验（同一提示词 / 同一种子 / 强度 1.0）：

        变体                       base      tmpl      tmpl+瑶光
        原始（不阻尼）              14.5 噪   14.6 噪   14.6 噪
        context_refiner × 0.5       8.6 ok    8.5 ok    8.4 ok
        context_refiner × 0.25      8.2 ok    7.8 ok    7.8 ok
        完全去掉 context_refiner     8.2 ok    7.8 ok    7.7 ok

    即：**只阻尼 context_refiner 就能消掉噪点，且 LoRA 的风格效果仍在**
    （阻尼后 vs 无 LoRA 的 MAE 仍有 30+）。

为什么不简单地「全局降强度」：
    全局降到 0.8 只是把阈值从「扰动幅度」挪到「扰动幅度 × 强度」，
    而崩不崩还取决于提示词的文本条件复杂度 —— 同一个 0.8，
    对 700 字模板提示词安全，对某些收尾方式仍会崩（实测存在刀刃效应）。
    阻尼 context_refiner 是**针对根因**的修法。

产出的派生文件：
    在 ComfyUI 的 loras 目录下写一份 `h3studio_<name>_ctx<k>.safetensors`，
    内容 = 原文件，但 context_refiner.* 的 lora_A / lora_B 乘以阻尼系数 k
    （dW = B@A，缩放 A 或 B 都等效于把该模块的 dW 缩放 k 倍）。
    工作流里引用这份派生文件，强度保持用户选的 1.0。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 阻尼系数 —— 实测标定：
#   k=0.9 仍有提示词会崩（base 模板 14.5 噪点）
#   k=0.8 全部 6 个提示词 x 5 个种子全干净
#   k=0.5 更保守但会损失部分风格强度
DEFAULT_CTX_DAMPING = 0.8

# 判定「需要阻尼」的 context_refiner 相对扰动阈值。
# 低于该值说明这个 LoRA 扰动 context_refiner 很轻（如梦似幻 0.74%），不需要阻尼。
CTX_DAMPING_TRIGGER = 0.03   # 3%

# 派生文件名前缀（用于识别/清理自己的产物）
DERIVED_PREFIX = "h3studio_ctx_"


def _damped_name(original: str, factor: float) -> str:
    """派生文件名：h3studio_ctx_<factor>__<原名>（原名保留子目录结构用 __ 代替 /）"""
    flat = original.replace("/", "__")
    pct = int(round(factor * 100))
    return f"{DERIVED_PREFIX}{pct}__{flat}"


def _ctx_damping_needed(lora_path: Path, unet_path: Path | None) -> float | None:
    """返回该 LoRA 的 context_refiner 相对扰动（最大值），读不到返回 None。"""
    try:
        import torch
        from safetensors.torch import load_file
    except Exception:
        return None
    try:
        lora_sd = load_file(str(lora_path))
        if unet_path is None:
            return None
        unet_sd = load_file(str(unet_path))
    except Exception:
        return None

    def _pair(base: str):
        a_key = f"{base}.lora_A.default.weight"
        b_key = f"{base}.lora_B.default.weight"
        if a_key in lora_sd and b_key in lora_sd:
            return lora_sd[a_key].float(), lora_sd[b_key].float()
        return None

    worst = 0.0
    found = False
    # context_refiner 的 feed_forward（w1/w2/w3）与底模同名 1:1 对应，
    # attention 是融合 qkv 需切片，量级结论一致，这里只量 FFN（最准且最省）。
    for layer in ("context_refiner.0", "context_refiner.1"):
        for ff in ("w1", "w2", "w3"):
            base = f"{layer}.feed_forward.{ff}"
            pair = _pair(base)
            if pair is None:
                continue
            a, b = pair
            wkey = f"{base}.weight"
            if wkey not in unet_sd:
                continue
            try:
                delta = b @ a
            except Exception:
                continue
            weight = unet_sd[wkey].float()
            if delta.shape != weight.shape:
                continue
            denom = float(weight.norm().item())
            if denom <= 0:
                continue
            rel = float(delta.norm().item()) / denom
            worst = max(worst, rel)
            found = True
    return worst if found else None


def _write_damped(lora_path: Path, target: Path, factor: float) -> bool:
    """写出阻尼后的派生 LoRA。成功返回 True。"""
    try:
        from safetensors.torch import load_file, save_file
    except Exception:
        return False
    try:
        src = load_file(str(lora_path))
    except Exception:
        return False
    out = {}
    for key, value in src.items():
        if key.startswith("context_refiner."):
            try:
                value = (value.float() * factor).to(value.dtype)
            except Exception:
                return False
        out[key] = value
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        save_file(out, str(target), metadata={"format": "pt"})
    except Exception:
        return False
    return True


def cleanup_stale_derived(lora_dirs: list[Path] | None = None) -> int:
    """删除阻尼系数与当前标定不一致的旧派生文件。

    派生文件名里带阻尼系数（h3studio_ctx_80__xxx）—— 改了系数后旧文件
    就是垃圾，留着会让 ComfyUI 的 lora 下拉多出几份同名不同参的副本。
    返回删除数量。
    """
    from . import loras as lora_registry

    if lora_dirs is None:
        lora_dirs = lora_registry.comfyui_lora_dirs()
    current = f"{DERIVED_PREFIX}{int(round(DEFAULT_CTX_DAMPING * 100))}__"
    removed = 0
    for d in lora_dirs:
        try:
            candidates = list(d.glob(f"{DERIVED_PREFIX}*"))
        except Exception:
            continue
        for path in candidates:
            if not path.is_file():
                continue
            if path.name.startswith(current):
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def _is_damped(path: Path, factor: float, source: Path | None = None) -> bool:
    """校验派生文件确实做了阻尼。

    只看文件名不够 —— 早期版本或中断的写入会留下「名字对、内容没阻尼」的
    陈旧文件，ComfyUI 会照常加载它，结果就是噪点回归（真实踩过：
    14:47 留下的派生文件 ratio=1.0，出图仍是彩色噪点）。

    判定：抽查若干 context_refiner 的 lora_A / lora_B，
    它们的范数应约等于源文件的 factor 倍。
    """
    try:
        from safetensors.torch import load_file
    except Exception:
        return False
    if source is None:
        return False
    try:
        dst = load_file(str(path))
        src = load_file(str(source))
    except Exception:
        return False

    checked = 0
    for key in dst:
        # 注意：LoRA 键名是 context_refiner.N.*（无前导点）
        if "context_refiner." not in key:
            continue
        if key not in src:
            return False
        try:
            dn = dst[key].float().norm().item()
            sn = src[key].float().norm().item()
        except Exception:
            return False
        if sn <= 0:
            continue
        if abs(dn / sn - factor) > 0.02:
            return False
        checked += 1
    return checked > 0


def ensure_damped_lora(name: str, factor: float = DEFAULT_CTX_DAMPING,
                       lora_dirs: list[Path] | None = None) -> tuple[str, Path | None]:
    """确保存在一份 context_refiner 已阻尼的派生 LoRA。

    返回 (ComfyUI lora_name, 绝对路径)；不需要阻尼或失败时返回 ("", None)，
    调用方应回退到原始 LoRA。
    """
    from . import loras as lora_registry

    if lora_dirs is None:
        lora_dirs = lora_registry.comfyui_lora_dirs()
    if not lora_dirs:
        return "", None

    # 已经是派生文件了 —— 不能再阻尼一次（否则会套娃出 ctx_80__ctx_80__...）
    if Path(name).name.lower().startswith(DERIVED_PREFIX):
        return "", None

    resolved, path = lora_registry.resolve_lora(name)
    if not resolved or path is None:
        return "", None
    # 原始文件已经是派生文件 -> 不再套娃
    if path.name.lower().startswith(DERIVED_PREFIX):
        return "", None

    # 只有扰动够大才需要阻尼
    unet = lora_registry._z_image_unet_path()
    rel = _ctx_damping_needed(path, unet)
    if rel is None or rel < CTX_DAMPING_TRIGGER:
        return "", None

    derived_name = _damped_name(resolved, factor)
    # 优先写进第一个可写的 lora 目录
    target = None
    for d in lora_dirs:
        candidate = d / derived_name
        if candidate.exists():
            # 已存在的派生文件**必须校验内容**：早期版本 / 系数变更会留下
            # 「名字对但没真正阻尼」的陈旧文件，直接复用会导致噪点回归。
            if _is_damped(candidate, factor, source=path):
                return derived_name, candidate
            try:
                candidate.unlink()
            except OSError:
                pass
            target = candidate
            break
        try:
            if os.access(d, os.W_OK):
                target = candidate
                break
        except OSError:
            continue
    if target is None:
        return "", None

    if _write_damped(path, target, factor):
        return derived_name, target
    return "", None
