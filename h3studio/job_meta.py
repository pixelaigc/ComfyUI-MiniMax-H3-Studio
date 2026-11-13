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
# LoRA 值合法性判定（2026-11-13）
# ------------------------------------------------------------------
#
# 【为什么需要这一层 —— 用户 2026-11-13 实测报出的 BUG 1】
#
# 用户看到生成库卡片上的「LoRA」行显示着
#     integrated_multimodal_description: [Shot 1] ...
# —— 一段**提示词**，而那个任务**根本没用任何 LoRA**。
#
# 【根因链（两端都有份）】
#   1. 落库端：agent_schema.validate_params 的 LoRA 分支写成
#      `if allowed and str(lora) not in allowed` ——
#      图像 / 音乐模式的 `mode_loras()` 返回**空列表** ->
#      `allowed` 为空 -> 整个校验分支被跳过 -> **任何**字符串都能进 params。
#      所以 Agent 误把提示词填进 `lora` 时，任务照样建成功。
#   2. 展示端：`_legacy_video_summary` 直接把 `params["lora"]` 的**原文**
#      当成 LoRA 显示名（`out["lora_label"] = lora`）——
#      没有做任何「这个值是不是真的 LoRA key」的判断。
#      于是提示词原文被原样印在「LoRA」那一行。
#
# 【本函数的职责】**单一判定入口**：给一个字符串，回答
# 「它是不是一个真实存在的 LoRA 标识符」。
#
# 判定标准（三条全过才算）：
#   1. 长度上限 —— LoRA key 不可能是几百字的自然语言；
#   2. 不含空白 —— 提示词必有换行/空格，LoRA key 没有；
#   3. 在**真实清单**里 —— workflows.LORAS（视频加速 LoRA）+
#      video_models.VIDEO_ACCEL_LORA_COMBOS / VIDEO_ACCEL_LORAS（视频面板）+ 
#      loras.LORA_CATALOG（图像效果 LoRA）。
#
# 不在清单里 -> 返回 ""（不是 LoRA）。前端据此显示「无」。
#
# 【为什么用「清单」而不是「模式」来判断】
# 历史落库数据里，图像模式也存过 `params["lora"] = "ckpt850"`
# （前端旧实现无条件写遗留字段，见 web/app.js 的 params.lora 赋值）——
# 但图像工作流**根本不读**这个字段（workflows.build_t2i 读的是 params["loras"]）。
# 所以「ckpt850 在清单里」不能作为「这个任务用了 ckpt850」的证据。
# 真正的判据是**该模式的工作流是否读这个字段**（见 _legacy_video_summary）。
def is_real_lora_key(value: str) -> str:
    """给一个字符串，判断它是不是**真实存在**的 LoRA 标识符。

    是 -> 原样返回（去空白）；不是 -> 返回 ""。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    # 1) 长度上限：LoRA key / 权重文件名都是短标识符
    if len(text) > 120:
        return ""
    # 2) 不含空白：提示词 / 描述文本一定带换行或空格
    if any(ch.isspace() for ch in text):
        return ""
    # 3) 在真实清单里
    from . import workflows as wf
    if text in (getattr(wf, "LORAS", {}) or {}):
        return text
    if text in (getattr(vmodels, "VIDEO_ACCEL_LORA_COMBOS", {}) or {}):
        return text
    if text in (getattr(vmodels, "VIDEO_ACCEL_LORAS", {}) or {}):
        return text
    try:
        from . import loras as lora_registry
        catalog = getattr(lora_registry, "LORA_CATALOG", {}) or {}
        if text in catalog:
            return text
    except Exception:
        pass
    return ""


#: 走「params.lora」这条旧路径的**视频**模式。
#: 只有这些模式的工作流真的读 params["lora"]：
#:   workflows._h3_video_workflow:344 `lora = params.get("lora", "ckpt850")`
#:   workflows.build_r2v:481     `lora_name: LORAS[lora]`
#: 图像模式读的是 params["loras"]（效果 LoRA 数组），音乐模式没有 LoRA 概念。
_VIDEO_MODES_WITH_LEGACY_LORA = ("t2v", "flf", "r2v", "longfilm")


def resolve_job_lora(mode: str, params: dict) -> dict:
    """算出「这个任务真实用到的 LoRA」——**唯一入口**（2026-11-13）。

    返回固定形状：
        {
          "lora":      str | None   视频旧路径的单枚加速 LoRA key（没有则 None）
          "loras":     list | None   图像模式的效果 LoRA 数组（没有则 None）
          "strength":  float | None  强度（没有则 None）
          "labels":    list[str]     给卡片/抽屉直接显示的文案（每项含强度）
        }

    【为什么必须有这个函数】
    用户 2026-11-13 实测报出：卡片上的 LoRA 显示的根本不是该任务用过的 LoRA，
    甚至是一段提示词。要杜绝这件事，**取值必须是白名单式的**，
    而不是「把 params 里叫 lora 的东西抄出来」。

    【判定规则】
      1. 视频旧路径（t2v / flf / r2v / longfilm）：
         读 params["lora"]，但值必须过 is_real_lora_key() 白名单；
         "none" = 明确不用；非法值 = 视为没有。
      2. 视频新面板（params 里有 model_key）：读 params["accel_loras"]
         + params["fixed_accel_lora"]（这两个字段本身就是结构化的，
         key 来自服务端下发的清单，天然可信）。
      3. 图像模式（t2i / t2i_qwen / i2i_qwen / t2i_moss）：
         读 params["loras"]（效果 LoRA 数组）—— **不读** params["lora"]
         （旧前端无条件写的遗留字段，图像工作流根本不读它）。
      4. 音乐模式（t2a / a2a）：没有 LoRA 概念 -> 全空。
    """
    params = params or {}
    mode = str(mode or "")
    out = {"lora": None, "loras": None, "strength": None, "labels": []}

    # --- 3) 图像模式：效果 LoRA 数组 ---
    if mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"):
        items = params.get("loras")
        if isinstance(items, list) and items:
            cleaned = []
            labels = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled") is False:
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                try:
                    strength = float(item.get("strength", 1.0))
                except (TypeError, ValueError):
                    strength = 1.0
                if strength <= 0:
                    continue
                display = str(item.get("display") or "").strip() \
                    or _effect_lora_display(name)
                cleaned.append({"name": name, "display": display,
                                "strength": strength,
                                "trigger": str(item.get("trigger") or "")})
                labels.append(f"{display} ×{strength:.2f}")
            if cleaned:
                out["loras"] = cleaned
                out["strength"] = cleaned[0]["strength"]
                out["labels"] = labels
        return out

    # --- 4) 音乐模式：没有 LoRA 概念 ---
    if mode in ("t2a", "a2a"):
        return out

    # --- 2) 视频新面板路径 ---
    if mode in _VIDEO_MODES_WITH_LEGACY_LORA and params.get("model_key"):
        labels = []
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
            display = _accel_lora_display(key)
            out["lora"] = key
            out["strength"] = strength
            labels.append(f"{display} ×{strength:.2f}")
            break
        fixed_key = str(params.get("fixed_accel_lora") or "").strip()
        if fixed_key:
            fixed = vmodels.VIDEO_ACCEL_LORAS.get(fixed_key) or {}
            try:
                fixed_strength = float(params.get(
                    "fixed_accel_strength",
                    fixed.get("recommended_strength", 1.0)))
            except (TypeError, ValueError):
                fixed_strength = float(fixed.get("recommended_strength", 1.0))
            labels.append(f"工作流固定：{_fixed_accel_lora_display(fixed_key)}"
                          f" ×{fixed_strength:.2f}")
        out["labels"] = labels
        return out

    # --- 1) 视频旧路径：params["lora"]，但必须过白名单 ---
    if mode in _VIDEO_MODES_WITH_LEGACY_LORA:
        raw = str(params.get("lora") or "").strip()
        if raw == "none":
            # 明确选了「不用加速 LoRA」-> 确实没有
            return out
        if raw:
            # 有记录 -> 必须是真实 LoRA key，否则视为「没有」（不编造）
            real = is_real_lora_key(raw)
            if real:
                try:
                    strength = float(params.get("lora_strength", 1.0))
                except (TypeError, ValueError):
                    strength = 1.0
                out["lora"] = real
                out["strength"] = strength
                out["labels"] = [f"{real} ×{strength:.2f}"]
            return out
        # 【没有记录 lora】-> 工作流走**默认档**，并且**确实挂上了**：
        #   workflows._h3_video_workflow:344
        #     `lora = params.get("lora", "ckpt850")`
        #   workflows.py:371-374 `if lora != "none": p["138"] = {...LoraLoader...}`
        # 所以老任务（内置 Agent 建的，常常没记 lora）**实际用了 ckpt850**。
        # 显示「无」是错的 —— 那会让用户以为这条视频没挂加速 LoRA。
        #
        # 【这不是编造】ckpt850 是工作流源码里的真实默认常量
        # （workflows.LORAS["ckpt850"]），而且工作流确实会把它挂上。
        # 与 _legacy_video_summary 的「else」分支同源、同一语义。
        from . import workflows as wf
        default_key = "ckpt850"
        if default_key in (getattr(wf, "LORAS", {}) or {}):
            out["lora"] = default_key
            out["strength"] = 1.0
            out["labels"] = [f"{default_key} ×1.00"]
    return out


# ------------------------------------------------------------------
# 文生视频（t2v 新面板路径）
# ------------------------------------------------------------------
def _attention_label(key: str) -> str:
    """注意力机制 key -> 显示名（**逐字**取面板下拉里的那个 label）。

    与面板下拉同源（h3studio.video_models.VIDEO_ATTENTION_MODES），
    所以「生成库显示的注意力机制」与「面板上能选到的那个」永远一致。

    已被移除的机制（sol_scheduled）也会查到 —— 那时返回
    「<label>（已下线）」，如实说明这条老任务当时用的是哪个机制，
    而不是显示空白或编一个。
    """
    meta = vmodels.VIDEO_ATTENTION_MODES.get(key)
    if meta:
        return str(meta.get("label") or key)
    removed = vmodels.VIDEO_REMOVED_ATTENTION_MODES.get(key)
    if removed:
        return f"{removed.get('label') or key}（已下线）"
    return key or ""


def attention_label(key: str) -> str:
    """**公开**入口：注意力机制 key -> 显示名（与面板下拉逐字一致）。

    素材库入库时要用它给条目打上「用的是哪种注意力机制」的标注，
    所以对外暴露（内部实现仍是 _attention_label）。
    """
    return _attention_label(key)


def _attention_summary(params: dict) -> dict:
    """文生视频任务的「注意力机制」摘要。

    【为什么要有】用户要求「媒体库/生成库标注所用注意力机制」——
    每个视频条目要显示生成时**实际使用的**注意力机制。

    字段来源：params["attention"]（面板真实提交的字段，
    服务端归一层已校验过它在清单里）。

    老任务没有这个字段 -> attention_key 为空、label 为空
    （**不编造**：老任务确实没有记录注意力机制）。
    """
    key = str(params.get("attention") or "").strip()
    out = {
        "attention_key": key,
        "attention_label": "",
        "attention_speed_label": "",
        "attention_available": True,
    }
    if not key:
        return out
    meta = vmodels.VIDEO_ATTENTION_MODES.get(key)
    if meta is None:
        removed = vmodels.VIDEO_REMOVED_ATTENTION_MODES.get(key)
        out["attention_label"] = _attention_label(key)
        out["attention_available"] = False
        if removed:
            out["attention_note"] = removed.get("reason", "")
        return out
    out["attention_label"] = str(meta.get("label") or key)
    out["attention_speed_label"] = str(meta.get("speed_label") or "")
    return out


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
        # 【2026-11-13 修 —— 只认「真实存在的 LoRA key」】
        #
        # 旧实现：`out["lora_label"] = lora` —— 把 params["lora"] 的**原文**
        # 直接当显示名。于是当 Agent 误把提示词填进 lora 字段时
        # （agent_schema 的 LoRA 校验对图像/音乐模式是空转的，见那里的说明），
        # 卡片「LoRA」行会显示
        #     integrated_multimodal_description: [Shot 1] ...
        # —— 一段提示词冒充 LoRA（用户 2026-11-13 实测报出）。
        #
        # 现在：先过 is_real_lora_key() 白名单。
        #   * 通过 -> 照旧显示（这是该模式工作流真的会读、真的会挂上的 LoRA）；
        #   * 不通过 -> 视为「这个任务没有 LoRA」（lora_label 留空）。
        #
        # 【不猜、不补】被判定为非法时**不编造**一个 LoRA 名 ——
        # 用户要看到的是「无」，而不是另一个错误的名字。
        real = is_real_lora_key(lora)
        if real:
            out["lora_label"] = real
            out["lora_name"] = str(wf.LORAS.get(real) or "")
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
        out = _t2v_summary(params)
    elif mode in ("t2v", "flf", "r2v", "longfilm"):
        out = _legacy_video_summary(params, mode)
    elif mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss"):
        out = _image_summary(params, mode)
    elif mode in ("t2a", "a2a"):
        out = _music_summary(params)
    else:
        # 未知模式：尽力给出一个不编造的摘要
        out = _legacy_video_summary(params)
    # 【2026-11-12】注意力机制摘要对**所有模式**统一附加 ——
    # 保证 summarize() 返回的形状恒定（前端只渲染，不判断字段是否存在）。
    # 图像 / 音乐模式本来就没有注意力机制，字段会是空值（不编造）。
    out.update(_attention_summary(params))
    return out


# ------------------------------------------------------------------
# 生成参数摘要（C2 卡片行 / C3 详情模态框）—— 2026-11-13
# ------------------------------------------------------------------
#
# 【为什么要有这个模块】
# 用户 2026-11-13 报出 BUG 2：卡片**不显示步数等参数**，显示区域也有限。
# 要求：
#   * 卡片补齐关键生成参数：模型、步数、调度器、采样器、尺寸、时长、seed；
#   * 缺失的显式显示「—」，**不得拿别的字段顶替**。
#
# 【纪律 —— 与 model_info 同一套】
# 前端不自己猜「这个任务的步数是多少」——服务端按任务真实 params 算好下发，
# 前端只渲染。缺失就是 None，前端显示「—」。
# **绝不用别的字段顶替**（例如时长缺失时不能拿 max_duration 顶，
# 那是另一个模式的字段名，语义不同）。
def _num(value):
    """把参数值转成可显示的字符串；没有就返回 ""（空 = 缺失）。"""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return ""
    try:
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return ""
            if float(value).is_integer():
                return str(int(value))
            return f"{value:.2f}".rstrip("0").rstrip(".")
        return str(value)
    except (TypeError, ValueError):
        return ""


def gen_params(mode: str, params: dict, model_info: dict | None = None,
               resolution_info: dict | None = None) -> dict:
    """算出任务的**完整生成参数**（C2 卡片行 + C3 详情模态框的唯一真源）。

    返回固定形状，每个键要么是字符串（可能为 "" = 缺失），要么是列表：
        {
          "model":      模型显示名
          "steps":      步数
          "scheduler":  调度器
          "sampler":    采样器
          "shift":      shift
          "cfg":        CFG
          "seed":       种子（"随机" = 没指定）
          "size":       尺寸（宽×高）
          "duration":   时长
          "lora":       LoRA 名称 + 强度（"无" = 确实没有）
          "attention":  注意力机制
          "workflow":   工作流名
          "prompt":     完整提示词
        }

    【缺失 = ""，由前端渲染成「—」】
    这里**不编造**、也**不顶替** —— 缺就是缺。
    """
    params = params or {}
    mode = str(mode or "")
    info = model_info if isinstance(model_info, dict) else summarize(mode, params)
    reso = resolution_info if isinstance(resolution_info, dict) else {}

    out = {
        "model": str(info.get("model_label") or ""),
        "steps": _num(params.get("steps")),
        "scheduler": _num(params.get("scheduler")),
        "sampler": _num(params.get("sampler")),
        "shift": _num(params.get("shift")),
        "cfg": _num(params.get("cfg")),
        "seed": _num(params.get("seed")),
        "size": "",
        "duration": "",
        "lora": "",
        "attention": "",
        "workflow": "",
        "prompt": str(params.get("prompt") or ""),
    }

    # --- shift：视频用 shift_video / shift_audio，图像用 shift ---
    # 视频面板提交的是 shift_video / shift_audio（两个都真实存在），
    # 图像面板提交的是 shift。**不互相顶替** —— 各自只认自己的字段。
    if not out["shift"]:
        shift_video = _num(params.get("shift_video"))
        shift_audio = _num(params.get("shift_audio"))
        if shift_video or shift_audio:
            parts = []
            if shift_video:
                parts.append(f"视频 {shift_video}")
            if shift_audio:
                parts.append(f"音频 {shift_audio}")
            out["shift"] = " · ".join(parts)

    # --- 尺寸：优先用服务端按产物实际像素算好的宽高 ---
    #
    # 【为什么 resolution_info 优先】reso_label.describe() 内部已经做过
    # 「实际像素优先」的判定（source = actual / request / none），
    # 它返回的 width/height 就是**卡片该显示的**那个尺寸。
    # 如果这里先读 params.width/height，就会拿到**请求值**
    # （1344×760），而卡片显示的是**实际值**（1344×768）——
    # 同一张卡片两个数字对不上。
    #
    # 【为什么还要兜底 params】resolution_info.width 可能是 None
    # （老任务既没有请求宽高也没有产物像素，source == "none"）。
    width = reso.get("width")
    height = reso.get("height")
    if not (width and height):
        width = params.get("width")
        height = params.get("height")
    if width and height:
        out["size"] = f"{int(width)}×{int(height)}"

    # --- 时长：视频/图像用 duration；音乐用 max_duration（字段名不同） ---
    duration = _num(params.get("duration"))
    if not duration and mode in ("t2a", "a2a"):
        duration = _num(params.get("max_duration"))
    if duration:
        out["duration"] = f"{duration}s"

    # --- 种子：没指定就是「随机」（这是真实语义，不是缺失） ---
    if not out["seed"]:
        out["seed"] = "随机"

    # --- LoRA：走 resolve_job_lora（白名单判定） ---
    lora = resolve_job_lora(mode, params)
    if lora["labels"]:
        out["lora"] = " · ".join(lora["labels"])
    else:
        # 确实没有 LoRA —— 显式写「无」，而不是留空让人猜。
        out["lora"] = "无"

    # --- 注意力机制 ---
    attention = str(info.get("attention_label") or "")
    if attention:
        speed = str(info.get("attention_speed_label") or "")
        out["attention"] = f"{attention} · {speed}" if speed else attention

    # --- 工作流名：模式 -> 工作流键 ---
    out["workflow"] = WORKFLOW_LABELS.get(mode, mode or "")

    return out


#: 模式 -> 工作流名（C3 详情模态框要显示「工作流名」）。
#: 来源是 workflows.BUILDERS 的键名 —— 工作流构建器的真实名字，
#: 不是编的营销名。
WORKFLOW_LABELS = {
    "t2v": "文生视频（_h3_video_workflow）",
    "flf": "首尾帧生视频（_h3_video_workflow）",
    "r2v": "参考图生视频（build_r2v）",
    "longfilm": "长片拼接",
    "t2i": "文生图（build_t2i）",
    "t2i_qwen": "Qwen-Image 文生图（build_t2i_qwen）",
    "i2i_qwen": "Qwen-Image 图片编辑（build_i2i_qwen）",
    "t2i_moss": "MOSS-Image 文生图（build_t2i_moss）",
    "t2a": "文生音乐（music_yue2）",
    "a2a": "参考生音乐（music_yue2）",
}
