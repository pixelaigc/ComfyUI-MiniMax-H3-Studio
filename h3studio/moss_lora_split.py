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


#: 转换状态的三个取值（conversion_status 返回 status 字段）
STATUS_DIRECT = "direct"        # 分离形态 —— 原样就能挂，用户路径无需转换
STATUS_DERIVED = "derived"      # 融合形态 —— 派生文件已存在且完整，转换曾成功过
STATUS_UNVERIFIED = "unverified"  # 融合形态 —— 还没有派生文件，转换可行性**未验证**
STATUS_BROKEN = "broken"        # 文件本体读不出 / 被截断 —— 必然失效


def conversion_status(lora_path) -> dict:
    """判定「用户选这个 LoRA 之后能不能真的完整生效」。

    ⚠️ **为什么需要这个判定**（2026-11-15 缺陷 A 复核）：
    兼容性判定（`check_moss_compatibility`）只看 **safetensors 头部**，
    判出 compatible=true 的含义是「**结构**对得上底模」。
    但用户实际能不能用，取决于**另一件事**：这份权重是不是融合 qkv 形态
    —— 是的话，ComfyUI 会静默丢弃它（只打 "lora key not loaded"，
    出图照跑但 LoRA 没生效），必须**先转换成派生文件**才能用。

    这两件事是**两套独立逻辑**，中间有真实缺口：
    `check_moss_compatibility` 从来**没有验证过转换是否可行**
    （`convertible` 只按头部形态推断），于是「结构兼容但转换会失败」
    的 LoRA 也会以 compatible=true 照常下发。

    本函数给出的是**用户路径**的可行性，四个状态：

    | status | 含义 | 用户能不能用 |
    |---|---|---|
    | `direct` | 分离形态，原样可挂 | 能，且**无需任何转换** |
    | `derived` | 融合形态，派生文件已存在且完整 | 能（服务端自动挂派生文件） |
    | `unverified` | 融合形态，尚无派生文件 | **未验证** —— 必须先转换 |
    | `broken` | 文件本体损坏 | 不能 |

    `unverified` 的判定是**保守**的：只有当派生文件**确实存在且读得出完整头部**
    时才认为转换曾成功过。没验证过 ≠ 不可用，但**不能当作已验证来下发**。

    返回：{"status": str, "needs_conversion": bool, "derived_path": str|None,
            "reason": str}
    """
    src = Path(lora_path)
    result = {"status": STATUS_UNVERIFIED, "needs_conversion": False,
              "derived_path": None, "reason": ""}

    header = compat.read_safetensors_header(src)
    if not header:
        detail = compat.read_safetensors_header_detail(src)
        result["status"] = STATUS_BROKEN
        result["reason"] = detail.get("reason") or "读不出 safetensors 头部"
        return result

    shape = compat.detect_qkv_shape(header)
    raw_keys = [k for k in header if k != "__metadata__"]
    needs_prefix = any(compat.normalize_key(k) != k for k in raw_keys)
    needs_split = (shape == compat.QKV_SHAPE_FUSED)

    # ⚠️ **前缀归一不等于需要转换**：
    # `normalize_key` 会把 `lora_unet_` 前缀改写成 `diffusion_model.`，
    # 所以 `needs_prefix` 为 True —— 但 ComfyUI **同时登记了
    # `lora_unet_<层名>` 别名**，这类文件**原样就能挂**，
    # 生成派生文件纯属多一份副本干扰用户。
    # 判据必须跟 `normalize_lora` 一致（见那里的 prefix_directly_hits 分支）：
    # 归一后的键能被 ComfyUI 的别名规则直接命中 -> 不需要转换。
    if needs_prefix and not needs_split:
        unnormalized = [k for k in raw_keys if compat.normalize_key(k) is None]
        if not unnormalized and compat.prefix_directly_hits(raw_keys):
            needs_prefix = False

    if not needs_prefix and not needs_split:
        # 已经是 ComfyUI 能直接命中的形态 —— 用户路径无需转换
        result["status"] = STATUS_DIRECT
        result["reason"] = "权重形态与本机底模一致，可直接挂载"
        return result

    # 需要转换：查派生文件是否已经存在且完整
    target = src.parent / derived_name(src.name)
    try:
        if target.is_file():
            t_header = compat.read_safetensors_header(target)
            if t_header:
                t_shape = compat.detect_qkv_shape(t_header)
                t_keys = [k for k in t_header if k != "__metadata__"]
                t_needs = any(compat.normalize_key(k) != k for k in t_keys)
                # 同上：前缀能被 ComfyUI 别名直接命中 -> 视为归一完成
                if t_needs and t_shape != compat.QKV_SHAPE_FUSED:
                    t_unnorm = [k for k in t_keys if compat.normalize_key(k) is None]
                    if not t_unnorm and compat.prefix_directly_hits(t_keys):
                        t_needs = False
                if t_shape != compat.QKV_SHAPE_FUSED and not t_needs:
                    result["status"] = STATUS_DERIVED
                    result["derived_path"] = str(target)
                    result["needs_conversion"] = True
                    result["reason"] = (
                        "权重是融合 qkv 形态，服务端会在提交时自动使用已生成的"
                        "归一版（转换已验证可用）")
                    return result
    except Exception:
        pass

    result["status"] = STATUS_UNVERIFIED
    result["needs_conversion"] = True
    if needs_split:
        result["reason"] = (
            "权重是融合 qkv 形态，与本机底模的分离 q/k/v 结构不一致。"
            "本服务在提交任务时会自动做无损转换（转换后完整生效），"
            "但该转换**尚未在本机验证过**。")
    else:
        result["reason"] = (
            "权重的键名前缀与本机 ComfyUI 的登记写法不一致，"
            "需要先做前缀归一才能挂载。"
            "本服务在提交任务时会自动完成归一，但该归一**尚未在本机验证过**。")
    return result


def cleanup_stale_derived(lora_dirs=None) -> int:
    """清理过期的派生文件（源文件比派生文件更新时，派生文件失效）。"""
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


def prewarm_derived(lora_paths, progress=None, should_stop=None) -> dict:
    """批量生成归一化派生文件，把「未验证」变成「已验证」。

    ⚠️ **为什么需要它**（2026-11-15 缺陷 A 收口）：
    `check_moss_compatibility` 现在要求融合 qkv 形态的 LoRA **转换已验证**
    才发 compatible=true（否则就是半静默失效 —— 用户以为能用，实际不生效）。
    而「转换已验证」的证据就是**派生文件确实存在且完整**。
    没有预热，36 条融合形态的 LoRA 会一直停在「未验证」状态。

    代价：每个文件要读全量权重 + 写出等大的派生文件
    （实测 36 条合计 6.64 GB，USB 盘上约 10-20 分钟）。
    所以这是**后台任务**，绝不能放在 HTTP 请求里。

    参数：
      lora_paths  待预热的 LoRA 绝对路径列表
      progress    进度回调 (done, total, current_path, ok, reason)
      should_stop 可选的停止回调 -> 返回 True 时提前中止

    返回：{"attempted": int, "converted": int, "reused": int,
           "failed": int, "results": [...], "stopped": bool}
    """
    total = len(list(lora_paths))
    out = {"attempted": 0, "converted": 0, "reused": 0, "failed": 0,
           "results": [], "stopped": False}
    done = 0
    for path in lora_paths:
        if should_stop is not None and should_stop():
            out["stopped"] = True
            break
        src = Path(path)
        out["attempted"] += 1
        row = {"name": src.name, "ok": False, "reason": ""}
        try:
            r = normalize_lora(src)
            row["ok"] = bool(r.get("ok"))
            row["reason"] = r.get("reason", "")
            row["target"] = r.get("target")
            if r.get("ok"):
                if r.get("reused"):
                    out["reused"] += 1
                else:
                    out["converted"] += 1
            else:
                out["failed"] += 1
        except Exception as exc:  # noqa: BLE001
            row["ok"] = False
            row["reason"] = f"{type(exc).__name__}: {exc}"[:200]
            out["failed"] += 1
        out["results"].append(row)
        done += 1
        if progress is not None:
            try:
                progress(done, total, str(src), row["ok"], row["reason"])
            except Exception:
                pass
    return out
