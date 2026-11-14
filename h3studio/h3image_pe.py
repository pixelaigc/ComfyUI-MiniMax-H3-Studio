# -*- coding: utf-8 -*-
"""MiniMax H3-Image 自带的提示词扩写（PE）—— 独立成一次「增强」。

【为什么要有这个接口 —— 需求 16】
H3-Image 的**官方模板里本来就有 PE**，但它是在出图链路内部做的
（提示词送进模型前被 PE 改写），用户在前端看不到改写成了什么。
本项目此前**没有**把 H3-Image 的 PE 接进「增强」体系，
所以：
  * 用户没法预览 / 编辑扩写结果；
  * 内置 Agent 也没法用 H3-Image 的 PE（需求 2c 要求所有 Agent 生成
    都走「增强提示词」路线）。

【官方 PE 的真实结构（从本机 ComfyUI 官方模板反解）】
文件：comfyui_workflow_templates_json/templates/image_minimax_h3_image_t2i.json
      （其子图 "Text to Image (MiniMax H3 Image)" 里）

    CLIPLoader(qwen3.5_9b_w4a8_gptq.safetensors, type=minimax)
        -> TextGenerateV2
             system_prompt <- PrimitiveStringMultiline「Text (system_prompt)」
             prompt        <- StringReplace(task_prompt, "{input text}" <- user_prompt)
        -> JsonExtractString(json_string, key="expanded_caption")
        -> ComfySwitchNode(switch=enhance_prompt,
                          on_true=PE 结果, on_false=user_prompt 原样)
        -> MiniMaxH3ImageConditioning(prompt=switch 输出)

要点（照抄官方，不自己发明）：
  * PE 用的是 **qwen3.5_9b_w4a8_gptq.safetensors**（本机在 ComfyUI 的
    text_encoders 里，可被 CLIPLoader 选中）；
  * 节点是 **TextGenerateV2**（不是 TextGenerate）；
  * 输出是 **JSON**：`{"expanded_caption": "...", "aspect_ratio": "16:9"}`，
    要用 **JsonExtractString** 取 `expanded_caption`；
  * task_prompt 里 `{input text}` 会被 StringReplace 替换成用户原提示词；
  * system_prompt 只有一句（要求「先 thinking，最终只输出一个两键 JSON」），
    真正的写作规则在 task_prompt 里；
  * **aspect_ratio 也是 PE 的一部分** —— 官方 PE 会自己选比例。

【与 qwen_pe.py 的关系】
Qwen-Image 2.1 的官方 PE 是**纯文本**扩写（直接返回改写后的英文描述）；
H3-Image 的官方 PE 返回 **JSON**（描述 + 比例），且比例会影响出图尺寸。
所以本模块是独立实现，不能直接复用 qwen_pe.build_pe_workflow。

【与 /api/enhance 的区别 —— 不能混用】
/api/enhance 走本项目自己的 Provider（本地 LLM），用 skill 里的文本模板；
本模块走**官方 PE checkpoint 本身**（ComfyUI 的 TextGenerateV2）。
产物形态不同 —— 官方 PE 的产物允许出现具体名词性描述，
不能套 validate.py 那套给 Z-Image 的硬约束。
"""
from __future__ import annotations

import asyncio
import json

from h3studio import h3image as H3

#: 扩写一次的最长等待（秒）。官方 PE 是 9B 模型 + thinking，
#: 实测文生图档约 30~60 秒；留足余量但不能无限等。
PE_TIMEOUT_SEC = 900

#: 轮询 ComfyUI history 的间隔（秒）
PE_POLL_INTERVAL_SEC = 1.5

#: 官方 PE 的 checkpoint（本机 ComfyUI 的 text_encoders 里真实存在）
PE_CLIP_NAME = H3.H3_IMAGE_PE_CLIP

#: 官方 system prompt（来自官方模板的 PrimitiveStringMultiline）
#:
#: 注意模板里写的是 "must be exactly one JSON object with exactly two keys:
#: expanded_caption and aspect_ratio"，并且强调 ASCII 双引号要转义。
SYSTEM_PROMPT = (
    "Reason in the thinking section first. The visible final content must be "
    "exactly one JSON object with exactly two keys: expanded_caption and "
    "aspect_ratio. Every ASCII double quotation mark used as caption content "
    "inside expanded_caption must be serialized as the JSON escape sequence "
    '\\". Do not output text outside that object.'
)

#: 官方 task_prompt 的**开头**（写作规则的完整正文见官方模板文件）。
#:
#: 【为什么这里只放开头而不是整份】
#: 官方 task_prompt 有 200+ 行、约 12KB —— 全量塞进代码里会让本模块
#: 无法审阅、也无法与官方模板保持同步。
#: 本模块的定位是**接 H3-Image 的 PE**（需求 16），不是复刻官方的
#: 全部写作规则 —— 官方的完整规则仍然由官方 checkpoint 自己在
#: 出图链路里使用（面板「H3-Image」那一侧的 use_pe 语义不变）。
#:
#: 这里保留的是「契约部分」：模型该产出什么 JSON、怎么填 input_text、
#: 以及 caption 与 aspect_ratio 的取值范围 —— 这些是**解析端**必须知道的。
TASK_PROMPT_HEAD = (
    "You are a caption-expansion and canvas-selection model for still-image\n"
    "text-to-image generation. Rewrite input_text into a natural English detailed\n"
    "caption and choose one aspect ratio for the requested final image. After private\n"
    "reasoning, serialize the complete caption and aspect ratio together as the one\n"
    "required two-key JSON object; no visible analysis, markdown, or self-talk.\n"
    "The caption may be long when the requested image is a complex information or "
    "design artifact.\n\n"
    "Role: act like an expert visual designer and technical illustrator, not a word\n"
    "expander. Preserve every hard fact from the input, then complete the image with\n"
    "visible, inspectable content. The goal is not more words; the goal is that a\n"
    "viewer can inspect each important part of the image and find meaningful subjects,\n"
    "labels, relationships, materials, data, and hierarchy.\n\n"
    "Aspect-ratio decision policy:\n"
    "Choose exactly one of these allowed ratios: 1:1, 5:4, 4:3, 3:2, 16:9, 21:9,\n"
    "4:5, 3:4, 2:3, or 9:16. Decide in the following order; earlier evidence\n"
    "overrides later evidence.\n\n"
    "Universal hard rules:\n"
    "1. Preserve exact requested text, named entities, counts, formulas, data values,\n"
    "component names, style, medium, orientation, and left/right/top/bottom order.\n"
    "2. Required content must be visible, not implied.\n"
    "3. Respect requested subject count.\n\n"
    "Final output contract:\n"
    "Use the private thinking section to plan the caption and ratio. After thinking,\n"
    "the visible final answer must be exactly one JSON object with exactly these two\n"
    "keys:\n\n"
    '{{"expanded_caption":"ONE_DETAILED_ENGLISH_CAPTION","aspect_ratio":"ALLOWED_RATIO"}}\n\n'
    "Replace ONE_DETAILED_ENGLISH_CAPTION with one fluent English caption paragraph\n"
    "that follows every rule above, and ALLOWED_RATIO with exactly one allowed ratio\n"
    "string. Keep expanded_caption as one valid JSON string. Every ASCII double\n"
    "quotation mark used as caption content inside expanded_caption must be\n"
    "serialized as the JSON escape sequence \\\". Escape all other characters as valid\n"
    "JSON requires. Do not add or omit keys. Do not put thinking or think tags inside\n"
    "the object. Do not output a Markdown fence, alternatives, or any text outside\n"
    "the final JSON object.\n\n"
    "input_text: {input text}\n"
    "output_text:\n"
)

#: PE 允许选的比例（官方 task_prompt 里列的）
ALLOWED_RATIOS = ("1:1", "5:4", "4:3", "3:2", "16:9", "21:9",
                  "4:5", "3:4", "2:3", "9:16")


class PEError(RuntimeError):
    """H3-Image 官方 PE 扩写失败。message 面向用户，直接可读。"""


def build_pe_workflow(prompt: str,
                      ref_images: list[str] | None = None,
                      task_prompt: str | None = None) -> dict:
    """构造 H3-Image 官方 PE 扩写的 ComfyUI 工作流（API 格式）。

    链路（照抄官方模板的子图）：

        CLIPLoader(qwen3.5_9b_w4a8_gptq) ─┐
        PrimitiveStringMultiline(system) ─┼-> TextGenerateV2
        StringReplace(task_prompt <- user) ┘        │
                                                     v
                                          JsonExtractString(expanded_caption)
                                                     │
                                                     v
                                             PreviewAny（输出节点）

    【为什么带 PreviewAny】ComfyUI 要求工作流里必须有输出节点，
    否则报 prompt_no_outputs。

    【参考图】图片编辑时官方把参考图**同时**喂给 PE（TextGenerateV2 的
    images.imageN），这样 PE 才能「看着图」改写编辑指令。
    本模块保持同样接法；顺序必须与占位标记 <imageN> 严格一致。
    """
    task = (task_prompt if task_prompt is not None else TASK_PROMPT_HEAD)
    if "{input text}" not in task:
        raise PEError("H3-Image PE 的 task prompt 缺少 {input text} 占位符")

    workflow: dict = {
        "pe:clip": {
            "inputs": {"clip_name": PE_CLIP_NAME, "type": "minimax",
                       "device": "default"},
            "class_type": "CLIPLoader",
        },
        "pe:sys": {
            "inputs": {"value": SYSTEM_PROMPT},
            "class_type": "PrimitiveStringMultiline",
        },
        "pe:task": {
            "inputs": {"value": task},
            "class_type": "PrimitiveStringMultiline",
        },
        "pe:user": {
            "inputs": {"value": prompt},
            "class_type": "PrimitiveStringMultiline",
        },
        # 把 {input text} 换成用户原提示词（官方用的是 StringReplace）
        "pe:replace": {
            "inputs": {"string": ["pe:task", 0], "find": "{input text}",
                       "replace": ["pe:user", 0]},
            "class_type": "StringReplace",
        },
    }

    pe_inputs = {
        "clip": ["pe:clip", 0],
        "prompt": ["pe:replace", 0],
        # 官方模板用的是 8192；不能压到 4096 —— 官方文档警告
        # 「reasoning block 用完 4096 预算时不会闭合，enhancer 返回空串」。
        "max_length": 8192,
        "sampling_mode": "on",
        "sampling_mode.temperature": 1.0,
        "sampling_mode.top_k": 20,
        "sampling_mode.top_p": 0.95,
        "sampling_mode.min_p": 0,
        "sampling_mode.repetition_penalty": 1,
        "sampling_mode.seed": 0,
        "sampling_mode.presence_penalty": 0.0,
        # thinking 必须开（官方 PE 模型卡：thinking 必开）
        "thinking": True,
        "use_default_template": True,
        "mtp": "auto",
        "system_prompt": ["pe:sys", 0],
    }

    # 参考图也喂给 PE（官方 PE-I2I 工作流就是这么接的）
    if ref_images:
        load_nodes = []
        for index, filename in enumerate(ref_images):
            node_id = f"pe:ref{index}"
            workflow[node_id] = {
                "inputs": {"image": filename},
                "class_type": "LoadImage",
            }
            load_nodes.append(node_id)
        workflow["pe:batch"] = {
            "inputs": {f"images.image{i + 1}": [node_id, 0]
                       for i, node_id in enumerate(load_nodes)},
            "class_type": "BatchImagesNode",
        }
        pe_inputs["image"] = ["pe:batch", 0]

    workflow["pe:gen"] = {"inputs": pe_inputs, "class_type": "TextGenerateV2"}
    # 从 JSON 里取 expanded_caption
    workflow["pe:json"] = {
        "inputs": {"json_string": ["pe:gen", 0], "key": "expanded_caption"},
        "class_type": "JsonExtractString",
    }
    # 【为什么还要第二个提取器】官方 PE 的输出是一个 JSON 对象
    # （expanded_caption + aspect_ratio），JsonExtractString 一次只取一个键。
    # caption 是主产物（走 PreviewAny 出去），aspect_ratio 是附加信息
    # （单独取一份，供出图时对齐比例）。
    workflow["pe:json_ratio"] = {
        "inputs": {"json_string": ["pe:gen", 0], "key": "aspect_ratio"},
        "class_type": "JsonExtractString",
    }
    workflow["pe:out"] = {
        "inputs": {"source": ["pe:json", 0]},
        "class_type": "PreviewAny",
    }
    workflow["pe:out_ratio"] = {
        "inputs": {"source": ["pe:json_ratio", 0]},
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


def extract_pe_ratio(outputs: dict) -> str:
    """从 outputs 里取出 PE 选出的画面比例（pe:out_ratio 节点）。"""
    outputs = outputs or {}
    node = outputs.get("pe:out_ratio")
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
    """去掉 thinking 残留。

    TextGenerateV2 节点已经把 <think>…</think> 切到第二个输出了，
    但官方 PE 开 thinking 时偶尔仍会把未闭合的 <think> 留在正文开头。
    这里兜底剥一次 —— 留着会让提示词框里出现思维链。
    """
    if not text:
        return ""
    cleaned = text
    if "</think>" in cleaned:
        cleaned = cleaned.split("</think>", 1)[1]
    if cleaned.lstrip().startswith("<think>"):
        cleaned = (cleaned.split("</think>", 1)[-1]
                   if "</think>" in cleaned else "")
    return cleaned.strip()


def parse_pe_json(text: str) -> tuple[str, str]:
    """解析 PE 的输出 -> (expanded_caption, aspect_ratio)。

    【两种输入形态都要能吃】
    1. **整个 JSON**：`{"expanded_caption": "...", "aspect_ratio": "16:9"}`
       —— 官方 PE 节点的原始输出。
    2. **已提取的纯 caption**：JsonExtractString 已经把 expanded_caption
       取出来了 —— 这才是**正常路径**（工作流里就是这么接的）。
       这时没有 JSON 可解析，caption 就是整段文本，比例另取。

    兼容处理：
      * 外面套 ```json 代码围栏 -> 剥掉；
      * 前面有 thinking 残留 -> 剥掉；
      * aspect_ratio 不在允许列表里 -> 丢弃比例（**不丢 caption**）。

    【为什么不因为比例不合法就丢掉整条 caption】
    caption 才是用户要的东西；比例只是附加信息。
    """
    raw = (text or "").strip()
    if not raw:
        raise PEError("H3-Image PE 返回了空内容")
    # 去掉 markdown 围栏
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw
        if raw.lstrip().lower().startswith("json"):
            raw = raw.lstrip()[4:]
        raw = raw.strip()
    # 找第一个 { 到最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    blob = raw[start:end + 1] if start >= 0 and end > start else ""
    if not blob:
        # 不是 JSON —— 这就是「已提取好的纯 caption」（正常路径）
        return raw, ""
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        # 提取器兜底返回了原文（模型没按 JSON 输出）—— 当作纯 caption
        return raw, ""
    if not isinstance(data, dict):
        return raw, ""
    caption = str(data.get("expanded_caption") or "").strip()
    if not caption:
        # JSON 里没有 expanded_caption -> 整段当 caption
        return raw, ""
    ratio = str(data.get("aspect_ratio") or "").strip()
    if ratio not in ALLOWED_RATIOS:
        ratio = ""
    return caption, ratio


def validate_pe_output(caption: str, original: str) -> str:
    """官方 PE 产物的**最小**检查。

    刻意不做 validate.py 的硬约束校验（禁质量元标签 / 禁权重语法 …）——
    那是给 Z-Image 的约束，官方 PE 的写作规则不同。
    这里只挡三种真正没用的输出：
      1. 空
      2. 太短
      3. 与原文完全一致（没扩写）
    """
    cleaned = (caption or "").strip()
    if not cleaned:
        raise PEError("H3-Image PE 返回了空内容（可能是 max_length 太小导致思考段未闭合）")
    if len(cleaned) < 20:
        raise PEError(f"H3-Image PE 返回的内容过短（{len(cleaned)} 字），不像有效的扩写结果")
    src = (original or "").strip()
    if src and cleaned == src:
        raise PEError("H3-Image PE 返回的内容与原文完全一致，没有扩写")
    return cleaned


async def run_pe_expansion(session, comfy_url: str, prompt: str,
                           ref_images: list[str] | None = None,
                           task_prompt: str | None = None) -> dict:
    """跑一次 H3-Image 官方 PE 扩写。

    返回 {"expanded": str, "aspect_ratio": str, ...}。
    失败一律抛 PEError（带可读原因），**不返回半成品**。
    """
    workflow = build_pe_workflow(prompt, ref_images, task_prompt=task_prompt)

    payload = {"prompt": workflow}
    async with session.post(f"{comfy_url}/prompt", json=payload) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise PEError(f"ComfyUI 拒绝 H3-Image PE 工作流（HTTP {resp.status}）：{raw[:300]}")
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
                if (isinstance(message, list) and message
                        and message[0] == "execution_error"):
                    messages.append(str((message[1] or {}).get("exception_message", "")))
            raise PEError("ComfyUI 执行 H3-Image PE 失败："
                          + ("; ".join(messages) or "未知错误")[:300])
        text = extract_pe_text(entry.get("outputs") or {})
        if text:
            caption = strip_thinking(text)
            ratio = strip_thinking(extract_pe_ratio(entry.get("outputs") or {}))
            # caption 已经是 JsonExtractString 取出来的 expanded_caption
            # （**不是**整个 JSON）—— 但为了兼容「模型没按 JSON 输出、
            # 提取器兜底返回原文」的情况，parse_pe_json 会先试 JSON、
            # 失败就把整段当 caption。
            caption, ratio_from_json = parse_pe_json(caption)
            if not ratio:
                ratio = ratio_from_json
            if ratio not in ALLOWED_RATIOS:
                ratio = ""
            expanded = validate_pe_output(caption, prompt)
            return {
                "expanded": expanded,
                "aspect_ratio": ratio,
                "clip_name": PE_CLIP_NAME,
                "prompt_id": prompt_id,
                "ref_count": len(ref_images or []),
            }
    raise PEError(f"H3-Image PE 扩写超时（{PE_TIMEOUT_SEC} 秒）")
