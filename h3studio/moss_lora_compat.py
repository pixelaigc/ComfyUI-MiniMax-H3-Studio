# -*- coding: utf-8 -*-
"""
MOSS-Image LoRA 的**键形态归一化**（前缀 + 融合 qkv 拆分）。

## 本机实测到的 4 类失效形态（148 个 MOSS 系 LoRA 全量跑 ComfyUI 真实 load_lora）

全量实测结果（scripts/verify_moss_lora_attach.py，ComfyUI 真实
`model_lora_keys_unet()` + `load_lora()` 代码路径）：

| 形态 | 条数 | 键样例 | 失效原因 |
|---|---|---|---|
| 原生分离 q/k/v | 90 | `diffusion_model.layers.N.proj.q.lora_A.weight` | ✅ 全部挂上 |
| 融合 qkv | 33 | `diffusion_model.layers.N.proj.qkv.lora_A.weight` | ❌ 底模没有 `proj.qkv` 模块 |
| `lora_unet_` 前缀 | 11 | `lora_unet_layers_N_proj_qkv.lora_down.weight` | ❌ 前缀形态 ComfyUI 不认 |
| 裸 `layers.` 前缀 | 12 | `layers.N.proj.ffn_down.lora_A.default.weight` | ❌ 缺 `diffusion_model.` 前缀 |
| `transformer.` 前缀 | 1 | `transformer.layers.N.proj.*` | ❌ ComfyUI 只认 `diffusion_model.` |

MOSS 底模的注意力投影是**分离**的 `proj.{q,k,v,o}`
（`custom_nodes/moss-image/model.py` 的 `_Block` 用 `nn.ModuleDict`），
ComfyUI 登记的 892 条 key_map 里含 `qkv` 的只有文本塔的 2 条。

## 本模块做两件事

1. **前缀归一**：把 `lora_unet_layers_0_proj_q` / `layers.0.proj.q` /
   `transformer.layers.0.proj.q` 统一还原成 ComfyUI 认识的
   `diffusion_model.layers.0.proj.q`。
2. **融合 qkv 拆分**：把 `proj.qkv` 按**已验证无损**的规则拆成 q/k/v。

## 融合 -> 分离 的等价性（上游成对文件逐位验证）

上游同时提供两份文件：
    融合: models/_lora_source_peft/MOSS_image_MAX_ULTRA_hires_lora_test/moss_hires_lora_test_v0.4.safetensors
    分离: loras/moss/moss_hires_lora_v0.4_qkv_split.safetensors

实测：
- 分离的 `proj.q.hada_w1_a` == 融合 `proj.qkv.hada_w1_a` **按行三等分**的第 0 块（逐字节相同）
- 分离的 `proj.k.*` == 第 1 块，`proj.v.*` == 第 2 块
- 分离的 `hada_w1_b` / `hada_w2_b` / `alpha` 与融合**逐字节相同**（复制三份）
- 全文件逐条规则校验：**630 条逐位一致，0 条不一致**
- 数值等价（同一底模输入下融合 patch vs 分离 patch 拼接）：**最大绝对差 = 0.0**
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

# ------------------------------------------------------------------ 常量

#: 融合 qkv 拆出来的三个分离投影名（与底模 nn.ModuleDict 的键顺序一致）
QKV_SPLIT_NAMES = ("q", "k", "v")

#: 底模真实存在的投影模块（实测 255 个 .proj.*，qkv 为 0）
#: ComfyUI 登记的 key（去掉 diffusion_model. 前缀后的裸层名）

#: 「行三等分」的载体后缀 —— 形状 (out, rank)
#:
#: ⚠️ **LoKR 的 lokr_w1 / lokr_w2 不在这里** —— Kronecker 分解的
#: 行映射与「三等分」不对应（实测：融合态 out_l=16 / out_k=576，
#: 分离态 out_l=16 / out_k=192；行 p -> (i, j) 的映射不同，
#: 按行三等分后与真实 q 块的最大绝对差 = 0.13（相对误差 1.16），
#: **不可无损拆分**）。所以 LoKR 的融合 qkv 只能下架 + 标注。
_ROW_SPLIT_SUFFIXES = (
    "hada_w1_a",   # LoHa: (out, rank)
    "hada_w2_a",
    "lokr_w1_a",   # LoKR 分解形态的 a 段 (out, rank) —— 可三等分
    "lokr_w2_a",
    "lora_B.weight",       # 标准 LoRA: (out, rank)
    "lora_B.default.weight",
    "lora_up.weight",      # diffusers 风格: (out, rank)
)

#: **不可拆分**的载体后缀 —— 出现即判定为「无法转成分离形态」。
#: LoKR 的直接形态（lokr_w1 / lokr_w2）走 Kronecker 乘积，
#: 融合 qkv -> 分离 q/k/v 没有等价的重排（实测数值不等价）。
UNSPLITTABLE_SUFFIXES = (
    "lokr_w1",
    "lokr_w2",
)

#: 「复制三份」的载体后缀 —— 形状 (rank, in) 或标量
_SHARED_SUFFIXES = (
    "hada_w1_b",   # LoHa: (rank, in)
    "hada_w2_b",
    "lokr_w1_b",
    "lokr_w2_b",
    "alpha",
    "dora_scale",          # DoRA: 标量缩放因子（复制三份）
    "lora_A.weight",       # 标准 LoRA: (rank, in)
    "lora_A.default.weight",
    "lora_down.weight",    # diffusers 风格: (rank, in)
)

#: 「diff」形态的载体 —— ComfyUI 的 model_lora_keys_unet 支持
#: `<层>.diff`（直接加到权重上，形状与底模权重相同）。
#: 这种**不参与** q/k/v 拆分（它本来就是整块权重差），
#: 但前缀归一后它就能挂上了（实测 krealistick_MOSS 的 .diff 部分
#: 之前因为 lora_unet_ 前缀而挂不上）。
_DIFF_SUFFIXES = ("diff", "diff_b")

#: 载体后缀的**匹配顺序**（长后缀优先，避免 "lora_B.weight" 被
#: "lora_B" 抢先匹配，或 "lokr_w2" 抢在 "lokr_w2_a" 前面）。
#: 匹配时统一用「最长后缀优先」策略，不依赖声明顺序。
_ALL_CARRIER_SUFFIXES = tuple(sorted(
    set(_ROW_SPLIT_SUFFIXES) | set(_SHARED_SUFFIXES),
    key=len, reverse=True))

#: 全部载体后缀（含不可拆分的），按长度倒序 —— 匹配时用最长优先
_ALL_SUFFIXES_INCLUDING_UNSPLITTABLE = tuple(sorted(
    set(_ALL_CARRIER_SUFFIXES) | set(UNSPLITTABLE_SUFFIXES) | set(_DIFF_SUFFIXES),
    key=len, reverse=True))

#: 归一化后的标准前缀
TARGET_PREFIX = "diffusion_model."


# ------------------------------------------------------------------ 基础工具

def read_safetensors_header(path) -> dict:
    """只读 safetensors 头部（不加载权重本体）。

    ⚠️ **只读头部会漏掉「文件被截断」**：safetensors 的头部是
    「8 字节长度 + JSON + 数据区」，**头部本身完整**并不代表权重完整。
    实测 D2（hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors）：
    文件只有 5,369,856 字节，但头部声明数据区要 297,355,184 字节
    —— 缺 291,985,328 字节。ComfyUI 加载时抛
    `SafetensorError: incomplete metadata, file not fully covered`，
    但本函数照样能读出完整头部 —— 于是旧逻辑「头部能读 = 文件正常」
    -> 判成 compatible=true -> 用户选了必然报错。
    所以这里额外做**完整性校验**：实际文件大小必须覆盖头部声明的数据区。
    """
    try:
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(size))
    except Exception:
        return {}
    if not header:
        return {}
    # 完整性校验：文件实际大小必须 >= 头部声明的完整长度
    if not _header_covers_file(path, size, header):
        return {}
    return header


def _header_covers_file(path, header_size: int, header: dict) -> bool:
    """头部声明的数据区是否被文件实际大小完整覆盖。

    截断文件（下载中断 / 拷贝未完成）返回 False。
    """
    try:
        actual = os.path.getsize(path)
    except OSError:
        return False
    max_end = 0
    for name, meta in header.items():
        if name == "__metadata__" or not isinstance(meta, dict):
            continue
        offsets = meta.get("data_offsets")
        if isinstance(offsets, (list, tuple)) and len(offsets) == 2:
            try:
                max_end = max(max_end, int(offsets[1]))
            except (TypeError, ValueError):
                continue
    # 期望的完整文件长度 = 8（长度头）+ JSON 头 + 数据区
    expected = 8 + header_size + max_end
    return actual >= expected


def read_safetensors_header_detail(path) -> dict:
    """带完整性结论的头部读取（给「为什么不可用」用）。

    返回: {"header": dict, "ok": bool, "reason": str}
    """
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            size = struct.unpack("<Q", handle.read(8))[0]
            header = json.loads(handle.read(size))
    except Exception as exc:
        return {"header": {}, "ok": False,
                "reason": f"读不出 safetensors 头部：{type(exc).__name__}: {exc}"[:160]}
    if not header:
        return {"header": {}, "ok": False, "reason": "safetensors 头部为空"}
    try:
        actual = os.path.getsize(path)
    except OSError as exc:
        return {"header": {}, "ok": False,
                "reason": f"读不到文件大小：{type(exc).__name__}: {exc}"[:160]}
    max_end = 0
    for name, meta in header.items():
        if name == "__metadata__" or not isinstance(meta, dict):
            continue
        offsets = meta.get("data_offsets")
        if isinstance(offsets, (list, tuple)) and len(offsets) == 2:
            try:
                max_end = max(max_end, int(offsets[1]))
            except (TypeError, ValueError):
                continue
    expected = 8 + size + max_end
    if actual < expected:
        return {"header": header, "ok": False,
                "reason": (f"权重文件被截断（实际 {actual:,} 字节，"
                           f"头部声明需要 {expected:,} 字节，"
                           f"缺 {expected - actual:,} 字节）："
                           "ComfyUI 加载时会抛 SafetensorError"
                           "（incomplete metadata），这个 LoRA 无法使用。")}
    return {"header": header, "ok": True, "reason": ""}


def _strip_diffusion_prefix(name: str) -> str:
    for prefix in ("diffusion_model.", "transformer."):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _lora_unet_to_dots(name: str) -> str | None:
    """`lora_unet_layers_0_proj_q` -> `layers.0.proj.q`

    ComfyUI 的 diffusers 风格别名规则（comfy/lora.py:194）：
        key_lora = k[len("diffusion_model."):-len(".weight")].replace(".", "_")
        key_map["lora_unet_{}".format(key_lora)] = k
    也就是 `lora_unet_` 后面是**层名把点换成下划线**。
    反解时按下划线切分是**有歧义**的（层名里本来就有下划线），
    所以这里用「已知的 MOSS 模块名」做锚点还原，不瞎猜。

    入参可以是带 `lora_unet_` 前缀的完整键，也可以是已剥掉前缀的 body。

    返回 None 表示无法无歧义还原。
    """
    body = name[len("lora_unet_"):] if name.startswith("lora_unet_") else name
    parts = body.split("_")
    # 文本塔：text_embedder_blocks_N_<模块>
    #
    # ⚠️ **2026-11-15 修复（死分支）**：旧代码写的是
    #    `if parts[0] == "text_embedder"`，但 body 是用**下划线**切分的，
    #    `text_embedder_blocks_0_ffn_down` 切出来是
    #    ['text','embedder','blocks','0','ffn','down'] ——
    #    `parts[0]` 永远等于 'text'，**这个分支永远进不去**。
    #    后果：凡是带文本塔键的 LoRA（实测 D1
    #    moss_image_sft_look_lora.safetensors 的 30 个
    #    `lora_unet_text_embedder_blocks_*` 键）都被判成
    #    「无法无歧义归一」-> normalize_lora 整体放弃转换
    #    -> 融合 qkv 保持原样 -> ComfyUI 挂上 137 层、
    #    丢弃 126 层（实测 patched=137 / missing=126）。
    #    修法：按**下划线段**重切（text/embedder/blocks/N/...），
    #    并把 ffn_down / ffn_gate 还原成底模里的真实模块名。
    if len(parts) >= 4 and parts[0] == "text" and parts[1] == "embedder":
        # text_embedder.blocks.N.<tail>
        if parts[2] != "blocks" or not parts[3].isdigit():
            return None
        tail_parts = parts[4:]
        if not tail_parts:
            return None
        # ffn_down / ffn_gate / ffn_up —— **两段**（ffn + down/gate/up）
        if len(tail_parts) == 2 and tail_parts[0] == "ffn" \
                and tail_parts[1] in ("down", "gate", "up"):
            return f"text_embedder.blocks.{parts[3]}.ffn_{tail_parts[1]}"
        # proj / qkv —— 单段模块名
        if len(tail_parts) == 1 and tail_parts[0] in ("proj", "qkv"):
            return f"text_embedder.blocks.{parts[3]}.{tail_parts[0]}"
        # k_norm / q_norm —— 两段（k/q + norm）
        if len(tail_parts) == 2 and tail_parts[0] in ("k", "q") \
                and tail_parts[1] == "norm":
            return f"text_embedder.blocks.{parts[3]}.{tail_parts[0]}_norm"
        # norm1 / norm2 —— 两段（norm + 数字）
        if len(tail_parts) == 2 and tail_parts[0] == "norm" \
                and tail_parts[1].isdigit():
            return f"text_embedder.blocks.{parts[3]}.norm{tail_parts[1]}"
        # norm1 / norm2 —— **单段**（有些上游写成 norm1 而不是 norm_1）
        if len(tail_parts) == 1 and tail_parts[0] in ("norm1", "norm2"):
            return f"text_embedder.blocks.{parts[3]}.{tail_parts[0]}"
        # k_norm / q_norm —— 单段（有些上游写成 knorm）
        if len(tail_parts) == 1 and tail_parts[0] in ("knorm", "qnorm"):
            return f"text_embedder.blocks.{parts[3]}.{tail_parts[0]}"
        # 非 blocks.* 的顶层：text_embedder.proj（底模有 proj / blocks.*）
        if len(tail_parts) == 1 and tail_parts[0] == "proj":
            return "text_embedder.proj"
        return None
    # 找 "layers" 锚点
    if "layers" not in parts:
        # 非 layers.* 的顶层模块（final_linear_video / final_norm_video）
        tail = "_".join(parts)
        if tail in ("final_linear_video", "final_norm_video"):
            return tail
        return None
    i = parts.index("layers")
    if i + 1 >= len(parts):
        return None
    layer_no = parts[i + 1]
    if not layer_no.isdigit():
        return None

    rest = parts[i + 2:]
    if not rest:
        return None
    # 层内的非投影模块（k_norm / norm1 / norm2）
    if rest[0] in ("k_norm", "norm1", "norm2"):
        if len(rest) != 1:
            return None
        return f"layers.{layer_no}.{rest[0]}"
    # MOSS 的模块名只有 proj + (q|k|v|o|qkv|ffn_down|ffn_up_gate)
    if rest[0] != "proj":
        return None
    if len(rest) == 1:
        tail = "proj"
    elif len(rest) == 2 and rest[1] in ("q", "k", "v", "o", "qkv"):
        tail = "proj." + rest[1]
    elif len(rest) == 3 and rest[1] == "ffn" and rest[2] == "down":
        tail = "proj.ffn_down"
    elif len(rest) == 4 and rest[1] == "ffn" and rest[2] == "up" and rest[3] == "gate":
        # proj_ffn_up_gate —— 名字里有**两个**下划线段，必须按 4 段匹配
        tail = "proj.ffn_up_gate"
    elif len(rest) == 3 and rest[1] == "ffn" and rest[2] == "up":
        # proj_ffn_up （文本塔用）
        tail = "proj.ffn_up"
    else:
        return None
    return f"layers.{layer_no}.{tail}"


def normalize_key(name: str) -> str | None:
    """把任意形态的 LoRA 键归一成 ComfyUI 认识的 `diffusion_model.<层名>`。

    支持的输入形态：
      * `diffusion_model.layers.0.proj.q`        -> 原样
      * `transformer.layers.0.proj.q`            -> 换前缀
      * `layers.0.proj.q`                       -> 加前缀
      * `lora_unet_layers_0_proj_q`             -> 反解下划线 + 加前缀
      * `text_embedder.blocks.0.ffn_down`       -> 加前缀（文本塔，底模有）
    返回 None 表示无法无歧义归一（调用方必须据此放弃，不猜）。
    """
    # 1) 已经是标准形态
    if name.startswith(TARGET_PREFIX):
        return name

    # 2) lora_unet_ 形态
    if name.startswith("lora_unet_"):
        body = name[len("lora_unet_"):]
        # 先剥掉权重载体后缀（最长优先），再反解层名
        # 后缀匹配必须带分隔符 —— 否则 "…ffn_down.alpha" 匹配 "alpha"
        # 后会残留一个点（实测 stem='layers_0_proj_ffn_down.'）。
        for candidate in _ALL_SUFFIXES_INCLUDING_UNSPLITTABLE:
            if body.endswith("." + candidate):
                base = _lora_unet_to_dots(body[: -(len(candidate) + 1)])
                if base is None:
                    return None
                return TARGET_PREFIX + base + "." + candidate
        base = _lora_unet_to_dots(body)
        if base is None:
            return None
        return TARGET_PREFIX + base

    # 3) transformer. / 裸 layers. / text_embedder. / 顶层模块
    bare = _strip_diffusion_prefix(name)
    if (bare.startswith("layers.") or bare.startswith("text_embedder.")
            or bare.startswith("final_linear_video")
            or bare.startswith("final_norm_video")):
        return TARGET_PREFIX + bare
    return None


# ------------------------------------------------------------------ 形态判定

QKV_SHAPE_SPLIT = "split"      # 分离 q/k/v —— 与当前底模直接对齐
QKV_SHAPE_FUSED = "fused"      # 融合 qkv —— 需要拆分
QKV_SHAPE_NA = "n/a"           # 没有 q/k/v 投影


def detect_qkv_shape(header: dict) -> str:
    """判定一个 LoRA 的注意力投影形态（归一化**之后**的键）。"""
    has_fused = False
    has_split = False
    for key in header:
        if key == "__metadata__":
            continue
        norm = normalize_key(key)
        if norm is None:
            continue
        if ".proj.qkv." in norm:
            has_fused = True
        elif any(f".proj.{n}." in norm for n in QKV_SPLIT_NAMES):
            has_split = True
    if has_fused:
        return QKV_SHAPE_FUSED
    if has_split:
        return QKV_SHAPE_SPLIT
    return QKV_SHAPE_NA


def module_name_of(key: str) -> str | None:
    """从任意形态的键里抽出「模块名」（去掉权重载体后缀）。

    载体后缀按**最长优先**匹配 —— 否则 "lokr_w2_a" 会被 "lokr_w2"
    抢先匹配，模块名就会被截错。
    """
    for marker in _ALL_CARRIER_SUFFIXES + UNSPLITTABLE_SUFFIXES + _DIFF_SUFFIXES:
        if key.endswith("." + marker):
            return key[: -(len(marker) + 1)]
    return None


def lora_modules(header: dict) -> set[str]:
    """LoRA 想 patch 的模块名集合（已归一 + 去载体后缀）。"""
    out: set[str] = set()
    for key in header:
        if key == "__metadata__":
            continue
        norm = normalize_key(key)
        if norm is None:
            continue
        module = module_name_of(norm)
        if module:
            out.add(module)
    return out


# ------------------------------------------------------------------ 底模键

MOSS_BASE_ROOTS = (
    Path("/mnt/nvme1t_colorful/comfyui-models/MOSS-Image-Base"),
    Path("/mnt/nvme1t_colorful/comfyui-models/MOSS-Image-SFT"),
)

_MOSS_KEYS_CACHE: set[str] | None = None


def moss_base_layer_keys() -> set[str] | None:
    """MOSS-Image 底模的层键集合（带 `diffusion_model.` 前缀）。

    只读 index.json 的 weight_map —— 不需要把 9.5GB 权重读出来。
    """
    global _MOSS_KEYS_CACHE
    if _MOSS_KEYS_CACHE is not None:
        return _MOSS_KEYS_CACHE or None
    keys: set[str] = set()
    for root in MOSS_BASE_ROOTS:
        index = root / "transformer" / "diffusion_pytorch_model.safetensors.index.json"
        if not index.is_file():
            continue
        try:
            weight_map = json.loads(index.read_text(encoding="utf-8")).get("weight_map", {})
        except Exception:
            continue
        for name in weight_map:
            for suffix in (".weight", ".bias"):
                if name.endswith(suffix):
                    keys.add(TARGET_PREFIX + name[: -len(suffix)])
                    break
            else:
                keys.add(TARGET_PREFIX + name)
        if keys:
            break
    _MOSS_KEYS_CACHE = keys
    return keys or None


# ------------------------------------------------------------------ 归一 + 拆分

def _row_split3(tensor):
    if tensor.shape[0] % 3 != 0:
        raise ValueError(
            f"第一维 {tensor.shape[0]} 不能被 3 整除，无法按 q/k/v 三等分")
    third = tensor.shape[0] // 3
    # .contiguous()：切片出来的视图在 safetensors.save_file 里会报
    # "You are trying to save a non contiguous tensor"（实测 LoKR 权重触发）。
    # contiguous() 不改数值，只改内存布局。
    return (tensor[:third].contiguous(),
            tensor[third:2 * third].contiguous(),
            tensor[2 * third:].contiguous())


def normalize_and_split(state: dict) -> tuple[dict, list[str]]:
    """把任意形态的 LoRA 权重字典归一成 ComfyUI 认的形态。

    返回 (新字典, 无法处理的键列表)。
    无法处理的键**原样保留**在结果里（不丢数据），
    调用方必须检查列表非空 —— 非空就说明转换不完整，不能宣称已修复。
    """
    out: dict = {}
    unhandled: list[str] = []
    for key, value in state.items():
        if key == "__metadata__":
            out[key] = value
            continue
        norm = normalize_key(key)
        if norm is None:
            unhandled.append(key)
            out[key] = value
            continue
        # 拆出「模块名 + 载体后缀」
        module = module_name_of(norm)
        if module is None:
            unhandled.append(key)
            out[key] = value
            continue
        suffix = norm[len(module):].lstrip(".")

        if not module.endswith(".proj.qkv"):
            out[norm] = value
            continue

        # LoKR 直接形态（lokr_w1 / lokr_w2）走 Kronecker 乘积，
        # 融合 qkv -> 分离 q/k/v 没有等价重排（实测数值不等价），
        # 所以不猜、不拆 —— 交给调用方下架 + 标注。
        if suffix in UNSPLITTABLE_SUFFIXES:
            unhandled.append(key)
            out[key] = value
            continue

        # 融合 qkv -> 分离 q/k/v
        stem = module[: -len(".proj.qkv")]
        if suffix in _ROW_SPLIT_SUFFIXES:
            try:
                chunks = _row_split3(value)
            except Exception:
                unhandled.append(key)
                out[key] = value
                continue
            for name, chunk in zip(QKV_SPLIT_NAMES, chunks):
                out[f"{stem}.proj.{name}.{suffix}"] = chunk
        elif suffix in _SHARED_SUFFIXES:
            for name in QKV_SPLIT_NAMES:
                # .clone()：共享张量直接复制三份会「共享内存」，
                # safetensors.save_file 会拒绝保存
                # （"Some tensors share memory" —— 实测 Ultra-real-moss 触发）。
                # clone() 复制数据（不共享），数值不变。
                out[f"{stem}.proj.{name}.{suffix}"] = value.clone()
        else:
            unhandled.append(key)
            out[key] = value
    return out, unhandled


# ------------------------------------------------------------------ 前缀别名判定

#: ComfyUI 的 model_lora_keys_unet（comfy/lora.py:192-196）为每个底模权重
#: 登记这些别名：
#:   * `diffusion_model.<层名>`            （通用格式）
#:   * `lora_unet_<层名把点换成下划线>`     （diffusers 风格）
#:   * `<层名>`                            （裸名）
#: 所以带 `lora_unet_` 前缀的 LoRA **不需要**转换就能命中；
#: 而裸 `layers.` / `transformer.` 前缀**命不中**（ComfyUI 不登记这两种别名）。
_COMFYUI_ALIAS_PREFIXES = ("lora_unet_", "diffusion_model.")


def prefix_directly_hits(raw_keys: list[str]) -> bool:
    """这些带前缀的键，ComfyUI 的别名规则能不能**直接**命中。

    True  -> 不需要产出派生文件（ComfyUI 本来就能挂上）
    False -> 必须归一（裸 layers. / transformer. 前缀 ComfyUI 不认）
    """
    if not raw_keys:
        return False
    for key in raw_keys:
        if key.startswith("diffusion_model."):
            continue
        if key.startswith("lora_unet_"):
            continue
        # 裸层名 / transformer. 前缀 / 其它 -> ComfyUI 没有对应别名
        return False
    return True


# ------------------------------------------------------------------ 兼容性判定

def check_moss_compatibility(path) -> dict:
    """MOSS-Image LoRA 的**真实**兼容性判定。

    与旧实现的关键区别（这是 2026-11-10 静默失效的根因）：

    旧实现用正则 `\\.proj\\.(q|k|v|o)$` 去匹配 LoRA 的模块名。
    对**融合 qkv** 的 LoRA，这个正则**一个都匹配不到** ->
    匹配集合为空 -> `_family_layer_hit_ratio()` 返回 (1.0, 0, 0) ->
    被当成「无法判定」-> 兼容 = True、命中率 = 1.0 -> **假兼容**。

    新实现：
      1. 先归一前缀（lora_unet_ / 裸 layers. / transformer. -> diffusion_model.）
      2. 判定形态（split / fused / n/a）
      3. 逐个模块名去底模真实键集合里查，命中才叫命中
      4. 融合 qkv 的模块按「可无损拆分」判定（拆开后必然命中）
      5. 命中率为 0 或有未知缺失 -> 明确判为不兼容并说明原因
    """
    base = {
        "compatible": True, "reason": "",
        "attention_hit": 1.0, "attention_hit_count": 0, "attention_total": 0,
        "relative_perturbation": None, "suggested_strength": 1.0,
        "strength_cap": 1.0, "ctx_perturbation": None, "ctx_damping": None,
        "verified": False, "qkv_shape": QKV_SHAPE_NA, "convertible": False,
        "needs_normalize": False,
    }
    base_keys = moss_base_layer_keys()
    if not base_keys:
        return base

    # ⚠️ **2026-11-15 修复（损坏文件被当成兼容）**：
    # 旧逻辑是「读不到头部 -> return base」，而 base 的 compatible=True。
    # 于是**文件本体损坏**（读头抛异常 / 文件被截断）的 LoRA
    # 会以 compatible=true 下发 —— 用户选中后 ComfyUI 必然抛错。
    # 实测 D2（hina_MOSSImageBase_animeEnhance_v1.0-lora.safetensors）：
    # 文件被截断（5,369,856 / 297,355,184 字节），
    # ComfyUI 抛 SafetensorError: incomplete metadata，
    # 但旧代码仍判 attention_hit=1.0 / compatible=true。
    # 修法：**fail closed** —— 读不出完整文件 = 明确不兼容 + 如实原因。
    detail = read_safetensors_header_detail(path)
    if not detail["ok"]:
        base["compatible"] = False
        base["verified"] = True
        base["attention_hit"] = 0.0
        base["reason"] = (
            f"{detail['reason']} "
            "这个 LoRA 无法在本机上使用，已从可选列表中排除。")
        return base

    header = detail["header"]
    if not header:
        return base

    shape = detect_qkv_shape(header)
    base["qkv_shape"] = shape
    # 有多少键需要前缀归一（界面提示用）
    raw_keys = [k for k in header if k != "__metadata__"]
    normalized = sum(1 for k in raw_keys if normalize_key(k) != k)
    base["needs_normalize"] = normalized > 0
    base["normalized_key_count"] = normalized

    modules = lora_modules(header)
    if not modules:
        return base

    # 每个模块的载体 -> 形状（从头部拿，不读权重本体）
    # 用途：判定「融合 qkv 拆开后**必然**命中」这个前提是否真的成立
    #       —— 行映射载体（lora_B / hada_w1_a ...）的第 0 维必须能被 3 整除，
    #       否则 _row_split3 会抛 ValueError，转换就做不成。
    module_carriers: dict[str, dict[str, tuple]] = {}
    for key in raw_keys:
        norm = normalize_key(key)
        if norm is None:
            continue
        module = module_name_of(norm)
        if not module:
            continue
        suffix = norm[len(module):].lstrip(".")
        # ⚠️ 变量名不要叫 `shape` —— 外面已经有一个 `shape`（QKV 形态字符串
        #    "fused"/"split"）了，在这里被元组覆盖会导致后面的
        #    `shape == QKV_SHAPE_FUSED` 永远为 False（实测 D1 命中率算成 0.75）。
        tshape = tuple(header[key].get("shape", ())) if isinstance(header[key], dict) else ()
        module_carriers.setdefault(module, {})[suffix] = tshape

    # LoKR 直接形态的融合 qkv **不可无损拆分**（Kronecker 分解的行映射
    # 与三等分不对应，实测数值不等价）-> 明确判不兼容。
    unsplittable = False
    for key in raw_keys:
        norm = normalize_key(key)
        if norm is None:
            continue
        module = module_name_of(norm)
        if not module or not module.endswith(".proj.qkv"):
            continue
        suffix = norm[len(module):].lstrip(".")
        if suffix in UNSPLITTABLE_SUFFIXES:
            unsplittable = True
            break
    if unsplittable:
        base["compatible"] = False
        base["qkv_shape"] = QKV_SHAPE_FUSED
        base["reason"] = (
            "该 LoRA 的融合 qkv 部分是 LoKR（Kronecker）权重，"
            "无法无损拆分成分离 q/k/v（Kronecker 分解的行映射与三等分不对应，"
            "实测数值不等价），在本机 MOSS-Image 底模上无法生效。"
            "ComfyUI 会静默丢弃这部分权重（只打 \"lora key not loaded\" 的 WARNING，"
            "出图照跑但 LoRA 不生效）。")
        base["verified"] = True
        return base

    hit = 0
    total = 0
    miss_fused = 0
    miss_other = 0
    for module in sorted(modules):
        # 只统计**图像侧**的投影模块（q/k/v/o/ffn_*）。
        # text_embedder.* 是**文本塔**（Qwen3.5 侧），它不参与图像采样，
        # 而且底模里根本没有（底模的 text_embedder 在**另一个**组件里，
        # ComfyUI 的 MOSS loader 不加载它）—— 所以不计入命中率，
        # 否则会把「文本塔补丁打不上」误报成「图像 LoRA 不兼容」。
        if ".proj." not in module:
            continue
        if module.startswith(TARGET_PREFIX + "text_embedder."):
            continue
        total += 1
        if module in base_keys:
            hit += 1
            continue
        if module.endswith(".proj.qkv") and shape == QKV_SHAPE_FUSED:
            # 可无损拆分 -> 拆开后必然命中。
            #
            # ⚠️ **2026-11-15 修复（命中率口径失真）**：
            # 旧代码这里只 `continue`（不计入 hit），但 total 已经 +1
            # —— 于是「拆开后**能**命中」的融合 qkv 被算成了「没命中」。
            # 实测 D1（moss_image_sft_look_lora.safetensors）：
            # 42 层融合 qkv 全被算成 miss -> 命中率只有 126/168 = 0.75，
            # 而实际上转换后 ComfyUI 挂上 **263/0**（实测 patched=263,
            # missing=0）。命中率必须按「**转换后**的真实结果」算。
            # 前提：行映射载体的第 0 维必须能被 3 整除（否则拆分会失败），
            # 这里用头部的 shape 直接校验 —— 不读权重本体。
            carriers = module_carriers.get(module, {})
            splittable = True
            for suffix, tshape in carriers.items():
                if suffix in _ROW_SPLIT_SUFFIXES:
                    if not tshape or tshape[0] % 3 != 0:
                        splittable = False
                        break
            if splittable:
                hit += 1
                continue
            miss_other += 1
            continue
        if ".proj.qkv." in module:
            miss_fused += 1
            continue
        miss_other += 1

    if total == 0:
        return base

    ratio = hit / total
    base["attention_hit"] = round(ratio, 4)
    base["attention_hit_count"] = hit
    base["attention_total"] = total
    base["verified"] = True
    base["convertible"] = (shape == QKV_SHAPE_FUSED and miss_fused == 0 and miss_other == 0)

    if hit == 0 and total > 0:
        base["compatible"] = False
        base["reason"] = (
            f"该 LoRA 的 {total} 个投影模块在 MOSS-Image 底模里一个都不存在"
            f"（底模是分离 q/k/v，LoRA 用的是别的键名）：ComfyUI 会静默丢弃"
            "（只打 \"lora key not loaded\" 的 WARNING，出图照跑但 LoRA 没生效）。")
        return base

    if miss_other > 0:
        base["compatible"] = False
        base["reason"] = (
            f"投影模块命中率仅 {hit}/{total}：有 {miss_other} 个模块在 "
            "MOSS-Image 底模里不存在，ComfyUI 会静默丢弃这些权重"
            "（\"lora key not loaded\" WARNING，出图照跑但 LoRA 只生效一部分）。")
        return base

    if miss_fused > 0:
        base["compatible"] = False
        base["reason"] = (
            f"投影模块命中率 {hit}/{total}：有 {miss_fused} 个融合 qkv 模块"
            "在 MOSS-Image 底模里不存在（底模是分离 q/k/v），"
            "ComfyUI 会静默丢弃这些权重。")

    return base
