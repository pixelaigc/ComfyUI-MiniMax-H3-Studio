# -*- coding: utf-8 -*-
"""内置 Agent 的「增强提示词」统一管线（2026-11-14 需求 2c / 16）。

【要解决的】
用户要求：**所有 Agent 发起的生成都必须走「增强提示词」路线** ——
先把用户自然语言需求整理成普通提示词，再送进**该模型专用的**增强流程，
拿到最终增强提示词，并把增强后的提示词作为**实际生成提示词**
写入工作流与媒体库记录。

【为什么需要这个模块】
此前 Agent 只有两个增强入口：
  * enhance_prompt（走本项目 Provider + 文本模板）
  * 没有「模型专用 PE」的概念 —— Qwen 的官方 PE、H3-Image 的官方 PE
    都只在前端「增强」下拉里可用，Agent 够不着。

本模块把「按模式选对增强流程」这件事收敛到**一个**函数里：
    plan_for_mode(mode) -> EnhancePlan
Agent 的 create_job 在建任务前调它，拿到的就是
「最终要送进工作流的提示词」。

【各模式的增强流程（按需求逐条对应）】
    t2i / t2i_qwen / i2i_qwen / t2i_moss / t2i_h3image / i2i_h3image
        -> 文本增强（enhancer 模板）+ **该模型专用的 PE**
           t2i_moss     -> moss-image 专用模板（需求 2c 的 MOSS 示例）
           t2i_qwen     -> qwen21-pe-t2i（官方 PE 档）
           i2i_qwen     -> qwen21-pe-i2i（官方 PE 档）
           t2i_h3image  -> H3-Image 官方 PE（需求 16）
           i2i_h3image  -> H3-Image 官方 PE（需求 16，图片编辑也走）
           t2i (Z-Image)-> image 模板
    t2v / flf / r2v
        -> video-director 模板（H3 官方三段式导演提示词）
    t2a / a2a
        -> YuE2 结构化风格 + 歌词（**结构化路线**，不是文本）

【增强失败怎么办 —— 如实告知，不静默降级】
需求说「所有 Agent 发起的生成都必须走增强提示词路线」。
但硬失败（比如 PE 跑不起来）不能让整个任务作废 —— 那会让 Agent 变成
「什么都不干」。本模块的策略：
  * 增强**成功** -> 用增强后的提示词（记 enhanced=true）
  * 增强**失败** -> 用**原始提示词**继续，并在任务与结果里如实标注
    「本次未能增强，用的是原始提示词」+ 失败原因（notice）。
  * 绝不假装增强过（媒体库里必须能看出用的是哪一份提示词）。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from . import enhancer as enh

#: 需要走「模型专用 PE」的模式 -> PE 档位。
#:
#: 【为什么只有这几个】PE 是**模型专属**的：用 Qwen 的 PE 权重去扩写
#: MOSS 的提示词，产物风格完全不搭。H3-Image 的 PE 更是只能给 H3-Image 用。
PE_MODES = {
    "t2i_qwen": "qwen21-pe-t2i",
    "i2i_qwen": "qwen21-pe-i2i",
    "t2i_h3image": "h3image-pe",
    "i2i_h3image": "h3image-pe",
}

#: 走「模型专用 PE」的模式集合（Agent 必须先跑 PE，再跑文本增强）
PE_MODE_KEYS = set(PE_MODES)


@dataclass
class EnhancePlan:
    """一次增强的方案 + 结果。"""

    mode: str
    #: 文本模板 spec id（走本项目 Provider 的那部分）
    spec_id: str = ""
    #: 模型专用 PE 的档位（None = 这一档没有专用 PE）
    pe_tier: str | None = None
    #: 结构化路线（YuE2）：产物是对象不是文本
    structured: bool = False

    # ---- 执行结果 ----
    #: 最终要送进工作流的提示词（增强后）
    prompt: str = ""
    #: 用户原始输入
    original: str = ""
    #: 是否真的完成了增强
    enhanced: bool = False
    #: 如实告知（增强失败的原因 / 降级说明）
    notices: list[str] = field(default_factory=list)
    #: 结构化产物（YuE2 风格字段 + 歌词）
    structured_data: dict | None = None
    #: 额外参数（例如 H3-Image PE 选出的画面比例）
    extra_params: dict = field(default_factory=dict)


def plan_for_mode(mode: str) -> EnhancePlan:
    """按模式决定「该走哪条增强路线」。

    这是**唯一的**决策入口 —— Agent 与前端都该走它，
    避免「Agent 用一套、前端用另一套」的漂移。
    """
    mode = (mode or "").strip()
    if mode in ("t2a", "a2a"):
        # 音乐：结构化路线（12 个风格字段 + 歌词），产物不是文本
        return EnhancePlan(mode=mode, spec_id="yue2-style", structured=True)
    spec_id = enh.default_spec_for_mode(mode) or enh.default_spec_for_mode("t2i")
    pe_tier = PE_MODES.get(mode)
    return EnhancePlan(mode=mode, spec_id=spec_id or "", pe_tier=pe_tier)


async def _text_enhance(spec_id: str, original: str, ctx) -> str:
    """走本项目 Provider 的文本增强（与 /api/enhance 同一套模板与参数）。"""
    from . import llm as llm_client

    saved = _load_overrides(ctx)
    spec = enh.get_spec(spec_id, saved)
    params = enh.resolve_params(None, saved, spec)
    messages = enh.build_messages(spec, original, params.get("language", "auto"))

    provider = getattr(ctx, "provider", None)
    if not provider:
        raise RuntimeError("没有可用的本地模型 Provider")
    raw = await llm_client.call_provider(
        provider, messages,
        options={"temperature": params["temperature"],
                 "top_p": params["top_p"],
                 "max_tokens": params["max_tokens"],
                 "do_sample": params.get("do_sample", True),
                 "timeout": _step_timeout()})
    raw = enh.strip_thinking(raw)
    out = enh.strip_wrapping(raw)
    if not out:
        raise RuntimeError("模型返回了空内容，未做修改")
    echo = enh.echo_detail(out, spec["system_prompt"])
    if echo.get("is_echo"):
        raise RuntimeError(
            f"Provider「{provider.get('name')}」返回的是系统提示词复读，"
            f"不是有效的提示词（{echo.get('reason')}）")
    validation = enh.validate_output(out, original, spec.get("validate_mode", "pe"))
    if not validation.get("ok", True):
        raise RuntimeError("扩写结果违反工作台的提示词硬约束："
                          + "；".join(validation.get("errors", [])))
    return out


def _load_overrides(ctx) -> dict[str, Any]:
    """读「增强」模板的用户覆盖（与 /api/enhance 同一份来源）。

    【为什么这么读】覆盖存在 model_provider_prefs 表的 "enhancer" key 里，
    与 /api/enhancer_config 读的是同一份。ctx 上没有就回退到空 ——
    空覆盖等于用内置模板，与改动前行为一致。
    """
    try:
        from . import agent_settings
        loader = getattr(agent_settings, "load_enhancer_overrides", None)
        if callable(loader):
            return loader() or {}
        return {}
    except Exception:
        return {}


def _step_timeout() -> float:
    from . import agent as agent_mod
    return float(getattr(agent_mod, "STEP_TIMEOUT", 120))


def _server_self_url(ctx) -> str:
    """取服务端自己的可访问地址。

    【为什么不能写死】ctx.server 是**服务端模块**（server.py 那个 module），
    不是某个实例。地址要从模块里拿。

    【取值顺序】
      1. server.self_url —— 公开属性（若将来改成公开命名）
      2. server._studio_self_url() —— server.py 里现有的那个（带下划线，
         是给 ComfyUI 节点回连用的同一个地址）
      3. 都没有 -> 抛错（**不猜端口** —— 猜错了会打到别人的服务上）

    【为什么必须有兜底】H3-Image 官方 PE 与 YuE2 结构化风格这两条路线
    都是**服务端自己**的 HTTP 接口（/api/h3image-pe/expand、
    /api/music-enhance），Agent 要回调它们。地址拿不到就调不了。
    """
    server = getattr(ctx, "server", None)
    if server is None:
        raise RuntimeError("Agent 未接入服务端环境")
    url = getattr(server, "self_url", None)
    if callable(url):
        try:
            value = url()
        except Exception:
            value = None
        if value:
            return str(value)
    elif isinstance(url, str) and url:
        return url
    helper = getattr(server, "_studio_self_url", None)
    if callable(helper):
        try:
            value = helper()
        except Exception:
            value = None
        if value:
            return str(value)
    raise RuntimeError("服务端未提供可回访的自身地址，无法调用服务端增强接口")


async def _run_h3image_pe(ctx, prompt: str, mode: str,
                          ref_images: list[str] | None = None) -> dict:
    """跑 H3-Image 官方 PE（需求 16）。

    走**服务端**的 /api/h3image-pe/expand —— 那边已经封装了
    ComfyUI 提交 + 轮询 + 解析。与前端「增强」按钮走同一条链路。
    """
    url = _server_self_url(ctx)
    import aiohttp
    payload = {"prompt": prompt, "mode": mode}
    if ref_images:
        payload["refs"] = ref_images
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{url}/api/h3image-pe/expand", json=payload) as resp:
            body = await resp.json()
    if resp.status != 200:
        raise RuntimeError(body.get("error") or f"H3-Image PE 扩写失败（HTTP {resp.status}）")
    return body


async def enhance_for_mode(ctx, mode: str, original: str,
                           ref_images: list[str] | None = None) -> EnhancePlan:
    """给某个模式生成**最终提示词**。

    返回的 plan.prompt 就是应该写进工作流与媒体库记录的提示词。
    增强失败时不抛异常 —— 降级到原始提示词，并在 notices 里如实说明。
    """
    original = (original or "").strip()
    plan = plan_for_mode(mode)
    plan.original = original
    plan.prompt = original
    if not original:
        plan.notices.append("没有提示词")
        return plan

    # ---- 1) 模型专用 PE（先跑 PE，再跑文本增强）----
    if plan.pe_tier == "h3image-pe":
        try:
            body = await _run_h3image_pe(ctx, original, mode, ref_images)
            pe_text = str(body.get("expanded") or "").strip()
            if pe_text:
                plan.prompt = pe_text
                plan.enhanced = True
                ratio = str(body.get("aspect_ratio") or "").strip()
                if ratio:
                    plan.extra_params["aspect"] = ratio
                plan.extra_params["pe"] = "h3image-official"
            else:
                plan.notices.append("H3-Image 官方 PE 返回空内容，用原始提示词")
        except Exception as exc:
            plan.notices.append(
                f"H3-Image 官方 PE 扩写失败（{exc}），本次用原始提示词生成")
        if plan.enhanced:
            # PE 已经给出高质量扩写 —— 不再叠一层文本增强
            # （两层串联会把提示词改得面目全非）。
            return plan

    # ---- 2) 文本增强（走该模式的专用模板）----
    if plan.structured:
        # 音乐的结构化路线由 music_style + 歌词生成负责，
        # Agent 侧只把原始描述整理成 style 字段（见 _structured_music）。
        try:
            data = await _structured_music(ctx, original)
            plan.structured_data = data
            plan.prompt = data.get("style", "") or original
            plan.enhanced = True
            plan.extra_params["lyrics"] = data.get("lyrics", "")
        except Exception as exc:
            plan.notices.append(f"YuE2 结构化风格生成失败（{exc}），本次用原始描述生成")
        return plan

    try:
        plan.prompt = await _text_enhance(plan.spec_id, original, ctx)
        plan.enhanced = True
    except Exception as exc:
        plan.notices.append(f"提示词增强失败（{exc}），本次用原始提示词生成")
    return plan


async def _structured_music(ctx, original: str) -> dict:
    """YuE2 结构化风格 + 歌词（需求 2c 的 YuE2 示例）。

    走服务端已有的 /api/music-enhance —— 与前端「生成风格+歌词」按钮
    **同一条链路**，产物形状一致（12 个风格字段 + 歌词 + 合成好的 style 串）。
    """
    url = _server_self_url(ctx)
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{url}/api/music-enhance",
                                json={"prompt": original, "mode": "t2a"}) as resp:
            body = await resp.json()
    if resp.status != 200:
        raise RuntimeError(body.get("error") or
                          f"结构化风格生成失败（HTTP {resp.status}）")
    style_fields = body.get("style")
    style_text = str(body.get("style_text") or "").strip()
    if not style_text:
        raise RuntimeError("结构化风格返回为空")
    lyrics_field = body.get("lyrics")
    lyrics_text = ""
    if isinstance(lyrics_field, dict):
        lyrics_text = str(lyrics_field.get("lyrics_override") or "").strip()
    elif isinstance(lyrics_field, str):
        lyrics_text = lyrics_field.strip()
    if not lyrics_text:
        lyrics_text = str(body.get("lyrics_text") or "").strip()
    return {
        "style": style_text,
        "style_fields": style_fields if isinstance(style_fields, dict) else {},
        "lyrics": lyrics_text,
    }
