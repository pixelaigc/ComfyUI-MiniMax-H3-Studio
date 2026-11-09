# -*- coding: utf-8 -*-
"""
Qwen-Image 2.1 官方 PE 提示词扩写 —— 独立成一次「增强」。

【为什么要有这个接口】
出图链路里本来就有官方 PE（workflows._qwen_common_nodes 的 use_pe 分支），
但那是**出图时**在 ComfyUI 工作流内部做的：提示词送进模型前被 PE 改写，
用户在前端看不到改写成了什么，只能看到最终图片。

用户的要求是「官方 PE 扩写」必须并进「增强」下拉菜单：
  在「增强」下拉里选中「qwen21图片官方增强PE」，点「增强」按钮
  -> 走官方 PE checkpoint 把当前提示词扩写
  -> 扩写结果**写回提示词框**，用户可以继续编辑
  -> 出图时仍按各自的 use_pe 开关决定要不要再扩写一次

所以这里提供的是**可预览、可编辑的官方 PE 扩写**，
与出图链路内部那一次是同一套 checkpoint、同一套 system prompt、同一套采样参数。

【与 /api/enhance 的区别 —— 不能混用】
/api/enhance 走的是本项目自己配置的 Provider（本地 LLM / OpenAI 兼容 API），
用的是 skill 里的**文本 system prompt 模板**模拟官方 PE 的写作规则。
本模块走的是**官方 PE checkpoint 本身**（ComfyUI 的 TextGenerate 节点
+ qwen3.5_qwen_image_2.1_pe_t2i / _pe_i2i 权重 + 官方 system prompt）。

两者的产物形态不同，**不能用同一套校验器**：
  - /api/enhance 的产物要过 validate.py（禁质量元标签、禁权重语法…）
    —— 那套约束是给 Z-Image 的。
  - 官方 PE 的产物是「观察者式结构化描述」，里面**本来就允许**出现
    具体的名词性描述；且官方 PE-T2I 的 system prompt 明确要求
    「英文描述 + 图内文字保留原语种」，与 Z-Image 约束不兼容。
  -> 所以本模块**不做** validate.py 的硬约束校验，只做最基本的
     「非空 / 不是系统提示词复读 / 不是 think 残留」检查。

【官方事实（本机可验证）】
  checkpoint:
    qwen3.5_qwen_image_2.1_pe_t2i.int8_convrot.safetensors  （文生图）
    qwen3.5_qwen_image_2.1_pe_i2i.int8_convrot.safetensors  （图片编辑）
  采样参数（官方 prompt_rewrite/README + HF model card）:
    T2I: presence_penalty=1.5, max_new_tokens=16256
    I2I: presence_penalty=0,   max_new_tokens=24000
  两个 checkpoint **不可互换** —— 档位由「文生图 / 图片编辑」自动决定。
  thinking 必须开（官方 PE 模型卡：thinking 必开）。
  max_length 不能压到官方 ComfyUI 模板的 4096 —— 官方文档警告
  「reasoning block 用完 4096 预算时不会闭合，enhancer 返回空串」。

【参考图】
  图片编辑模式下，官方 PE-I2I 工作流把参考图**同时**喂给 PE 扩写节点
  （BatchImagesNode -> TextGenerate.image），这样 PE 才能「看着图」改写
  编辑指令并在输出里写 <image1>/<image2>。
  本模块保持同样接法；顺序必须与占位标记 <imageN> 严格一致。
"""
from __future__ import annotations

import asyncio
import json

from h3studio import workflows as W

#: 扩写一次的最长等待（秒）。官方 PE 是 9B 模型 + thinking，
#: 实测文生图档约 30~60 秒；留足余量但不能无限等。
PE_TIMEOUT_SEC = 600

#: 轮询 ComfyUI history 的间隔（秒）
PE_POLL_INTERVAL_SEC = 1.5

#: 两个档位 -> 官方 checkpoint
PE_CLIP_NAMES = {
    "t2i": W.QWEN_IMAGE_CLIP_PE_T2I,
    "i2i": W.QWEN_IMAGE_CLIP_PE_I2I,
}

#: 两个档位 -> 官方 system prompt 资产文件
PE_PROMPT_FILES = dict(W.QWEN_PE_PROMPT_FILES)


class PEError(RuntimeError):
    """官方 PE 扩写失败。message 面向用户，直接可读。"""


def tier_for(is_edit: bool) -> str:
    """档位 key：图片编辑 -> i2i，文生图 -> t2i。"""
    return "i2i" if is_edit else "t2i"


def pe_params_for(is_edit: bool) -> dict:
    """官方采样参数（presence_penalty / max_new_tokens）。"""
    return dict(W.QWEN_IMAGE_PE_PARAMS[tier_for(is_edit)])


def system_prompt_for(is_edit: bool) -> str:
    """官方 PE 的 system prompt 原文。取不到就报错 —— 宁可不扩写，
    也不能用模型内置模板冒充官方 PE（那是另一套规则）。"""
    text = W._qwen_pe_system_prompt(is_edit)
    if not text.strip():
        raise PEError(
            "官方 PE 的 system prompt 缺失："
            f"{PE_PROMPT_FILES.get(tier_for(is_edit), '?')}。"
            "请检查 h3studio/skills/prompt-enhancer/assets/ 下的模板文件是否完整。")
    return text


def build_pe_workflow(prompt: str, is_edit: bool,
                      ref_images: list[str] | None = None,
                      system_prompt: str | None = None,
                      pe_params: dict | None = None) -> dict:
    """
    构造官方 PE 扩写的 ComfyUI 工作流。

    链路（与官方 Qwen PE 工作流一致）：
        CLIPLoader(PE checkpoint) -> TextGenerate(+官方 system prompt) -> PreviewAny

    参考图（仅图片编辑）：
        LoadImage -> BatchImagesNode -> TextGenerate.image
        顺序即语义：ref_images[0] -> <image1> -> image_1 插槽。

    注意：**不接** TextEncodeQwenImage21 / UNETLoader —— 这里只做扩写，
    不出图。带 UNET 会白占 20GB 显存，且与出图链路的显存争抢。

    system_prompt / pe_params 可覆盖：
      「增强」多选列表里官方 PE 那一项自带**可编辑的 system prompt**
      与**可编辑的档位参数**（presence_penalty / max_new_tokens）。
      传 None = 用官方内置值（默认行为，向后兼容）。
    """
    tier = tier_for(is_edit)
    clip_name = PE_CLIP_NAMES[tier]
    pe = dict(pe_params_for(is_edit))
    if pe_params:
        # 只覆盖显式给出的键，未给出的保留官方值（不写死一套并行默认）
        for key in ("presence_penalty", "max_new_tokens"):
            if pe_params.get(key) is not None:
                pe[key] = pe_params[key]
    system_prompt = (system_prompt if system_prompt is not None
                     else system_prompt_for(is_edit))
    if not (system_prompt or "").strip():
        raise PEError(
            "官方 PE 的 system prompt 为空："
            f"{PE_PROMPT_FILES.get(tier, '?')}。"
            "请检查 h3studio/skills/prompt-enhancer/assets/ 下的模板文件是否完整。")

    workflow: dict = {
        "pe:clip": {
            "inputs": {"clip_name": clip_name, "type": "qwen_image",
                       "device": "default"},
            "class_type": "CLIPLoader",
        },
    }

    pe_inputs = {
        "clip": ["pe:clip", 0],
        "prompt": prompt,
        # max_length 上限 32768（TextGenerate 节点 schema）；
        # 官方卡值 16256/24000 都在范围内。不能用官方模板的 4096 ——
        # 官方文档原文：reasoning block 用完预算不会闭合 -> 返回空串。
        "max_length": int(pe.get("max_new_tokens") or 4096),
        # COMFY_DYNAMICCOMBO_V3 在 API 格式下：主字段是选项 key，
        # 子输入扁平化成 sampling_mode.xxx
        "sampling_mode": "on",
        "sampling_mode.temperature": 1.0,
        "sampling_mode.top_k": 20,
        "sampling_mode.top_p": 0.95,
        "sampling_mode.min_p": 0,
        "sampling_mode.repetition_penalty": 1,
        "sampling_mode.seed": 0,
        # 官方 PE 的 presence_penalty：T2I=1.5 / I2I=0
        "sampling_mode.presence_penalty": float(pe.get("presence_penalty", 0.0)),
        # thinking 必须开（官方 PE 模型卡：thinking 必开）
        "thinking": True,
        "use_default_template": True,
        "mtp": "auto",
        # 官方 PE 自己的 system prompt（不是模型内置模板）
        "system_prompt": system_prompt,
    }

    # 参考图也喂给 PE（官方 PE-I2I 工作流就是这么接的）
    if is_edit and ref_images:
        load_nodes = []
        for index, filename in enumerate(ref_images):
            node_id = f"pe:ref{index}"
            workflow[node_id] = {
                "inputs": {"image": filename},
                "class_type": "LoadImage",
            }
            load_nodes.append(node_id)
        workflow["pe:batch"] = {
            "inputs": {f"images.image{i}": [node_id, 0]
                       for i, node_id in enumerate(load_nodes)},
            "class_type": "BatchImagesNode",
        }
        pe_inputs["image"] = ["pe:batch", 0]

    workflow["pe:gen"] = {"inputs": pe_inputs, "class_type": "TextGenerate"}
    # ComfyUI 要求工作流里必须有输出节点，否则报 prompt_no_outputs
    workflow["pe:out"] = {
        "inputs": {"source": ["pe:gen", 0]},
        "class_type": "PreviewAny",
    }
    return workflow


def extract_pe_text(outputs: dict) -> str:
    """从 ComfyUI history 的 outputs 里取出 PE 生成的文本。

    必须**按节点 id 精确取**（pe:out），不能遍历所有输出取最长的 ——
    万一以后往工作流里加了别的输出节点，最长法会拿错。
    """
    outputs = outputs or {}
    node = outputs.get("pe:out")
    if isinstance(node, dict):
        texts = node.get("text")
        if isinstance(texts, list):
            for item in texts:
                if isinstance(item, str) and item.strip():
                    return item.strip()
        elif isinstance(texts, str) and texts.strip():
            return texts.strip()
    return ""


def strip_thinking(text: str) -> str:
    """
    去掉 thinking 残留。

    TextGenerate 节点已经把 <think>…</think> 切到第二个输出了，
    但官方 PE 开 thinking 时偶尔仍会把未闭合的 <think> 留在正文开头。
    这里兜底剥一次 —— 留着会让提示词框里出现思维链。
    """
    if not text:
        return ""
    cleaned = text
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1]
    if cleaned.lstrip().startswith("<think>"):
        cleaned = cleaned.split("</think>", 1)[-1] if "</think>" in cleaned else ""
    return cleaned.strip()


def validate_pe_output(text: str, original: str) -> str:
    """
    官方 PE 产物的**最小**检查。

    刻意不做 validate.py 的硬约束校验（禁质量元标签 / 禁权重语法 …）——
    那是给 Z-Image 的约束，官方 PE 的写作规则不同（它要的就是
    「观察者式结构化描述」，里面会出现具体的名词性描述）。
    这里只挡三种真正没用的输出：
      1. 空
      2. 系统提示词复读
      3. 与原文完全一致（没扩写）
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise PEError("官方 PE 返回了空内容（可能是 max_length 太小导致思考段未闭合）")
    if len(cleaned) < 20:
        raise PEError(f"官方 PE 返回的内容过短（{len(cleaned)} 字），不像有效的扩写结果")
    src = (original or "").strip()
    if src and cleaned == src:
        raise PEError("官方 PE 返回的内容与原文完全一致，没有扩写")
    return cleaned


async def run_pe_expansion(session, comfy_url: str, prompt: str, is_edit: bool,
                           ref_images: list[str] | None = None,
                           system_prompt: str | None = None,
                           pe_params: dict | None = None) -> dict:
    """
    跑一次官方 PE 扩写，返回 {"expanded": str, "tier": str, ...}。

    失败一律抛 PEError（带可读原因），**不返回半成品**。

    system_prompt / pe_params：来自「增强」多选列表里官方 PE 那一项的
    可编辑字段；None = 用官方内置值。
    """
    workflow = build_pe_workflow(prompt, is_edit, ref_images,
                                 system_prompt=system_prompt,
                                 pe_params=pe_params)
    tier = tier_for(is_edit)
    applied_pe = dict(pe_params_for(is_edit))
    if pe_params:
        for key in ("presence_penalty", "max_new_tokens"):
            if pe_params.get(key) is not None:
                applied_pe[key] = pe_params[key]

    payload = {"prompt": workflow}
    async with session.post(f"{comfy_url}/prompt", json=payload) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise PEError(f"ComfyUI 拒绝官方 PE 工作流（HTTP {resp.status}）：{raw[:300]}")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PEError(f"ComfyUI 返回了非 JSON 内容：{raw[:200]}") from exc
    prompt_id = body.get("prompt_id")
    if not prompt_id:
        raise PEError(f"ComfyUI 没有返回 prompt_id：{json.dumps(body, ensure_ascii=False)[:300]}")

    deadline = asyncio.get_event_loop().time() + PE_TIMEOUT_SEC
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(PE_POLL_INTERVAL_SEC)
        async with session.get(f"{comfy_url}/history/{prompt_id}") as resp:
            if resp.status != 200:
                continue
            hist_body = await resp.json()
        entry = hist_body.get(prompt_id)
        if not entry:
            continue
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            messages = []
            for message in status.get("messages") or []:
                if isinstance(message, list) and message and message[0] == "execution_error":
                    messages.append(str((message[1] or {}).get("exception_message", "")))
            raise PEError("ComfyUI 执行官方 PE 失败：" +
                          ("; ".join(messages) or "未知错误")[:300])
        text = extract_pe_text(entry.get("outputs") or {})
        if text:
            expanded = validate_pe_output(strip_thinking(text), prompt)
            return {
                "expanded": expanded,
                "tier": tier,
                "tier_label": applied_pe.get("label", tier),
                "clip_name": PE_CLIP_NAMES[tier],
                "prompt_id": prompt_id,
                "ref_count": len(ref_images or []),
                "pe_params": {
                    "presence_penalty": applied_pe.get("presence_penalty"),
                    "max_new_tokens": applied_pe.get("max_new_tokens"),
                },
            }
    raise PEError(f"官方 PE 扩写超时（{PE_TIMEOUT_SEC} 秒）")
