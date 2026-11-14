# -*- coding: utf-8 -*-
"""H3 文生视频「效果 LoRA」目录发现 + 兼容性实测。

【为什么单独一个模块】
`video_models.py` 管的是**加速 LoRA**（Turbo / LightX2V / DMAD —— 把 20 步
压到 4 步的蒸馏权重）。它们的键名格式是裸 `blocks.*`，只认
`MiniMaxH3TurboLoRA` 节点。

效果 LoRA 是**另一类东西**（电影质感 / 写实人像 / 二次元 / 运镜 / 特效…），
本机这批的键名格式是 `diffusion_model.blocks.*` 或 `lora_unet_blocks_*`，
**必须走 ComfyUI 核心的 `LoraLoader` / `LoraLoaderModelOnly`**。

⚠️ 【本模块最重要的一条实测结论 —— 别改反了】
`MiniMaxH3TurboLoRA` 节点对 `diffusion_model.*` 格式的效果 LoRA 会
**静默失效**（不是报错，是「0 weights patched」）：

    [MiniMaxH3TurboLoRA] pruned base [merge]: lora=dmad_minimax_h3_4step_full_critic_comfyui.safetensors
        strength=1.0 | 416 backbone modules, 0 weights patched (merged)

原因（`custom_nodes/ComfyUI-MiniMax-H3-Turbo/__init__.py:318`）：
    key_map = {m: "diffusion_model.{}.weight".format(m) for m in modules}
它按**裸模块名** `blocks.0.attn.out_proj` 拼 key，而效果 LoRA 里的模块名
**自带 `diffusion_model.` 前缀**，拼出来变成
`diffusion_model.diffusion_model.blocks.0...` -> 匹配不上 -> 0 命中。

反过来，核心 `LoraLoaderModelOnly` 走
`comfy.lora.model_lora_keys_unet()`，它同时认
`diffusion_model.blocks.*`、`lora_unet_blocks_*`、裸 `blocks.*` 三种写法。

【「什么算 H3 效果 LoRA」的判据 —— 键绑定，不是文件名】
本机 loras 目录里有 686 枚 LoRA，其中绝大部分是**图像** LoRA
（Wan / MOSS / Z-Image / Flux / Krea2 / Qwen…）。它们也都是
`diffusion_model.*` 格式，靠文件名或目录名筛是筛不干净的
（`h3/` 目录里加速与效果混放，`z-image-effects/` 里名字带 "h3" 的都有）。

唯一的可靠判据是：**这枚 LoRA 的模块能不能绑到 H3 底模的权重键上**。
H3 底模的层名是 `blocks.N.attn.qkv_proj` / `token_refiner.blocks.N.*`
这种，与其它底模（`layers.N.attention.to_q`、`transformer_blocks.N.*`…）
**完全不重叠**。所以「绑得上」= 「是 H3 的 LoRA」，一次判定解决归属问题。

本模块的所有数字都来自**本机真实权重文件**的 safetensors 头，
不查表、不写死文件名。
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

from . import loras as lora_registry
from . import video_models as vmodels

# ------------------------------------------------------------------
# safetensors 头解析（只读头部，不加载权重本体 —— 很快）
# ------------------------------------------------------------------

#: LoRA 张量名后缀 -> 槽位。覆盖 ComfyUI 支持的全部 A/B 写法
#: （comfy/weight_adapter/lora.py:160-200 逐条对齐）。
_LORA_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".lora_A.default.weight", "A"),
    (".lora_B.default.weight", "B"),
    (".lora_A.weight", "A"),
    (".lora_B.weight", "B"),
    (".lora_down.weight", "A"),
    (".lora_up.weight", "B"),
)

_HEADER_CACHE: dict[tuple[str, int, int], dict] = {}


def read_safetensors_header(path: Path) -> dict:
    """只读 safetensors 头部（不加载权重本体，快）。"""
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
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


def lora_modules(header: dict) -> dict[str, dict]:
    """LoRA header -> {模块名: {"A": shape, "B": shape}}。

    模块名保留**原始前缀**（`diffusion_model.blocks.0...` /
    `lora_unet_blocks_0...` / 裸 `blocks.0...`）—— 键绑定判定要用它。
    """
    out: dict[str, dict] = {}
    for key, value in header.items():
        if key == "__metadata__":
            continue
        for suffix, slot in _LORA_SUFFIXES:
            if key.endswith(suffix):
                name = key[: -len(suffix)]
                out.setdefault(name, {})[slot] = list(value.get("shape") or [])
                break
    return out


def base_weight_shapes(header: dict) -> dict[str, list]:
    """底模 header -> {模块名: shape}（键名统一带 `diffusion_model.` 前缀）。"""
    out: dict[str, list] = {}
    for key, value in header.items():
        if key == "__metadata__":
            continue
        if key.endswith(".weight"):
            name = key[: -len(".weight")]
            if not name.startswith("diffusion_model."):
                name = "diffusion_model." + name
            out[name] = list(value.get("shape") or [])
    return out


def _flatten(name: str) -> str:
    """`diffusion_model.blocks.0.attn.out_proj` -> `blocks_0_attn_out_proj`。"""
    return name.replace("diffusion_model.", "").replace(".", "_")


def _candidate_base_keys(module: str) -> list[str]:
    """LoRA 模块名 -> 它可能对应的底模权重键（按 ComfyUI 的三种写法）。

    与 `comfy.lora.model_lora_keys_unet`（comfy/lora.py:187-198）登记的
    键一一对应：
      * `diffusion_model.blocks.0.attn.out_proj`   (通用格式)
      * `lora_unet_blocks_0_attn_out_proj`           (kohya 扁平格式)
      * `blocks.0.attn.out_proj`                    (裸格式)
    """
    if module.startswith("lora_unet_"):
        flat = module[len("lora_unet_"):]
        return [
            module,
            "diffusion_model." + flat.replace("_", ".", 0) if False else "diffusion_model." + flat,
        ]
    bare = module
    if bare.startswith("diffusion_model."):
        bare = bare[len("diffusion_model."):]
    flat = _flatten(bare)
    return [
        "diffusion_model." + bare,
        "lora_unet_" + flat,
        bare,
    ]


# ------------------------------------------------------------------
# 底模侧的 key_map（与 ComfyUI 同源）
# ------------------------------------------------------------------

_KEYMAP_CACHE: dict[tuple[str, int, int], dict[str, str]] = {}


def base_key_map(unet_path: Path) -> dict[str, str]:
    """底模权重 -> {LoRA 侧键名: 底模权重键}。

    **与 ComfyUI `comfy.lora.model_lora_keys_unet` 同源**：它对每个
    `diffusion_model.*.weight` 同时登记两个键：
        key_map["lora_unet_<扁平名>"] = <权重键>
        key_map["<权重键去掉 .weight>"] = <权重键>
    我们照抄这两条（外加裸名变体），不引入第三种规则。
    """
    try:
        st = unet_path.stat()
    except OSError:
        return {}
    key = (str(unet_path), st.st_mtime_ns, st.st_size)
    cached = _KEYMAP_CACHE.get(key)
    if cached is not None:
        return cached
    shapes = base_weight_shapes(read_safetensors_header(unet_path))
    key_map: dict[str, str] = {}
    for weight_key in shapes:
        bare = weight_key[: -len(".weight")] if weight_key.endswith(".weight") \
            else weight_key
        flat = _flatten(bare)
        key_map["lora_unet_" + flat] = weight_key
        key_map[bare] = weight_key
        key_map[weight_key] = weight_key
    _KEYMAP_CACHE[key] = key_map
    return key_map


def _bind_module(module: str, key_map: dict[str, str]) -> str | None:
    """LoRA 模块名 -> 它绑定的底模权重键（None = 绑不上）。"""
    for cand in _candidate_base_keys(module):
        hit = key_map.get(cand)
        if hit:
            return hit
    return None


# ------------------------------------------------------------------
# 兼容性实测
# ------------------------------------------------------------------

def check_effect_lora_compat(lora_path: Path, unet_path: Path | None) -> dict:
    """实测某个 LoRA 与某个 H3 底模能不能真的生效。

    两层判据：
      1. **键绑定**（能不能被 ComfyUI 的 key_map 认出来）
      2. **形状匹配**（A/B 的 in/out 维与底模权重对不对得上）
         —— 形状对不上时 `LoRAAdapter.calculate_weight` 抛异常，
            ComfyUI 记 ERROR 后**原样返回原权重** -> 又是静默失效。

    返回：
      {
        "compatible": bool,   # 两层都过 = 会真的生效
        "bound": int,         # 键绑定成功的模块数
        "shape_ok": int,      # 形状也对上的模块数
        "shape_mismatch": int,# 形状对不上（静默失效）
        "missing": int,       # 底模里根本没这个层
        "adaln_spin": int,    # 只 patch 了 adaln（空转）
        "total": int,
        "reason": str,
      }
    """
    empty = {"compatible": False, "bound": 0, "shape_ok": 0,
             "shape_mismatch": 0, "missing": 0, "adaln_spin": 0,
             "total": 0, "reason": ""}
    if lora_path is None or not lora_path.is_file():
        empty["reason"] = "本机找不到该 LoRA 文件"
        return empty
    if unet_path is None or not unet_path.is_file():
        empty["reason"] = "本机找不到底模权重"
        return empty

    modules = lora_modules(read_safetensors_header(lora_path))
    if not modules:
        empty["reason"] = "该文件的权重结构无法解析（未识别到标准 LoRA 键名）"
        return empty

    key_map = base_key_map(unet_path)
    shapes = base_weight_shapes(read_safetensors_header(unet_path))

    bound = shape_ok = mismatch = missing = adaln = 0
    for module, slots in modules.items():
        weight_key = _bind_module(module, key_map)
        if weight_key is None or weight_key not in shapes:
            missing += 1
            continue
        a_shape = slots.get("A")
        b_shape = slots.get("B")
        w = shapes[weight_key]
        if (not a_shape or not b_shape or len(a_shape) != 2
                or len(b_shape) != 2 or len(w) != 2):
            missing += 1
            continue
        bound += 1
        # lora_A: [rank, in_dim]  lora_B: [out_dim, rank]  base W: [out_dim, in_dim]
        if a_shape[1] == w[1] and b_shape[0] == w[0]:
            shape_ok += 1
            if "adaln" in module:
                adaln += 1
        else:
            mismatch += 1

    total = len(modules)
    result = {
        "compatible": False,
        "bound": bound,
        "shape_ok": shape_ok,
        "shape_mismatch": mismatch,
        "missing": missing,
        "adaln_spin": adaln,
        "total": total,
        "reason": "",
    }
    if shape_ok > adaln and mismatch == 0 and missing == 0:
        result["compatible"] = True
        return result
    if shape_ok == 0 and adaln > 0:
        result["reason"] = (f"只 patch 了 {adaln} 个 adaln 层，"
                            "其余层全部没绑上 —— 该 LoRA 对这个底模是空转。")
    elif missing:
        result["reason"] = (f"底模里没有该 LoRA 要 patch 的层（缺 {missing}/{total}）"
                            "—— ComfyUI 会静默丢弃，出片照跑但 LoRA 没生效。")
    elif mismatch:
        result["reason"] = (f"{mismatch}/{total} 个层的形状对不上"
                            "—— 加载了也不会按预期生效。")
    else:
        result["reason"] = "无法确认该 LoRA 与底模兼容。"
    return result


# ------------------------------------------------------------------
# 目录发现
# ------------------------------------------------------------------

def _candidate_lora_files() -> list[tuple[str, Path]]:
    """扫 ComfyUI 的 loras 目录，返回 [(相对名, 绝对路径)]。

    **相对名 = ComfyUI 里的 lora_name**（含子目录，如 `h3/xxx.safetensors`），
    与 ComfyUI 自己下拉里的写法一致。
    """
    out: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for root in lora_registry.comfyui_lora_dirs():
        if not root.is_dir():
            continue
        try:
            candidates = sorted(root.rglob("*.safetensors"))
        except OSError:
            continue
        for path in candidates:
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            out.append((rel, path))
    return out


def _accel_lora_names() -> set[str]:
    """已在加速 LoRA 注册表里的权重名（这些不是「效果 LoRA」）。

    权威来源：`video_models.VIDEO_ACCEL_LORAS`（工作流固定挂的 Turbo）
    与 `VIDEO_ACCEL_LORA_COMBOS`（用户可勾选的加速组合）。
    这两处登记的是 ComfyUI 里的 lora_name。
    """
    names: set[str] = set()
    for meta in vmodels.VIDEO_ACCEL_LORAS.values():
        names.add(meta["name"])
    for meta in vmodels.VIDEO_ACCEL_LORA_COMBOS.values():
        names.add(meta["name"])
    return names


#: **加速/蒸馏类**的文件名特征。
#:
#: 注册表（VIDEO_ACCEL_LORAS / VIDEO_ACCEL_LORA_COMBOS）只登记了 8 枚，
#: 但本机 loras 目录里还有**同族的其它加速权重**没进注册表：
#:   * `minimax_h3_turbo_4步加速_comfyui.safetensors`（中文名那份）
#:   * `h3_lightx2v/minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors`
#:   * `h3_lightx2v/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`
#:   * `minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors`
#: 它们同样是「把 20 步压到 N 步」的蒸馏权重，**不是效果**，
#: 跟固定挂的 Turbo LoRA 叠加会互相打架。
#:
#: 判据是**文件名里的加速语义词**（turbo / lightx2v / 4步 / 8step / 蒸馏 /
#: distill / step600 / ckpt850 / dmad）—— 这些词是加速权重独有的，
#: 效果 LoRA（电影质感 / 写实 / 二次元 / 运镜）不带。
_ACCEL_NAME_PATTERNS: tuple[str, ...] = (
    "turbo",
    "lightx2v",
    "distill",
    "dmad",
    "ckpt850",
    "step600",
    "4step",
    "4_step",
    "8step",
    "8_step",
    "4步",
    "8步",
)


def _is_accel_by_name(rel_name: str) -> bool:
    """按文件名判断是不是加速/蒸馏类。"""
    lowered = rel_name.lower()
    return any(pattern in lowered for pattern in _ACCEL_NAME_PATTERNS)


def _read_sidecar_doc(path: Path) -> str:
    """同名 .md 说明文档的首段摘要（作者写的用途 / 档位）。

    读不到就返回空串 —— 不编造。
    """
    doc = path.with_suffix(".md")
    if not doc.is_file():
        return ""
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ">", "|", "---", "===")):
            continue
        return stripped[:220]
    return ""


#: discover_effect_loras 的**结果缓存**（key = model_key）。
#:
#: 【为什么必须缓存 —— 2026-11-14 性能缺陷实测】
#: discover_effect_loras() 要**扫全盘 663 个候选 LoRA 文件**，
#: 逐个读 safetensors 头、逐模块比对 H3 底模权重键 —— 实测单次 **0.79s**。
#:
#: 而 job_meta._effect_lora_display() / job_meta.is_real_lora_key()
#: 会在**每一条任务记录**里各调一次（`/api/jobs` 一次返回 184 条）
#: -> 184 × 0.79s ≈ **145s**，且这些调用发生在 **aiohttp 事件循环里**
#: （同步阻塞）-> 整个 8190 服务被卡住，其它请求（含
#: PUT /api/video-config）全部排队 -> 用户看到「改了强度，20 秒后
#: 才落盘」。
#:
#: 缓存策略：**TTL 60 秒 + 显式失效**。
#:   * TTL：LoRA 目录在运行期基本不变（用户装新 LoRA 是低频事件），
#:     60 秒内的陈旧最多让「刚装好的 LoRA 晚 60 秒出现在下拉里」。
#:   * 显式失效：提供 invalidate_effect_lora_cache()，
#:     装完 LoRA / 重启后可以主动刷。
#:   * 缓存**只存只读派生数据**（名字/路径/形状统计），不缓存任何
#:     用户配置，失效不影响正确性 —— 最坏情况是下拉列表少一项。
_DISCOVER_CACHE: dict[str, tuple[float, list[dict]]] = {}

#: 缓存有效期（秒）。
_DISCOVER_CACHE_TTL = 60.0


def invalidate_effect_lora_cache() -> None:
    """主动作废 discover_effect_loras 的结果缓存。

    装完 / 替换 LoRA 后调用，让下拉立刻反映新文件。
    """
    _DISCOVER_CACHE.clear()


def discover_effect_loras(model_key: str | None = None) -> list[dict]:
    """本机真实存在的 H3 效果 LoRA 清单。

    **数据来源**：扫 ComfyUI 的 loras 目录 + 读 safetensors 头，
    用「能不能绑到 H3 底模的权重键」判定归属。
    没有硬编码的文件名列表 —— 目录里有什么就是什么。

    返回的每条：
      {
        "name":       ComfyUI 里的 lora_name（相对 loras 根目录，含子目录）
        "path":       绝对路径
        "display":    显示名（文件名去扩展名）
        "size_mb":    体积
        "key_format": "diffusion_model" | "lora_unet" | "bare"
        "modules":    模块数
        "base_model": safetensors metadata 里的 base_model（若有）
        "rank":       最大的 rank（若有）
        "doc":        同名 .md 摘要（若有）
        "directory":  所在子目录名
      }
    """
    model_key = model_key or vmodels.VIDEO_DEFAULT_MODEL

    # 【结果缓存】扫盘代价高（0.79s/次），而调用方是「每条任务记录一次」
    # -> /api/jobs 一次 184 条 = 145s 阻塞。TTL 60s + 显式失效兜底。
    now = time.time()
    cached = _DISCOVER_CACHE.get(model_key)
    if cached is not None and (now - cached[0]) < _DISCOVER_CACHE_TTL:
        # 返回**副本**：调用方拿到的是 list，避免有人原地改坏缓存。
        return [dict(item) for item in cached[1]]

    model = vmodels.VIDEO_MODELS.get(model_key)
    unet_path = vmodels.find_weight(model["unet"]) if model else None
    if unet_path is None:
        return []

    key_map = base_key_map(unet_path)
    shapes = base_weight_shapes(read_safetensors_header(unet_path))
    accel_names = _accel_lora_names()

    found: list[dict] = []
    for rel, path in _candidate_lora_files():
        if rel in accel_names:
            continue
        if _is_accel_by_name(rel):
            continue
        modules = lora_modules(read_safetensors_header(path))
        if not modules:
            continue
        # 【归属判据】至少一个模块能绑到 H3 底模的权重键
        bound_h3 = 0
        for module in modules:
            weight_key = _bind_module(module, key_map)
            if weight_key is None or weight_key not in shapes:
                continue
            a_shape = modules[module].get("A")
            b_shape = modules[module].get("B")
            w = shapes[weight_key]
            if (not a_shape or not b_shape or len(a_shape) != 2
                    or len(b_shape) != 2 or len(w) != 2):
                continue
            if a_shape[1] == w[1] and b_shape[0] == w[0]:
                bound_h3 += 1
        if bound_h3 == 0:
            continue

        meta = (read_safetensors_header(path).get("__metadata__") or {})
        ranks = []
        for slots in modules.values():
            a = slots.get("A")
            if a and len(a) == 2:
                ranks.append(int(a[0]))
        key_format = "bare"
        for module in modules:
            if module.startswith("diffusion_model."):
                key_format = "diffusion_model"
                break
            if module.startswith("lora_unet_"):
                key_format = "lora_unet"
                break
        directory = rel.split("/", 1)[0] if "/" in rel else ""
        found.append({
            "name": rel,
            "path": str(path),
            "display": path.stem,
            "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
            "key_format": key_format,
            "modules": len(modules),
            "bound_modules": bound_h3,
            "base_model": meta.get("base_model", ""),
            "rank": max(ranks) if ranks else 0,
            "doc": _read_sidecar_doc(path),
            "directory": directory,
        })
    found.sort(key=lambda item: (item["directory"], item["name"]))
    # 写缓存（TTL 60s）。缓存的是**只读派生数据**，
    # 失效最坏后果只是「下拉少一项 / 晚 60 秒出现」，不影响正确性。
    _DISCOVER_CACHE[model_key] = (time.time(), found)
    return [dict(item) for item in found]
