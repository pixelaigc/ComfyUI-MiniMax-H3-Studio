# -*- coding: utf-8 -*-
"""
MOSS-Image LoRA 的**归一化派生器**（前缀归一 + 融合 qkv 拆分）。

## 为什么需要它

MOSS-Image 底模的注意力投影是**分离**的 `proj.{q,k,v,o}`
（`custom_nodes/moss-image/model.py` 的 `_Block` 用 `nn.ModuleDict`）。
本机 148 个 MOSS 系 LoRA 实测有 4 类形态会静默失效：

| 形态 | 条数 | 修法 |
|---|---|---|
| 融合 qkv | 33 | 拆分成 q/k/v（无损，已验证） |
| `lora_unet_` 前缀 | 11 | 前缀归一 |
| 裸 `layers.` 前缀 | 12 | 前缀归一 |
| `transformer.` 前缀 | 1 | 前缀归一 |

全量实测：90 个原生可用 / 33 融合 qkv / 11 lora_unet_ / 13 裸 layers. / 1 transformer.

本模块把可修的全部落成**派生文件**，让 ComfyUI 真的挂上；
不可修的（转换后仍有缺失）明确报错，不静默提交一个必然失效的 LoRA。

产物命名：`h3studio_mossnorm__<原名>`（原名的 `/` 换成 `__`），
放在**同一个 LoRA 目录**里（ComfyUI 的 lora 下拉按目录递归扫）。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import moss_lora_compat as compat

#: 派生文件名前缀（用于识别/清理自己的产物）
DERIVED_PREFIX = "h3studio_mossnorm__"


def derived_name(original: str) -> str:
    """派生文件名：h3studio_mossnorm__<原名扁平化>"""
    return f"{DERIVED_PREFIX}{original.replace('/', '__')}"


def _load_state(path: Path):
    try:
        from safetensors.torch import load_file
    except Exception:
        return None
    try:
        return load_file(str(path))
    except Exception:
        return None


def _save_state(state: dict, path: Path, metadata: dict | None) -> bool:
    try:
        from safetensors.torch import save_file
    except Exception:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        if metadata:
            save_file(state, str(tmp), metadata=metadata)
        else:
            save_file(state, str(tmp))
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            leftover = path.with_suffix(path.suffix + ".tmp")
            if leftover.exists():
                leftover.unlink()
        except Exception:
            pass
        return False


def normalize_lora(lora_path, target_dir=None) -> dict:
    """把 LoRA 归一成 ComfyUI 认的形态并落盘。

    返回 dict：
      ok          是否成功产出
      target      产出路径（失败时 None）
      changed     是否真的做了转换（False = 原文件本来就对）
      unhandled   没能转换的张量名（列表）
      reason      失败原因
      reused      目标文件已存在且有效（直接复用）
    """
    src = Path(lora_path)
    result = {"ok": False, "target": None, "changed": False,
              "unhandled": [], "reason": "", "reused": False}
    if not src.is_file():
        result["reason"] = f"源文件不存在：{src}"
        return result

    header = compat.read_safetensors_header(src)
    if not header:
        result["reason"] = "读不到 safetensors 头部"
        return result

    raw_keys = [k for k in header if k != "__metadata__"]
    needs_prefix = any(compat.normalize_key(k) != k for k in raw_keys)
    shape = compat.detect_qkv_shape(header)
    needs_split = (shape == compat.QKV_SHAPE_FUSED)

    if not needs_prefix and not needs_split:
        # 本来就符合要求 —— 不产出派生文件
        result.update(ok=True, target=str(src), changed=False,
                      reason="已是 ComfyUI 兼容形态，无需转换")
        return result

    # **有些前缀形态 ComfyUI 的 key_map 本来就能命中**（它同时登记
    # `lora_unet_<层名>` 与 `transformer.<层名>` 等别名）。
    # 这类文件**不要**产出派生文件：多一份派生文件 = ComfyUI 下拉里
    # 多一个同款条目，反而干扰用户。
    #
    # ⚠️ 判据必须精确 —— 不能只看「有没有前缀」：
    #   * `lora_unet_layers_0_proj_q`   -> ComfyUI 有 `lora_unet_` 别名 -> 命中
    #   * `layers.0.proj.q`（裸）          -> ComfyUI **没有**这个别名 -> 挂不上
    #   * `transformer.layers.0.proj.q`   -> ComfyUI 只登记 `diffusion_model.` -> 挂不上
    # 所以判据是「归一后的键**能被 ComfyUI 的别名规则命中**」。
    if needs_prefix and not needs_split:
        unnormalized = [k for k in raw_keys if compat.normalize_key(k) is None]
        if not unnormalized and compat.prefix_directly_hits(raw_keys):
            result.update(ok=True, target=str(src), changed=False,
                          reason="前缀形态可被 ComfyUI 的别名直接命中，无需转换")
            return result

    target_dir = Path(target_dir) if target_dir else src.parent
    target = target_dir / derived_name(src.name)

    # 已存在且不比源文件旧 -> 直接复用
    try:
        if target.is_file() and target.stat().st_mtime >= src.stat().st_mtime:
            t_header = compat.read_safetensors_header(target)
            if t_header:
                t_shape = compat.detect_qkv_shape(t_header)
                t_needs = any(compat.normalize_key(k) != k
                              for k in t_header if k != "__metadata__")
                if t_shape != compat.QKV_SHAPE_FUSED and not t_needs:
                    result.update(ok=True, target=str(target), changed=True,
                                  reused=True, reason="派生文件已存在且为归一形态，直接复用")
                    return result
    except Exception:
        pass

    state = _load_state(src)
    if not state:
        result["reason"] = "读不出权重（safetensors 加载失败）"
        return result

    converted, unhandled = compat.normalize_and_split(state)
    if unhandled:
        result["reason"] = (
            f"有 {len(unhandled)} 个键的形态无法无歧义归一，未转换（不猜）")
        result["unhandled"] = unhandled[:10]
        return result

    # 检查产出确实是归一形态
    leftover = [k for k in converted if k != "__metadata__"
                and compat.normalize_key(k) != k]
    if leftover:
        result["reason"] = (
            f"转换后仍有 {len(leftover)} 个键不是 ComfyUI 兼容形态，放弃写出")
        result["unhandled"] = leftover[:10]
        return result

    metadata = None
    raw_header = compat.read_safetensors_header(src)
    if isinstance(raw_header.get("__metadata__"), dict):
        metadata = dict(raw_header["__metadata__"])
        metadata["h3studio_moss_norm"] = (
            "由 h3studio_mossnorm 派生器归一而来（前缀归一 + 融合 qkv 拆分）；"
            "原始文件 " + src.name)

    if not _save_state(converted, target, metadata):
        result["reason"] = "写出派生文件失败"
        return result

    result.update(ok=True, target=str(target), changed=True,
                  converted=sum(1 for k in converted if k != "__metadata__"))
    return result


def ensure_normalized(lora_path) -> dict:
    """确保有可用的归一版文件；返回 {path, derived, reason}。

    - 已经是兼容形态 -> 直接返回原路径
    - 需要转换 -> 转换并返回派生路径
    - 转换失败 -> path=None + reason（调用方必须报错，不静默）
    """
    src = Path(lora_path)
    header = compat.read_safetensors_header(src)
    if not header:
        return {"path": None, "derived": False, "reason": "读不到 safetensors 头部"}
    r = normalize_lora(src)
    if r["ok"]:
        return {"path": Path(r["target"]), "derived": bool(r["changed"]), "reason": ""}
    return {"path": None, "derived": False, "reason": r["reason"]}


def cleanup_stale_derived(lora_dirs=None) -> int:
    """清理过期的派生文件（源文件比派生文件新时，派生文件失效）。"""
    from .loras import comfyui_lora_dirs
    dirs = lora_dirs or comfyui_lora_dirs()
    removed = 0
    for directory in dirs:
        try:
            candidates = list(Path(directory).rglob(f"{DERIVED_PREFIX}*"))
        except Exception:
            continue
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                original = candidate.name[len(DERIVED_PREFIX):].replace("__", "/")
                source = Path(directory) / original
                if not source.is_file() or source.stat().st_mtime > candidate.stat().st_mtime:
                    candidate.unlink()
                    removed += 1
            except Exception:
                continue
    return removed
