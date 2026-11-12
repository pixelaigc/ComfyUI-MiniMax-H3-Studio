# -*- coding: utf-8 -*-
"""
生成任务「模型 / LoRA」元数据的**唯一真相源**。

【为什么需要这个模块 —— 2026-11-12 用户报告的缺陷】

用户实测（8190 线上截图）：文生视频面板选了
    底模 = MiniMax-H3 混合版 · FL2VA+Ref2VA
    加速 LoRA = DMAD 4 步（critic 版），强度 1.00
但生成库的任务卡片显示：
    模型 = 「H3 + ckpt850 · 4步」
    LoRA = 「空」

追查后确认：**实际下发给 ComfyUI 的参数完全正确**（实测线上真实任务的
ComfyUI history prompt JSON：UNETLoader.unet_name = 混合版权重、
MiniMaxH3TurboLoRA.lora_name = DMAD 权重、strength = 1.0）。
错的是**记录与显示层**：

  1. web/app.js 的 `modelLabel(p)` 读的是**遗留字段** `p.model` / `p.lora`，
     而新文生视频面板提交的是 `model_key` / `accel_loras` / `fixed_accel_lora`。
     于是卡片恒显旧路径的 `H3 + ckpt850 · 4步`。
  2. web/app.js 提交时对**所有模式**无条件写
     `params.lora = state.model === "lora" ? state.lora : "none"`
     —— `state.lora` 是旧面板的遗留全局值（恒为 "ckpt850"）。
     这个遗留字段被一并落库，于是落库记录里同时存在
     「正确的 model_key/accel_loras」与「错误的遗留 lora」。
  3. 卡片模板里**根本没有 LoRA 行** —— 用户看到的「空」来自更早的版本，
     现在这一版连行都没有（模型信息被塞在「模型」一行里，混作一谈）。

【本模块的职责】

给**每一个**生成任务算出一份**权威**的「模型 / LoRA」摘要，
由服务端随任务一起下发（job["model_info"]），前端**只渲染**这份摘要。
这样「面板所选 == 落库记录 == 卡片显示」三者必然同源，
不依赖前端去猜哪个字段才是真的。

【设计约束】

  * **不臆造**：只使用本机实测存在的模型 / LoRA 清单
    （h3studio/video_models.py 的 VIDEO_MODELS / VIDEO_ACCEL_LORAS /
    VIDEO_ACCEL_LORA_COMBOS，以及 workflows.py 的 *_MODELS 表）。
  * **兼容老任务**：老任务的 params 里没有 model_key（只有旧的
    `model` / `lora`），也要能算出摘要 —— 否则历史卡片会变成空白。
  * **纯函数、无 IO**：不查磁盘、不连 ComfyUI —— 只按 params 里的
    key 去清单里查显示名。这样单元测试可以完全离线跑。
"""

from __future__ import annotations

from . import video_models as vmodels


# ------------------------------------------------------------------
# 图像模式的模型清单（来自 workflows.py 的真实常量表）
# ------------------------------------------------------------------
def _image_model_table() -> dict:
    from . import workflows as wf
    out = {}
    for attr in ("Z_IMAGE_MODELS", "QWEN_IMAGE_MODELS", "MOSS_IMAGE_MODELS"):
        table = getattr(wf, attr, None)
        if isinstance(table, dict):
            out.update(table)
    return out


def _music_model_label(checkpoint: str) -> str:
    """音乐 checkpoint key -> 人话标签。

    与前端 musicModelLabel 的兜底表保持一致（同一套 key）。
    """
    from . import music_yue2
    table = getattr(music_yue2, "YUE2_CHECKPOINTS", None)
    if isinstance(table, dict):
        entry = table.get(checkpoint)
        if isinstance(entry, dict) and entry.get("label"):
            return str(entry["label"])
    return {"yue2_3b_bf16": "YuE2 3B · BF16 完整版"}.get(checkpoint, checkpoint or "")


def _video_model_label(model_key: str) -> str:
    """文生视频底模 key -> 面板上显示的那个 label（逐字一致）。"""
    meta = vmodels.VIDEO_MODELS.get(model_key)
    if meta:
        return str(meta.get("label") or model_key)
    return model_key or ""


def _accel_lora_display(key: str) -> str:
    """加速 LoRA 组合 key -> 面板上显示的那个 display（逐字一致）。"""
    meta = vmodels.VIDEO_ACCEL_LORA_COMBOS.get(key)
    if meta:
        return str(meta.get("display") or key)
    return key or ""


def _fixed_accel_lora_display(key: str) -> str:
    """固定加速 LoRA key -> 面板上显示的那个 display（逐字一致）。"""
    meta = vmodels.VIDEO_ACCEL_LORAS.get(key)
    if meta:
        return str(meta.get("display") or key)
    return key or ""


def _effect_lora_display(name: str) -> str:
    """效果 LoRA 的权重文件名 -> 显示名。

    用 h3studio.loras 的真实目录（LORA_CATALOG）查 display ——
    与图像面板下拉里显示的是同一份（前端拿的就是服务端下发的这份清单）。
    查不到时**原样返回文件名**（不编造名字）。
    """
    if not name:
        return ""
    try:
        from . import loras as lora_registry
        catalog = getattr(lora_registry, "LORA_CATALOG", {}) or {}
        info = catalog.get(name)
        if isinstance(info, dict):
            display = str(info.get("display") or "").strip()
            if display:
                return display
    except Exception:
        pass
    return name


# ------------------------------------------------------------------
# 文生视频（t2v 新面板路径）
# ------------------------------------------------------------------
def _t2v_summary(params: dict) -> dict:
    """文生视频面板路径的模型 / LoRA 摘要。

    字段来源（**全部**来自面板真实提交的字段）：
      * model_key       —— 面板「模型」下拉
      * accel_loras     —— 面板「加速 LoRA」下拉（单选，附强度）
      * fixed_accel_lora—— 工作流固定的那枚 Turbo LoRA
    """
    out = {
        "model_key": "",
        "model_label": "",
        "model_unet": "",
        "lora_label": "",
        "lora_strength": None,
        "lora_name": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_strength": None,
        "source": "t2v_panel",
    }

    model_key = str(params.get("model_key") or "").strip()
    if not model_key:
        return out
    out["model_key"] = model_key
    out["model_label"] = _video_model_label(model_key)
    meta = vmodels.VIDEO_MODELS.get(model_key) or {}
    out["model_unet"] = str(meta.get("unet") or "")

    # 用户勾选的加速 LoRA（单选；老任务里可能是多项，取第一个生效的）
    for item in params.get("accel_loras") or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        combo = vmodels.VIDEO_ACCEL_LORA_COMBOS.get(key) or {}
        try:
            strength = float(item.get("strength",
                                    combo.get("recommended_strength", 1.0)))
        except (TypeError, ValueError):
            strength = float(combo.get("recommended_strength", 1.0))
        out["lora_label"] = _accel_lora_display(key)
        out["lora_name"] = str(combo.get("name") or "")
        out["lora_strength"] = strength
        break

    # 工作流固定的那枚 Turbo LoRA
    fixed_key = str(params.get("fixed_accel_lora") or "").strip()
    if fixed_key:
        fixed = vmodels.VIDEO_ACCEL_LORAS.get(fixed_key) or {}
        out["fixed_lora_label"] = _fixed_accel_lora_display(fixed_key)
        out["fixed_lora_name"] = str(fixed.get("name") or "")
        strength = params.get("fixed_accel_strength")
        if strength is None:
            strength = fixed.get("recommended_strength", 1.0)
        try:
            out["fixed_lora_strength"] = float(strength)
        except (TypeError, ValueError):
            out["fixed_lora_strength"] = float(fixed.get("recommended_strength", 1.0))

    return out


# ------------------------------------------------------------------
# 旧版视频路径（flf / r2v，以及历史 t2v 任务）
# ------------------------------------------------------------------
def _legacy_video_summary(params: dict, mode: str = "") -> dict:
    """旧路径视频模式的模型 / LoRA 摘要。

    旧路径用的是 `model`（"lora" / "standard"）与 `lora`（"ckpt850" /
    "v4step600" / "none"）这两个**遗留但仍然有效**的字段 ——
    workflows._h3_video_workflow 读的就是它们。

    【没有记录时怎么办】老任务（内置 Agent 建的）常常**根本没记** model
    —— 工作流会走自己的默认值（workflows._h3_video_workflow:
    `lora = params.get("lora", "ckpt850")`、`UNET["fl2va"][precision]`）。
    这时摘要必须反映**实际跑的是哪个默认权重**，而不是显示空白 ——
    否则用户看到「模型 | —」，仍然无法确认用了什么。

    【r2v 的底模不一样】参考生视频走 `UNET["ref2va"]`（不是 fl2va）——
    workflows.build_r2v:477 `unet_name: UNET["ref2va"][precision]`。
    所以默认值必须按 mode 区分，否则 r2v 卡片会显示成 FL2VA（错的）。

    这些默认值全部取自**工作流源码里的真实常量**，不是编的。
    """
    out = {
        "model_key": "",
        "model_label": "",
        "model_unet": "",
        "lora_label": "",
        "lora_strength": None,
        "lora_name": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_strength": None,
        "source": "legacy_video",
    }
    model = str(params.get("model") or "").strip()
    lora = str(params.get("lora") or "").strip()

    from . import workflows as wf

    if model == "standard":
        out["model_label"] = "标准 MiniMax-H3"
        out["model_key"] = "standard"
    elif model == "lora":
        out["model_label"] = "MiniMax-H3 + LoRA"
        out["model_key"] = "lora"
    elif model:
        # 未知的历史值 —— 如实显示原值，不编造
        out["model_label"] = model
        out["model_key"] = model
    else:
        # 【没有记录 model】-> 工作流走默认值。
        # 底模按 mode 区分：t2v/flf 用 UNET["fl2va"]，r2v 用 UNET["ref2va"]
        # （workflows.build_r2v:477 `unet_name: UNET["ref2va"][precision]`）。
        precision = str(params.get("precision") or "int8").strip()
        unet_key = "ref2va" if mode == "r2v" else "fl2va"
        unet = wf.UNET.get(unet_key, {}).get(precision) or ""
        out["model_label"] = (f"MiniMax-H3 Ref2VA（{precision}，默认档）"
                             if unet_key == "ref2va" else
                             f"MiniMax-H3 FL2VA（{precision}，默认档）")
        out["model_key"] = unet_key
        out["model_unet"] = unet

    if lora == "none":
        # 明确选了「不用加速 LoRA」
        out["lora_label"] = ""
    elif lora:
        out["lora_label"] = lora
        out["lora_name"] = str(wf.LORAS.get(lora) or "")
        try:
            out["lora_strength"] = float(params.get("lora_strength", 1.0))
        except (TypeError, ValueError):
            out["lora_strength"] = 1.0
    else:
        # 【没有记录 lora】-> 工作流默认 ckpt850（workflows._h3_video_workflow:
        # `lora = params.get("lora", "ckpt850")`），并且**确实挂上了**。
        out["lora_label"] = "ckpt850"
        out["lora_name"] = str(wf.LORAS.get("ckpt850") or "")
        out["lora_strength"] = 1.0
    return out


# ------------------------------------------------------------------
# 图像模式
# ------------------------------------------------------------------
def _image_summary(params: dict, mode: str = "") -> dict:
    """图像模式的模型 / LoRA 摘要。

    图像模式的 `model` 字段是**真实生效**的（workflows 读的就是它），
    落库的也是它 —— 所以图像模式这里没有错配，只是把显示统一到同一套形状。

    【没有记录 model 时】老任务（内置 Agent 建的）常常没记 model，
    工作流会走自己的默认值（workflows.build_t2i:
    `model_key = params.get("model") or Z_IMAGE_DEFAULT_MODEL`）。
    摘要要反映**实际跑的是哪个默认底模**，不能显示空白。
    """
    out = {
        "model_key": "",
        "model_label": "",
        "model_unet": "",
        "lora_label": "",
        "lora_strength": None,
        "lora_name": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_strength": None,
        "source": "image",
    }
    model_key = str(params.get("model") or "").strip()
    table = _image_model_table()
    if not model_key:
        # 没记录 -> 用该模式的工作流默认底模（真实常量，不是编的）
        from . import workflows as wf
        default_key = {
            "t2i": getattr(wf, "Z_IMAGE_DEFAULT_MODEL", ""),
            "t2i_qwen": getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""),
            "i2i_qwen": getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""),
            "t2i_moss": getattr(wf, "MOSS_IMAGE_DEFAULT_MODEL", ""),
        }.get(mode, "")
        if not default_key:
            return out
        model_key = default_key
        out["model_label_is_default"] = True
    out["model_key"] = model_key
    meta = table.get(model_key) or {}
    out["model_label"] = str(meta.get("label") or model_key)
    out["model_unet"] = str(meta.get("unet") or meta.get("model_path") or "")

    loras = params.get("loras")
    if isinstance(loras, list) and loras:
        enabled = []
        for item in loras:
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            try:
                strength = float(item.get("strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            if strength <= 0:
                continue
            enabled.append((item, strength))
        if enabled:
            names = []
            for item, strength in enabled:
                raw_name = str(item.get("name") or "")
                display = str(item.get("display") or "").strip() or \
                    _effect_lora_display(raw_name)
                if not display:
                    continue
                names.append(f"{display} ×{strength:.2f}")
                if not out["lora_name"]:
                    out["lora_name"] = raw_name
                    out["lora_strength"] = strength
            out["lora_label"] = "、".join(names)
    return out


# ------------------------------------------------------------------
# 音乐模式
# ------------------------------------------------------------------
def _music_summary(params: dict) -> dict:
    out = {
        "model_key": "",
        "model_label": "",
        "model_unet": "",
        "lora_label": "",
        "lora_strength": None,
        "lora_name": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_strength": None,
        "source": "music",
    }
    checkpoint = str(params.get("checkpoint") or "").strip()
    if not checkpoint:
        # 没记录 -> 工作流走默认 checkpoint（music_yue2:395
        # `key = params.get("checkpoint") or YUE2_DEFAULT_CHECKPOINT`）
        from . import music_yue2
        checkpoint = str(getattr(music_yue2, "YUE2_DEFAULT_CHECKPOINT", "")
                         or "")
    if checkpoint:
        out["model_key"] = checkpoint
        out["model_label"] = _music_model_label(checkpoint)
    return out


# ------------------------------------------------------------------
# 统一入口
# ------------------------------------------------------------------
def summarize(mode: str, params: dict) -> dict:
    """给一个任务算「模型 / LoRA」摘要（**唯一入口**）。

    返回的字典形状固定，前端直接渲染：
      {
        "model_key":    底模 key（空 = 没记录）
        "model_label":  底模显示名（人话，与面板下拉一致）
        "model_unet":   实际权重文件名（可与 ComfyUI prompt 逐字核对）
        "lora_label":   加速 / 效果 LoRA 显示名（**空字符串 = 没有 LoRA**）
        "lora_strength":LoRA 强度（没有则为 None）
        "lora_name":    LoRA 权重文件名
        "fixed_lora_label": 工作流固定加速 LoRA 的显示名
        "fixed_lora_name":   工作流固定加速 LoRA 的权重文件名
        "fixed_lora_strength": 固定 LoRA 强度
        "source":       摘要来自哪条路径（t2v_panel / legacy_video /
                        image / music）
      }
    """
    params = params or {}
    mode = str(mode or "")
    if mode == "t2v" and params.get("model_key"):
        return _t2v_summary(params)
    if mode in ("t2v", "flf", "r2v", "longfilm"):
        return _legacy_video_summary(params, mode)
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"):
        return _image_summary(params, mode)
    if mode in ("t2a", "a2a"):
        return _music_summary(params)
    # 未知模式：尽力给出一个不编造的摘要
    return _legacy_video_summary(params)
