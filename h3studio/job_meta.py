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

import json
from pathlib import Path

from . import video_models as vmodels


# ------------------------------------------------------------------
# 图像模式的模型清单（来自 workflows.py 的真实常量表）
# ------------------------------------------------------------------
def _image_model_table() -> dict:
    from . import workflows as wf
    out = {}
    # H3-Image 的模型表在 h3image 模块里（不放在 workflows，避免
    # workflows.py 继续膨胀成「大杂烩」）
    try:
        from . import h3image
        h3_table = getattr(h3image, "H3_IMAGE_MODELS", None)
        if isinstance(h3_table, dict):
            out.update(h3_table)
    except Exception:
        pass
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

    两族分开查：
      * **H3 视频**效果 LoRA -> `h3_effect_loras.discover_effect_loras()`
        （它的显示名来自权重旁边的同名 .md 文档标题）
      * **图像**效果 LoRA -> `loras.LORA_CATALOG` 的 display

    查不到时**原样返回文件名**（不编造名字）。
    """
    if not name:
        return ""
    # H3 视频族
    try:
        from . import h3_effect_loras as effect_registry
        for entry in effect_registry.discover_effect_loras():
            if entry["name"] == name:
                from . import h3_effect_lora_docs as doc_parser
                doc = doc_parser.read_lora_doc(Path(entry["path"]))
                display = str(doc.get("display") or "").strip()
                if display:
                    return display
                return entry.get("display") or name
    except Exception:
        pass
    # 图像族
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
        "lora_key": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_key": "",
        "fixed_lora_strength": None,
        # 效果 LoRA（可多选）—— 列表，每项 {name, label, strength, trigger}
        "effect_lora_labels": [],
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
        out["lora_key"] = key
        break

    # 工作流固定的那枚 Turbo LoRA
    fixed_key = str(params.get("fixed_accel_lora") or "").strip()
    if fixed_key:
        fixed = vmodels.VIDEO_ACCEL_LORAS.get(fixed_key) or {}
        out["fixed_lora_label"] = _fixed_accel_lora_display(fixed_key)
        out["fixed_lora_name"] = str(fixed.get("name") or "")
        out["fixed_lora_key"] = fixed_key
        strength = params.get("fixed_accel_strength")
        if strength is None:
            strength = fixed.get("recommended_strength", 1.0)
        try:
            out["fixed_lora_strength"] = float(strength)
        except (TypeError, ValueError):
            out["fixed_lora_strength"] = float(fixed.get("recommended_strength", 1.0))

    # 效果 LoRA（可多选，与加速 LoRA 并列共存）
    #
    # 任务卡片要显示「用了哪几枚效果 LoRA、各自强度」——
    # 不显示的话用户无法从卡片确认效果 LoRA 是否真的挂上了。
    for item in params.get("effect_loras") or []:
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
        out["effect_lora_labels"].append({
            "name": name,
            "label": _effect_lora_display(name),
            "strength": strength,
            "trigger": str(item.get("trigger") or ""),
        })

    return out


# ------------------------------------------------------------------
# 旧版视频路径（flf / r2v，以及历史 t2v 任务）
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# LoRA 值白名单（2026-11-13 · C1 全模式统一）
# ------------------------------------------------------------------
#
# 【为什么要从「视频专用」升级成「全模式统一」】
# 最初这道闸只管**视频旧路径**的 `params["lora"]`（ckpt850 / v4step600）。
# 但 2026-11-13 上线前的复核实测发现：**图像 / 音乐模式完全没有闸门**：
#
#   * 落库端 `prepare_job_params` 只对视频模式做白名单拦截，
#     t2i / t2i_qwen / t2i_moss / t2a / a2a 的 `params["lora"]`
#     **原样落库** —— 实测落库值 = "integrated_multimodal_description: [Shot 1] ..."；
#   * `agent_schema.validate_params` 写的是 `if allowed and str(lora) not in allowed`，
#     而 `mode_loras()` 对图像 / 音乐模式返回**空列表** -> `allowed` 为空
#     -> 整个校验分支被跳过 -> **任何**字符串都能过；
#   * 素材库入库 `asset_service.add_from_media_output` 又把
#     `params["lora"]` / `params["loras"]` **原文**写进 source_meta，
#     前端素材抽屉把 source_meta 的每个键原样印出来。
#
# 结果：Agent 产出的**图像 / 音频**条目仍会把提示词字段名当 LoRA 显示。
#
# 【本模块的职责】给「一个字符串」一个**统一判定**：
# 它是不是本机真实存在的 LoRA 标识符。
# 落库端（server.prepare_job_params / agent_schema.validate_params）
# 与显示端（job_meta 摘要 / asset_service 入库）**共用**这一份判定，
# 不再各写一套 —— 两套必然漂移（这正是本次缺陷的成因）。

#: 走 `params["lora"]`（单枚加速 LoRA）这条**旧**路径的**视频**模式。
#: 只有这些模式的工作流真的读 `params["lora"]`：
#:   workflows._h3_video_workflow:344 `lora = params.get("lora", "ckpt850")`
#:   workflows.build_r2v:481     `lora_name: LORAS[lora]`
#: 图像模式读的是 `params["loras"]`（效果 LoRA 数组），音乐模式没有 LoRA 概念。
VIDEO_MODES_WITH_LEGACY_LORA = ("t2v", "flf", "r2v", "longfilm")


def is_real_lora_key(value: str) -> str:
    """给一个字符串，判断它是不是**真实存在**的 LoRA 标识符。

    是 -> 原样返回（去空白）；不是 -> 返回 ""。

    【判定顺序 —— 必须「先查清单，再查形状」】
    顺序**不能反**。本机真实 LoRA 里有 23 个文件名**带空格**
    （例："z-image-effects/CYBORG FACTORY 🦾 BY STX_epoch_13.safetensors"、
    "moss-effects/moss Hsin2.safetensors"）—— 所以「不含空白」这条
    粗判若排在清单判定**前面**，会把这 23 个真实 LoRA 全部误杀。
    因此顺序是：
      1. **在真实清单里** -> 直接认（清单是权威，形状不参与）；
      2. 不在清单里 -> 再用形状判据兜底：
         长度上限（LoRA key 是短标识符，提示词超长）
         + 不含冒号 / 换行（提示词正文必带，文件名不带）；
      3. 两条都不满足 -> 返回 ""（不是 LoRA）。

    【白名单的来源】
      * 视频加速 LoRA：workflows.LORAS
      * 视频面板加速 LoRA：video_models.VIDEO_ACCEL_LORAS /
        VIDEO_ACCEL_LORA_COMBOS
      * 图像效果 LoRA：loras.LORA_CATALOG（= 面板下拉的同源清单）

    【为什么形状判据要用「冒号/换行」而不是「任意空白」】
    因为真实 LoRA 文件名里**确实有空格**（见上）。提示词正文则必然带
    冒号（"integrated_multimodal_description: ..."）或换行 ——
    用这两条既能挡住提示词，又不会误杀带空格的真 LoRA。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""

    # 1) 清单优先 —— 权威判定，形状不参与
    if _in_lora_registry(text):
        return text

    # 2) 不在清单里 -> 形状兜底判据
    #    长度上限：LoRA key / 权重文件名都是短标识符
    if len(text) > 120:
        return ""
    #    冒号 / 换行：提示词正文必带，LoRA 文件名不带
    if ":" in text or "\n" in text or "\r" in text:
        return ""
    return ""


def _in_lora_registry(text: str) -> bool:
    """text 是否在本机真实 LoRA 清单里（四个来源）。"""
    from . import workflows as wf
    if text in (getattr(wf, "LORAS", {}) or {}):
        return True
    from . import video_models as vmodels
    if text in (getattr(vmodels, "VIDEO_ACCEL_LORAS", {}) or {}):
        return True
    if text in (getattr(vmodels, "VIDEO_ACCEL_LORA_COMBOS", {}) or {}):
        return True
    try:
        from . import loras as lora_registry
        if text in (getattr(lora_registry, "LORA_CATALOG", {}) or {}):
            return True
    except Exception:
        pass
    # H3 视频效果 LoRA（扫盘发现的，不在静态注册表里）
    try:
        from . import h3_effect_loras as effect_registry
        for entry in effect_registry.discover_effect_loras():
            if entry["name"] == text:
                return True
    except Exception:
        pass
    return False


def resolve_job_lora(mode: str, params: dict) -> dict:
    """算出「这个任务真实用到的 LoRA」—— **唯一入口**（2026-11-13 · C1）。

    返回固定形状：
        {
          "lora":      str | None   视频旧路径的单枚加速 LoRA key（没有则 None）
          "loras":     list | None  图像模式的效果 LoRA 数组（没有则 None）
          "strength":  float | None 强度（没有则 None）
          "labels":    list[str]    给卡片/抽屉直接显示的文案（每项含强度）
        }

    【为什么必须有这个函数】
    「这个任务用了什么 LoRA」必须由**服务端按白名单**算好下发，
    前端只渲染 —— 前端自己去 params 里翻，必然把提示词字段名当 LoRA
    （用户 2026-11-13 实测报出的 BUG 1）。

    【判定规则】
      1. 视频旧路径（t2v / flf / r2v / longfilm）：
         读 `params["lora"]`，但值必须过 `is_real_lora_key()` 白名单；
         "none" = 明确不用；非法值 = 视为没有。
         **注意**：`params["lora"]` 缺省时，工作流会走自己的默认
         （workflows._h3_video_workflow: `params.get("lora", "ckpt850")`），
         且**确实挂上了** -> 所以这里返回 ckpt850，而不是空。
      2. 视频新面板（params 里有 model_key）：读 `params["accel_loras"]`
         + `params["fixed_accel_lora"]`（这两个字段本身就是结构化的，
         key 来自服务端下发的清单，天然可信）。
      3. 图像模式：读 `params["loras"]`（效果 LoRA 数组），
         逐项过白名单；`params["lora"]` **一律不看**（工作流不读它）。
      4. 音乐模式：没有 LoRA 概念 -> 空。

    【与落库端的分工】
    落库端（server.prepare_job_params）负责**不让脏值进库**；
    本函数负责**不让脏值被显示**（包括历史库里已经躺着的脏值）。
    两道闸互补，都基于同一份 `is_real_lora_key()` 判定。
    """
    params = params or {}
    if not isinstance(params, dict):
        # 防御：某些调用方可能传 JSON 字符串（asset_service 的 job["params"]
        # 在真实链路上是 dict，但别让一条脏数据把入库路径炸掉）。
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    if not isinstance(params, dict):
        params = {}
    mode = str(mode or "")
    result = {"lora": None, "loras": None, "strength": None, "labels": [],
              "effect_loras": None}

    # --- 图像模式：效果 LoRA 数组（params["loras"]） ---
    if mode.startswith("t2i") or mode.startswith("i2i"):
        raw = params.get("loras")
        if isinstance(raw, list) and raw:
            kept = []
            labels = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not is_real_lora_key(name):
                    continue
                try:
                    strength = float(item.get("strength", 1.0))
                except (TypeError, ValueError):
                    strength = 1.0
                if strength <= 0:
                    continue
                kept.append({"name": name, "strength": strength,
                             "trigger": str(item.get("trigger") or "")})
                labels.append(f"{_effect_lora_display(name)} ×{strength:.2f}")
            if kept:
                result["loras"] = kept
                result["strength"] = kept[0]["strength"]
                result["labels"] = labels
        return result

    # --- 音乐模式：没有 LoRA 概念 ---
    if mode in ("t2a", "a2a"):
        return result

    # --- 视频模式 ---
    # 新面板路径（model_key + accel_loras）：字段本身就是结构化的，天然可信。
    #
    # 【2026-11-13 · 合并修正】这里同时读两种形状：
    #   * `params["accel_loras"]` —— **数组**。服务端 prepare_job_params
    #     落库的**唯一真实形状**（server.py: `out["accel_loras"] = combos`），
    #     面板 submitParams() 提交的也是数组。
    #   * `params["accel_lora"]` / `accel_lora_strength` —— **单数**。
    #     面板 collectConfig() 的旧形状（仅本地配置态，不落库）。
    #
    # 【为什么必须都读】只读单数的话，真实落库任务（数组形状）会取不到
    # 用户选的那枚加速 LoRA -> 卡片显示「无」，而实际明明用了。
    # 只读数组的话，老任务（单数形状）又会丢。两边都读，按数组优先。
    model_key = str(params.get("model_key") or "").strip()
    if model_key:
        from . import video_models as vmodels
        combos = getattr(vmodels, "VIDEO_ACCEL_LORA_COMBOS", {}) or {}
        accel_key = ""
        accel_strength = None
        # 形状 1：数组（落库真实形状）
        for item in params.get("accel_loras") or []:
            if not isinstance(item, dict):
                continue
            if item.get("enabled") is False:
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            accel_key = key
            try:
                accel_strength = float(item.get("strength", 1.0))
            except (TypeError, ValueError):
                accel_strength = 1.0
            break
        # 形状 2：单数（旧配置形状）
        if not accel_key:
            single = str(params.get("accel_lora") or "").strip()
            if single and single in combos:
                accel_key = single
                try:
                    accel_strength = float(params.get("accel_lora_strength", 1.0))
                except (TypeError, ValueError):
                    accel_strength = 1.0
        if accel_key:
            result["lora"] = accel_key
            result["strength"] = accel_strength
            result["labels"] = [f"{_accel_lora_display(accel_key)} ×{accel_strength:.2f}"]
        fixed_key = str(params.get("fixed_accel_lora") or "").strip()
        if fixed_key:
            try:
                strength = float(params.get("fixed_accel_strength", 1.0))
            except (TypeError, ValueError):
                strength = 1.0
            result["labels"].append(
                f"工作流固定：{_fixed_accel_lora_display(fixed_key)} ×{strength:.2f}")
        # 效果 LoRA（可多选，与加速 LoRA 并列共存）
        raw_effect = params.get("effect_loras")
        if isinstance(raw_effect, list) and raw_effect:
            kept_effect = []
            effect_labels = []
            for item in raw_effect:
                if not isinstance(item, dict):
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
                kept_effect.append({"name": name, "strength": strength,
                                    "trigger": str(item.get("trigger") or "")})
                effect_labels.append(
                    f"效果 {_effect_lora_display(name)} ×{strength:.2f}")
            if kept_effect:
                result["effect_loras"] = kept_effect
                result["labels"].extend(effect_labels)
        return result

    # 旧路径：params["lora"]
    raw_lora = params.get("lora")
    if raw_lora is None:
        # 没有记录 -> 工作流默认 ckpt850（workflows._h3_video_workflow:
        # `lora = params.get("lora", "ckpt850")`），且**确实挂上了**。
        from . import workflows as wf
        default_key = "ckpt850" if "ckpt850" in (getattr(wf, "LORAS", {}) or {}) else ""
        if default_key:
            result["lora"] = default_key
            result["strength"] = 1.0
            result["labels"] = [f"{_accel_lora_display(default_key)} ×1.00"]
        return result

    text = str(raw_lora).strip()
    if text == "none":
        # 明确选「不用加速 LoRA」-> 没有 LoRA（不是缺失，是明确语义）
        return result
    if not is_real_lora_key(text):
        # 脏值（提示词字段名 / 乱填）-> 视为没有
        return result

    from . import workflows as wf
    try:
        strength = float(params.get("lora_strength", 1.0))
    except (TypeError, ValueError):
        strength = 1.0
    result["lora"] = text
    result["strength"] = strength
    result["labels"] = [f"{_accel_lora_display(text)} ×{strength:.2f}"]
    return result


def sanitize_legacy_lora_value(value) -> str:
    """把 `params["lora"]` 归一成「可安全落库」的值。

    返回 "" 表示**丢弃**（调用方应把该字段删掉）。
    合法值原样返回（去空白）。

    【判定顺序 —— 刻意与显示层一致】
      1. 不是字符串 / 空 -> 丢弃；
      2. 字面量 "none"（明确选「不用加速 LoRA」）-> **保留**；
         它是合法语义，只是不在 workflows.LORAS 清单里；
      3. 该模式不读 params["lora"]（图像 / 音乐）-> 丢弃；
      4. 值不在真实清单里 -> 丢弃（提示词 / 乱填）。
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    if text == "none":
        return text
    if not is_real_lora_key(text):
        return ""
    return text


def _is_valid_legacy_lora_key(value: str) -> bool:
    """`params["lora"]` 是否是**真实存在**的加速 LoRA key。

    【白名单的来源】workflows.LORAS —— 工作流里真正会挂上去的那张表。
    只有那里的 key 才可能是「这条任务真的用了某枚加速 LoRA」。

    【为什么必须有这道闸 —— 2026-11-13 用户报的真实 BUG】
    用户在媒体库「文生视频」卡片上看到 LoRA 行显示
        integrated_multimodal_description
    那是 **H3 三段式提示词的字段名**（integrated_multimodal_description /
    overall_soundscape / non_diegetic_music），**根本不是 LoRA**。
    而该任务实际并没有使用这枚「LoRA」。

    根因链（已逐环核实）：
      1. 落库端 `params["lora"]` 这条路径**没有白名单校验** ——
         `prepare_job_params` 不校验 lora（它只按 lora 决定采样档位），
         Agent 的 `validate_params` 只管 Agent 工具入口，
         面板提交 / 内置 Agent 批量 / 直接调 API 都不经过它。
      2. 摘要层 `job_meta._legacy_video_summary` 把 `params["lora"]`
         **原样透传**进 `lora_label`（没有查表、没有校验）。
      3. 前端 `loraLabel()` 直接渲染 `lora_label` ->
         提示词字段名就被当成 LoRA 名显示出来了。

    本函数是第 2 环的闸门：不在白名单里的值一律**不认**。

    【注意】字面量 "none"（明确选「不用加速 LoRA」）不在 workflows.LORAS 里，
    但它是**合法语义**，由调用方单独处理（见 _legacy_video_summary）。
    """
    if not value:
        return False
    from . import workflows as wf
    return value in (wf.LORAS or {})


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

    【2026-11-13 修 —— LoRA 字段白名单】
    `params["lora"]` **必须**是工作流.LORAS 里的 key（ckpt850 / v4step600）
    或字面量 "none"。但这条链路上**没有白名单校验**：
      * prepare_job_params 不校验 lora（只按它决定采样档位）；
      * Agent 的 validate_params 只校验 Agent 工具入口，
        面板 / 内置 Agent 批量 / 直接调 API 都不经过它。
    于是**任意字符串**都能落进 params["lora"]。用户实测就遇到
    卡片 LoRA 行显示 `integrated_multimodal_description` ——
    那是 H3 提示词的**字段名**（H3 三段式提示词的第一段），根本不是 LoRA。
    根因就是这里**原样透传**，前端又把它当 LoRA 名渲染。
    修法：只认白名单里的 key，其余一律当「没有 LoRA」（不是编一个名字）。
    """

    out = {
        "model_key": "",
        "model_label": "",
        "model_unet": "",
        "lora_label": "",
        "lora_strength": None,
        "lora_name": "",
        "lora_key": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_key": "",
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
        # （media-card-detail 与 h3-model-meta 两分支在此冲突；按「以
        #   media-card-detail 为准」取其语义，并**并入** h3-model-meta
        #   独有的 lora_key / lora_rejected 两个字段 —— 两者不冲突，
        #   各自承载不同信息，保留双方。）
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
            # 【h3-model-meta】补 lora_key（key 形态，不是显示名）。
            out["lora_key"] = real
            try:
                out["lora_strength"] = float(params.get("lora_strength", 1.0))
            except (TypeError, ValueError):
                out["lora_strength"] = 1.0
        else:
            # 【h3-model-meta】白名单外的值 —— 如实当「没有 LoRA」。
            # 脏值记进 lora_rejected 留证据，但**绝不当 LoRA 展示**。
            out["lora_label"] = ""
            out["lora_name"] = ""
            out["lora_strength"] = None
            out["lora_rejected"] = lora
    else:
        # 【没有记录 lora】-> 工作流默认 ckpt850（workflows._h3_video_workflow:
        # `lora = params.get("lora", "ckpt850")`），并且**确实挂上了**。
        out["lora_label"] = "ckpt850"
        out["lora_name"] = str(wf.LORAS.get("ckpt850") or "")
        out["lora_key"] = "ckpt850"
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
        "lora_key": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_key": "",
        "fixed_lora_strength": None,
        "source": "image",
    }
    model_key = str(params.get("model") or "").strip()
    table = _image_model_table()
    if not model_key:
        # 没记录 -> 用该模式的工作流默认底模（真实常量，不是编的）
        from . import workflows as wf
        from . import h3image
        default_key = {
            "t2i": getattr(wf, "Z_IMAGE_DEFAULT_MODEL", ""),
            "t2i_qwen": getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""),
            "i2i_qwen": getattr(wf, "QWEN_IMAGE_DEFAULT_MODEL", ""),
            "t2i_moss": getattr(wf, "MOSS_IMAGE_DEFAULT_MODEL", ""),
            # MiniMax-H3-Image：文生图与图片编辑同一个 checkpoint
            "t2i_h3image": getattr(h3image, "H3_IMAGE_DEFAULT_MODEL", ""),
            "i2i_h3image": getattr(h3image, "H3_IMAGE_DEFAULT_MODEL", ""),
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
                # 【2026-11-13 修 —— C1 显示层闸门（效果 LoRA 也要过白名单）】
                # `params["loras"][].name` 是**图像模式的 LoRA 入口**，
                # 同样没有落库校验（prepare_job_params 不校验 loras 的 name）。
                # 实测：Agent 把提示词字段名塞进 loras[].name 时，
                # 卡片 LoRA 行会显示整段提示词。
                # 这里过 is_real_lora_key() —— 不在真实清单里的
                # 一律**不认**（不显示、不编名字）。
                if not is_real_lora_key(raw_name):
                    continue
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
        "lora_key": "",
        "fixed_lora_label": "",
        "fixed_lora_name": "",
        "fixed_lora_key": "",
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
    elif mode in ("t2i", "t2i_qwen", "i2i_qwen", "t2i_moss",
                  "t2i_h3image", "i2i_h3image"):
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
    # 【2026-11-13 新增】完整生成参数 + 作者推荐参数 ——
    # 需求 C2/C3：卡片要显示模型/步数/调度器/采样器/尺寸/时长/seed/
    # LoRA 列表；模态框还要显示 shift、注意力、任务来源与工作流名。
    #
    # 【为什么要放在服务端算】前端只能读它**下发**的字段，
    # 自己从 params 里猜就会回到「按位置/模糊匹配」的老毛病
    # （用户 2026-11-13 报的 BUG 根因就是前端拿到脏值直接渲染）。
    # 服务端算好 -> 前端只渲染，字段语义恒定。
    out.update(_generation_params(params))
    out.update(_recommendations(out))
    return out


# ------------------------------------------------------------------
# 完整生成参数（C2 / C3 / C4 的唯一真源）
# ------------------------------------------------------------------

#: 生成参数的字段白名单（前端只渲染这些键，不做模糊匹配）。
#:
#: 【为什么要白名单】用户 2026-11-13 报的 BUG：卡片的 LoRA 行显示了
#: `integrated_multimodal_description`（H3 提示词字段名），且卡片完全不显示
#: 步数/调度器。根因就是取值时**按位置/模糊匹配**，而不是按明确 key 读。
#: 这里把「能显示什么」钉死成一个清单：前端遍历这份清单渲染，
#: 清单外的 params 键**一律不显示**（提示词、描述、工作流节点名等）。
GENERATION_PARAM_FIELDS = (
    "steps", "sampler", "scheduler", "cfg",
    "shift_video", "shift_audio",
    "width", "height", "aspect", "resolution",
    "duration", "seed",
    "twopass",
)

#: 字段 -> 中文显示名（前端模态框用）。
GENERATION_PARAM_LABEL = {
    "steps": "步数",
    "sampler": "采样器",
    "scheduler": "调度器",
    "cfg": "CFG",
    "shift_video": "视频 shift",
    "shift_audio": "音频 shift",
    "width": "宽",
    "height": "高",
    "aspect": "画面比例",
    "resolution": "分辨率档位",
    "duration": "时长（秒）",
    "seed": "种子",
    "twopass": "二采（SelfLift）",
}


def _generation_params(params: dict) -> dict:
    """从 params 里按**白名单**取出生成参数。

    【铁律】
      * 只认 `GENERATION_PARAM_FIELDS` 里的键 —— 提示词、描述、
        工作流节点名等**不在白名单里**，因此永远不会被显示成参数。
      * 取不到的值一律留 `None` -> 前端显示「—」，
        **绝不用别的字段顶替**（用户明确要求：「取不到的值显示「—」，
        不得用其它字段顶替」）。
    """
    params = params or {}
    out: dict = {field: None for field in GENERATION_PARAM_FIELDS}

    for field in ("steps", "sampler", "scheduler", "cfg",
                  "shift_video", "shift_audio",
                  "width", "height", "aspect", "resolution",
                  "duration", "seed"):
        if field not in params:
            continue
        value = params.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        out[field] = value

    twopass = params.get("twopass")
    if isinstance(twopass, dict) and twopass:
        out["twopass"] = twopass
    return out


def _recommendations(summary: dict) -> dict:
    """挂上作者推荐参数（需求 1 / 2 / 5）。

    底模与加速 LoRA 各自一份，字段形状固定，取不到就是 not_provided。
    """
    from . import model_recommendations as mrec
    model_key = str(summary.get("model_key") or "")
    lora_key = str(summary.get("lora_key") or "")
    fixed_key = str(summary.get("fixed_lora_key") or "")
    return {
        "model_recommendation": mrec.model_recommendation(model_key),
        "lora_recommendation": mrec.accel_lora_recommendation(lora_key),
        "fixed_lora_recommendation": mrec.accel_lora_recommendation(fixed_key),
    }


# ------------------------------------------------------------------
# 【2026-11-13 · 合并】media-card-detail 分支的生成参数摘要（C2 卡片行 /
# C3 详情模态框的唯一真源）—— 与上面的 _generation_params 并存：
#   * _generation_params()  -> 挂进 summarize() 的 model_info（原始值，
#     缺失即 None，前端 web/job-meta.js 渲染成「—」）；
#   * gen_params()          -> 服务端 row_to_job 额外下发的 gen_params
#     （已格式化成字符串，缺失即 ""，前端 genParamRowsHtml() 渲染成「—」）。
# 两者字段名不同、形状不同，**不是重复实现**，不互相覆盖。
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

