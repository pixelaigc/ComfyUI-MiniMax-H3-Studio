"""加速 LoRA 的「按底模真实可用」判定 —— 内置 Agent 管理面板的唯一真源。

【为什么需要这个模块】
「内置 Agent 管理」里 flf / r2v 的加速 LoRA 选项原来来自
``workflows.LORAS.keys()`` —— 一张**硬编码两枚**的旧表（ckpt850 / v4step600），
既不问底模、也不问本机有没有、更不跑兼容性实测。

主生成面板早就不是这么做的：它消费服务端下发的
``video_models.options_payload()["accel_lora_compat"]``
（= ``accel_lora_compat_matrix``，对本机权重头逐层实测）按当前底模过滤下拉。

于是同一个工作台里出现两套口径：主面板按真实兼容性过滤，
Agent 管理面板照旧表列 3 项 —— 用户看到的正是这个矛盾。

【这里的判据全部来自实测，不是查表】
  * ``lora_available(name)``  —— 本机 ComfyUI 的模型清单里到底有没有这枚；
  * ``accel_lora_compat_matrix(model_key)`` —— 权重头与底模逐层比对，
    含「加载节点能不能做 AdaLN 运行期重注入」这一层（核心节点做不到）。

【为什么还要标 official / third_party】
用户要的区分是「官方加速档 vs 第三方蒸馏」。判据取自权重文件自身的
safetensors metadata（见 scripts/audit_lora_metadata_full.py 的实测输出）：
  * ``base_model: MiniMax-H3``  / ``license: MiniMax H3 Community License``
    —— 权重自己声明它属于 H3 官方谱系；
  * ``base_model: Comfy-Org`` 或文件名含 ``lightx2v``
    —— 第三方重打包 / 第三方蒸馏。
这是**字节级证据**，不是文件名猜测。
"""
from __future__ import annotations

import json
import struct

#: 权重 metadata 里声明自己属于 H3 官方谱系的字段值（小写比对）
_OFFICIAL_BASE_MARKERS = ("minimax-h3", "minimax h3")

#: 第三方蒸馏/重打包的标记（出现在文件名或 metadata.source 里）
_THIRD_PARTY_MARKERS = ("lightx2v", "comfy-org")


def _read_metadata(path) -> dict:
    """读 safetensors 头部里的 __metadata__（只读前几 KB，不加载权重）。"""
    try:
        with open(path, "rb") as fh:
            n = struct.unpack("<Q", fh.read(8))[0]
            head = json.loads(fh.read(n))
    except Exception:
        return {}
    meta = head.get("__metadata__")
    return meta if isinstance(meta, dict) else {}


def _blob(meta: dict) -> str:
    """把可能声明谱系的字段拼成一个小写文本，供标记匹配。"""
    parts = []
    for key in ("base_model", "license", "model", "source",
                "conversion_source_file", "converted_layout"):
        value = meta.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts).lower()


def weight_provenance(name: str) -> dict:
    """一枚权重的来源判定（读它自己的 metadata）。

    返回 {official_h3, third_party, base_model, reason}。
    读不到文件 / 读不到 metadata -> 三个标志都为 False，reason 说明原因
    （**不猜**：没有证据就不宣称它是官方档）。
    """
    from . import loras as lora_registry

    resolved, path = lora_registry.resolve_lora(name)
    if not path:
        return {"official_h3": False, "third_party": False,
                "base_model": "", "reason": "本机找不到该权重文件"}
    meta = _read_metadata(path)
    text = _blob(meta) + " " + str(name).lower()
    base_model = str(meta.get("base_model") or "")
    official = any(m in text for m in _OFFICIAL_BASE_MARKERS)
    third = any(m in text for m in _THIRD_PARTY_MARKERS)
    if not meta:
        return {"official_h3": False, "third_party": False,
                "base_model": base_model,
                "reason": "权重没有 metadata，无法证明它属于哪一谱系"}
    return {"official_h3": official, "third_party": third,
            "base_model": base_model, "reason": ""}


#: 官方 Turbo 加载节点。只有**官方 H3 Turbo 蒸馏权重**会需要它：
#: 这类权重的键名是裸形态（``blocks.*`` / ``token_refiner.*``），
#: 而第三方蒸馏 / 第三方重打包一律是 ``diffusion_model.*`` 前缀，
#: 走核心 ``LoraLoaderModelOnly``。
#:
#: 【为什么用加载节点当「官方 turbo 系」的判据】
#: 它不是名字约定，而是**权重字节自己决定的**：
#: ``video_models.detect_accel_lora_loader()`` 读权重头的键名前缀来决定
#: 用哪个加载节点 —— 实测见 scripts/audit_lora_metadata_full.py：
#:   ckpt850 / v4step600            -> blocks/token_refiner  -> MiniMaxH3TurboLoRA
#:   lightx2v_* / turbo_4step_cn(_ema) / dmad / better_motion / speed_slider
#:                                  -> diffusion_model        -> LoraLoaderModelOnly
#: 于是「官方 turbo 系」= 官方谱系 + 官方 Turbo 权重形态，
#: 完全派生，不需要维护任何 key 名单。
OFFICIAL_TURBO_LOADER = "MiniMaxH3TurboLoRA"


def lora_kind(item: dict) -> str:
    """一枚加速 LoRA 的类别（由它自己的字节决定，不是查表）。

    返回：
      "official_turbo"  官方 H3 谱系 + 官方 Turbo 权重形态
      "third_party"     第三方蒸馏 / 第三方重打包
      "other"           官方谱系但不是 turbo 蒸馏（如效果向、其它蒸馏）
    """
    if item.get("third_party"):
        return "third_party"
    if item.get("official_h3") and item.get("loader") == OFFICIAL_TURBO_LOADER:
        return "official_turbo"
    return "other"


def classify(key: str, model_key: str) -> dict:
    """单枚加速 LoRA 在某个底模下的分类（官方 / 第三方 / 其它）。

    返回 {kind, compatible, reason, official_h3, third_party, loader}。
    供服务端下发 ``selectable`` 时使用 —— 前端据此过滤，**不硬编码名单**。
    """
    from . import video_models as vmodels

    meta = vmodels.accel_lora_meta(key) or {}
    name = meta.get("name") or ""
    check = (vmodels.accel_lora_compat_matrix(model_key) or {}).get(key, {})
    prov = weight_provenance(name)
    item = {
        "official_h3": prov["official_h3"],
        "third_party": prov["third_party"],
        "loader": check.get("loader") or vmodels.detect_accel_lora_loader(name),
    }
    item["kind"] = lora_kind(item)
    item["compatible"] = bool(check.get("compatible"))
    item["reason"] = check.get("reason") or ""
    return item


def available_accel_loras(model_key: str) -> list[dict]:
    """该底模**真实可用**的加速 LoRA（服务端唯一真源）。

    判据（全部实测，缺一不可）：
      1. 本机 ComfyUI 的模型清单里真的有这枚权重；
      2. 逐层兼容性实测通过（含加载节点能不能重注入 AdaLN）。

    返回按 key 排序的 [{key, display, name, loader, compatible, reason,
    matched, total, official_h3, third_party, provenance_reason,
    recommended_steps, recommended_strength}]。
    """
    from . import video_models as vmodels

    matrix = vmodels.accel_lora_compat_matrix(model_key)
    out: list[dict] = []
    for key, check in matrix.items():
        meta = vmodels.accel_lora_meta(key) or {}
        name = meta.get("name") or ""
        if not name:
            continue
        if not vmodels.lora_available(name):
            continue
        if not check.get("compatible"):
            continue
        prov = weight_provenance(name)
        out.append({
            "key": key,
            "display": meta.get("display") or key,
            "name": name,
            "loader": check.get("loader") or vmodels.detect_accel_lora_loader(name),
            "compatible": True,
            "reason": check.get("reason") or "",
            "matched": check.get("matched"),
            "total": check.get("total"),
            "official_h3": prov["official_h3"],
            "third_party": prov["third_party"],
            "base_model": prov["base_model"],
            "provenance_reason": prov["reason"],
            "recommended_steps": meta.get("recommended_steps"),
            "recommended_strength": meta.get("recommended_strength"),
        })
    out.sort(key=lambda item: (not item["official_h3"], item["key"]))
    return out


def rejected_accel_loras(model_key: str) -> list[dict]:
    """该底模下被排除的加速 LoRA + 排除原因（用于说明与审计）。"""
    from . import video_models as vmodels

    matrix = vmodels.accel_lora_compat_matrix(model_key)
    out: list[dict] = []
    for key, check in matrix.items():
        meta = vmodels.accel_lora_meta(key) or {}
        name = meta.get("name") or ""
        if not name or check.get("compatible"):
            continue
        out.append({"key": key, "display": meta.get("display") or key,
                    "name": name, "reason": check.get("reason")
                    or "本机找不到该权重"})
    out.sort(key=lambda item: item["key"])
    return out
