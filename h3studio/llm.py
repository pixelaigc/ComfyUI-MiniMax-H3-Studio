# -*- coding: utf-8 -*-
"""
H3 Studio 本地模型调用层。

把不同 Provider 类型统一成一次「发消息拿文本」的调用：
  openai / llamacpp  → {base_url}/v1/chat/completions
  ollama             → {base_url}/api/chat        （原生协议，支持图片数组）
  comfyui            → 提交 ComfyUI 工作流，用 VRGDG_LocalLLM 节点跑

设计原则：
  1. 任何失败都必须抛出带原因的中文异常，绝不静默返回空串。
  2. 调用方只认 Provider 字典，不关心具体协议。
  3. 多模态走 images 字段（OpenAI 兼容用 data URL，Ollama 用 base64 原图）。
"""
import asyncio
import base64
import json
import mimetypes
import time

from aiohttp import ClientSession, ClientTimeout

# 超时：本地模型冷启动可能较慢，给足时间
CHAT_TIMEOUT = ClientTimeout(total=180, connect=10)
HEALTH_TIMEOUT = ClientTimeout(total=8, connect=5)

# ComfyUI 节点名（vrgamedevgirl 插件的本地 LLM 节点，支持图片输入）
COMFY_LLM_NODE = "VRGDG_LocalLLM"
COMFY_LLM_NODE_ALT = "AI_Ollama_text"


class ProviderError(RuntimeError):
    """Provider 调用失败。message 面向用户，直接可读。"""


# ------------------------------------------------------------------ 工具


def _auth_headers(provider: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    key = (provider.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _data_url(image_path: str) -> str:
    """本地图片 -> data URL（OpenAI 兼容多模态格式）"""
    with open(image_path, "rb") as handle:
        raw = handle.read()
    mime = mimetypes.guess_type(image_path)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _messages_to_openai(messages: list, images: list | None) -> list:
    """把统一消息格式转成 OpenAI 兼容 content。多模态图片附在最后一条 user 消息上。"""
    out = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        out.append({"role": role, "content": content})
    if images:
        parts = []
        for message in out:
            if message["role"] == "user":
                parts.append({"type": "text", "text": message["content"]})
                for path in images:
                    parts.append({"type": "image_url",
                                  "image_url": {"url": _data_url(path)}})
                message["content"] = parts
                break
        else:
            parts.append({"type": "text", "text": ""})
            for path in images:
                parts.append({"type": "image_url",
                              "image_url": {"url": _data_url(path)}})
            out.append({"role": "user", "content": parts})
    return out


def _messages_to_ollama(messages: list, images: list | None) -> list:
    """Ollama 原生 /api/chat 格式，图片是 base64 裸串数组。"""
    out = []
    for message in messages:
        entry = {"role": message.get("role", "user"),
                 "content": message.get("content", "")}
        if images and entry["role"] == "user":
            entry["images"] = []
            for path in images:
                with open(path, "rb") as handle:
                    entry["images"].append(base64.b64encode(handle.read()).decode("ascii"))
        out.append(entry)
    return out


def _strip_thinking(text: str) -> str:
    """
    去掉 reasoning 模型混进 content 的思考痕迹。

    两类来源：
      1. 显式围栏 <think>...</think> / <thinking>...</thinking>
      2. 未闭合的 <think> 起始（截断在思考中途）
    """
    if not text:
        return ""
    result = text
    for tag in ("think", "thinking", "reasoning"):
        open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        while open_tag in result.lower():
            lower = result.lower()
            start = lower.find(open_tag)
            end = lower.find(close_tag, start)
            if end == -1:
                # 未闭合：思考被截断，丢弃其后全部内容
                result = result[:start]
                break
            result = result[:start] + result[end + len(close_tag):]
    return result.strip()


def _extract_openai_text(body: dict) -> str:
    choices = body.get("choices") or []
    if not choices:
        raise ProviderError(f"模型返回里没有 choices：{json.dumps(body, ensure_ascii=False)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if text is None:
        # 兼容 reasoning 模型：content 可能为空但有 reasoning_content
        text = message.get("reasoning_content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("模型返回了空内容")
    cleaned = _strip_thinking(text)
    if not cleaned:
        raise ProviderError("模型只返回了思考内容，没有最终答案")
    return cleaned.strip()


def _extract_ollama_text(body: dict) -> str:
    message = body.get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("模型返回了空内容")
    return text.strip()


# ------------------------------------------------------------------ 连通性


async def check_provider(provider: dict, session: ClientSession | None = None) -> dict:
    """
    连通性探测。返回 {"ok": bool, "detail": str, "models": [...]}。
    只探测，不生成内容 —— 快，且不占 GPU。
    """
    ptype = provider.get("type")
    base = (provider.get("base_url") or "").rstrip("/")
    name = provider.get("name") or provider.get("id")
    if not base:
        return {"ok": False, "detail": "Base URL 为空", "models": []}

    own = session is None
    session = session or ClientSession(timeout=HEALTH_TIMEOUT)
    try:
        if ptype == "ollama":
            async with session.get(f"{base}/api/tags") as resp:
                if resp.status != 200:
                    return {"ok": False,
                            "detail": f"HTTP {resp.status}",
                            "models": []}
                body = await resp.json()
            models = [m.get("name") for m in (body.get("models") or []) if m.get("name")]
            return {"ok": True, "detail": f"连接正常，发现 {len(models)} 个模型",
                    "models": models}

        if ptype == "comfyui":
            async with session.get(f"{base}/object_info/{COMFY_LLM_NODE}") as resp:
                if resp.status == 200:
                    return {"ok": True,
                            "detail": f"连接正常，检测到节点 {COMFY_LLM_NODE}",
                            "models": []}
                if resp.status == 404:
                    return {"ok": False,
                            "detail": f"ComfyUI 可达，但未安装 {COMFY_LLM_NODE} 节点",
                            "models": []}
                return {"ok": False, "detail": f"HTTP {resp.status}", "models": []}

        # openai / llamacpp
        headers = _auth_headers(provider)
        async with session.get(f"{base}/v1/models", headers=headers) as resp:
            if resp.status == 200:
                body = await resp.json()
                models = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
                return {"ok": True, "detail": f"连接正常，发现 {len(models)} 个模型",
                        "models": models}
            if resp.status in (401, 403):
                return {"ok": False, "detail": f"鉴权被拒（HTTP {resp.status}），请检查 API Key",
                        "models": []}
            # 有些兼容服务不实现 /v1/models，退回 /v1/models 后仍探测一次根路径
            async with session.get(f"{base}/", headers=headers) as resp2:
                if resp2.status < 500:
                    return {"ok": True,
                            "detail": "服务可达（该服务未提供 /v1/models 模型列表接口）",
                            "models": []}
                return {"ok": False, "detail": f"HTTP {resp.status}", "models": []}
    except asyncio.TimeoutError:
        return {"ok": False, "detail": f"连接 {name} 超时（{base}）", "models": []}
    except Exception as exc:
        return {"ok": False,
                "detail": f"无法连接 {name}（{base}）：{type(exc).__name__} {exc}"[:200],
                "models": []}
    finally:
        if own:
            await session.close()


# ------------------------------------------------------------------ 调用


async def call_provider(provider: dict, messages: list, images: list | None = None,
                        options: dict | None = None) -> str:
    """
    调用 Provider 返回文本。失败一律抛 ProviderError（带可读原因）。
    """
    options = options or {}
    ptype = provider.get("type")
    base = (provider.get("base_url") or "").rstrip("/")
    model = provider.get("model") or ""
    name = provider.get("name") or provider.get("id") or "本地模型"

    if not base:
        raise ProviderError(f"Provider「{name}」的 Base URL 为空")
    if not model:
        raise ProviderError(f"Provider「{name}」的模型名为空")

    temperature = float(options.get("temperature", 0.7))
    max_tokens = int(options.get("max_tokens", 1024))
    top_p = float(options.get("top_p", 0.95))

    if images and not provider.get("multimodal"):
        raise ProviderError(
            f"Provider「{name}」未标记多模态能力，无法读图。"
            "请在本地模型配置里勾选「多模态」，或改用默认多模态模型。")

    timeout = ClientTimeout(total=float(options.get("timeout", 180)), connect=10)
    async with ClientSession(timeout=timeout) as session:
        try:
            if ptype == "ollama":
                payload = {
                    "model": model,
                    "messages": _messages_to_ollama(messages, images),
                    "stream": False,
                    "options": {"temperature": temperature, "top_p": top_p,
                                "num_predict": max_tokens},
                }
                async with session.post(f"{base}/api/chat", json=payload,
                                        headers=_auth_headers(provider)) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        raise ProviderError(
                            f"Ollama 返回 HTTP {resp.status}：{raw[:300]}")
                    body = json.loads(raw)
                return _extract_ollama_text(body)

            if ptype == "comfyui":
                return await _call_via_comfyui(session, provider, messages, images,
                                               temperature, top_p, max_tokens)

            # openai / llamacpp
            payload = {
                "model": model,
                "messages": _messages_to_openai(messages, images),
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
                "stream": False,
            }
            # greedy 档：复刻 BFL 官方 upsampler 的贪心解码行为
            if not options.get("do_sample", True):
                payload["temperature"] = 0.0
                payload["top_p"] = 1.0
            # reasoning 模型（如 Qwen3 thinking）会把思考过程混进 content，
            # 提示词增强场景必须拿到纯净结果，所以优先关掉 thinking。
            # 部分服务不认识这个参数，报错就退回不带参数重试一次。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            async with session.post(f"{base}/v1/chat/completions", json=payload,
                                    headers=_auth_headers(provider)) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    if "chat_template_kwargs" in raw or "enable_thinking" in raw:
                        payload.pop("chat_template_kwargs", None)
                        async with session.post(f"{base}/v1/chat/completions",
                                                json=payload,
                                                headers=_auth_headers(provider)) as resp2:
                            raw = await resp2.text()
                            if resp2.status != 200:
                                raise ProviderError(
                                    f"模型返回 HTTP {resp2.status}：{raw[:300]}")
                            body = json.loads(raw)
                    else:
                        raise ProviderError(
                            f"模型返回 HTTP {resp.status}：{raw[:300]}")
                else:
                    body = json.loads(raw)
            return _extract_openai_text(body)
        except ProviderError:
            raise
        except asyncio.TimeoutError:
            raise ProviderError(
                f"调用 Provider「{name}」超时（{max_tokens} tokens 上限，"
                f"模型可能未加载或过慢）")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Provider「{name}」返回的不是合法 JSON：{exc}")
        except Exception as exc:
            raise ProviderError(
                f"调用 Provider「{name}」失败：{type(exc).__name__} {exc}"[:300])


# ------------------------------------------------------------------ ComfyUI 节点


def _comfy_thinking_safe_prompts(user_prompt: str, system_prompt: str):
    """
    为 VRGDG_LocalLLM 节点改写提示词，规避 thinking 模型的问题。

    背景：该节点自己拼 payload（没有 chat_template_kwargs），所以关不掉 thinking。
    对 Qwen3 这类模型，一旦模板诱发推理链，OpenAI 兼容接口返回的
    `content` 就是 null，节点再 `str(None)` 变成字符串 "None" ——
    提示词彻底丢失。

    两个手段：
      1. 在 system 末尾追加显式的「不要输出思考过程，直接给最终结果」指令
      2. 把会诱发推理链的模板措辞（首先/接着/然后/最后 的工作流式描述）
         换成直接的执行式描述

    返回 (prompt, system_prompt)。
    """
    system = system_prompt or ""
    # 把推理链诱因的顺序措辞改成直接执行式
    for src, dst in (
        ("首先，你会分析并锁定", "直接锁定"),
        ("接着，你会判断提示词是否需要", "若需要，先在脑中构思，然后"),
        ("然后，当核心画面确立后", "在核心画面确立后"),
        ("最后，是对所有文字元素的精确处理", "对所有文字元素做精确处理"),
        ("你的工作流程严格遵循一个逻辑序列：", "你的工作方式："),
    ):
        system = system.replace(src, dst)

    guard = (
        "\n\n【输出纪律】不要输出任何思考过程、分析步骤或推理说明，"
        "不要以任何前缀开场。直接输出最终的提示词正文，"
        "第一个字符就是提示词的第一个字。"
    )
    if "【输出纪律】" not in system:
        system += guard

    prompt = user_prompt or ""
    prompt_guard = "\n\n（直接输出扩写后的提示词，不要解释，不要思考过程。）"
    if "不要解释" not in prompt:
        prompt += prompt_guard
    return prompt, system


async def _call_via_comfyui(session: ClientSession, provider: dict, messages: list,
                            images: list | None, temperature: float, top_p: float,
                            max_tokens: int) -> str:
    """
    走 ComfyUI 工作流调用本地 LLM 节点。

    工作流：LoadImage(可选) -> VRGDG_LocalLLM -> PreviewAny
    用 /prompt 提交，再轮询 /history 取回文本。

    两个 Base URL 的区别：
      provider["base_url"]      ComfyUI 自己的地址（提交工作流用）
      provider["llm_base_url"]  节点内部去连的 LLM 服务地址（可为空 = 用节点默认值）
    """
    base = provider["base_url"].rstrip("/")
    model = provider["model"]
    api_key = provider.get("api_key") or ""
    backend = provider.get("backend") or "openai_compatible"
    # 节点去连的 LLM 服务地址：优先取专门字段，没有就用 ComfyUI 地址
    llm_base = (provider.get("llm_base_url") or "").strip() or base
    if backend == "openai_compatible":
        # VRGDG_LocalLLM 节点拼的是 {base_url}/chat/completions（没有 /v1），
        # 所以这里必须给出带 /v1 的地址，否则节点会打到 404 并返回空文本。
        if not llm_base.rstrip("/").endswith("/v1"):
            llm_base = llm_base.rstrip("/") + "/v1"
        # thinking 模型（Qwen3 等）：节点无法关 thinking，会拿到 content=null。
        # 若配置了 use_proxy，走 H3 Studio 自己的 OpenAI 兼容代理转发，
        # 由代理强制 enable_thinking=false 并兜底 reasoning_content。
        if provider.get("use_proxy"):
            llm_base = provider.get("proxy_base_url") or llm_base

    system_prompt = ""
    user_prompt = ""
    for message in messages:
        if message.get("role") == "system":
            system_prompt = message.get("content", "")
        elif message.get("role") == "user":
            user_prompt = message.get("content", "")

    # VRGDG_LocalLLM 节点有自己的 payload（不含 chat_template_kwargs），
    # 遇到 thinking 模型时它拿到的 content 会是 null -> 节点把 Python 的
    # None 转成字符串 "None"。官方 PE 模板正好会触发 thinking。
    # 解决：把 system/user 消息改写成「非 thinking 也能听懂的」形式 ——
    # 用更强的指令前缀显式要求「不要输出思考过程」，并去掉会诱发
    # 推理链的模板措辞（官方模板里的「首先/接着/然后/最后」工作流描述
    # 本身就是推理诱因）。
    node_prompt, node_system = _comfy_thinking_safe_prompts(
        user_prompt, system_prompt)

    node_id = "10"
    workflow = {
        node_id: {
            "inputs": {
                "backend": backend,
                "base_url": llm_base,
                "api_key": api_key,
                "model": model,
                "custom_model": "",
                "prompt": node_prompt,
                "system_prompt": node_system,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
                "request_timeout_sec": 300,
            },
            "class_type": COMFY_LLM_NODE,
            "_meta": {"title": "本地 LLM"},
        },
        # ComfyUI 要求工作流里必须有输出节点，否则报 prompt_no_outputs
        "20": {
            "inputs": {"source": [node_id, 0]},
            "class_type": "PreviewAny",
            "_meta": {"title": "输出"},
        },
        # 节点第 4 个返回值是 status（"ok" 或具体错误原因）——
        # 节点自己会吞掉异常并返回空 text，靠这个输出才能诊断
        "21": {
            "inputs": {"source": [node_id, 3]},
            "class_type": "PreviewAny",
            "_meta": {"title": "状态"},
        },
    }

    # 有图则先上传到 ComfyUI input，再用 LoadImage 接进 image1
    if images:
        uploaded = await _comfy_upload_images(session, base, images)
        workflow["1"] = {"inputs": {"image": uploaded[0]}, "class_type": "LoadImage",
                         "_meta": {"title": "加载参考图"}}
        workflow[node_id]["inputs"]["image1"] = ["1", 0]

    async with session.post(f"{base}/prompt", json={"prompt": workflow}) as resp:
        raw = await resp.text()
        if resp.status != 200:
            raise ProviderError(f"ComfyUI 提交失败（HTTP {resp.status}）：{raw[:300]}")
        body = json.loads(raw)
    prompt_id = body.get("prompt_id")
    if not prompt_id:
        raise ProviderError(f"ComfyUI 没有返回 prompt_id：{raw[:300]}")

    deadline = time.time() + 300
    empty_reported = False
    while time.time() < deadline:
        await asyncio.sleep(1.2)
        async with session.get(f"{base}/history/{prompt_id}") as resp:
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
            raise ProviderError("ComfyUI 节点执行失败：" + ("; ".join(messages) or "未知错误")[:300])
        # VRGDG_LocalLLM 有 5 个输出（text/used_backend/used_model/status/image），
        # PreviewAny 会把它们都带进 outputs。必须精确取 text 那个，
        # 否则会拿到 "None" 之类的其它返回值。
        text_value = _comfy_extract_text(entry.get("outputs") or {})
        if text_value:
            return text_value
        # 节点跑完了但文本是空的 —— 节点自己会吞掉异常，真实原因在 status 里
        if status.get("completed") and not empty_reported:
            empty_reported = True
            node_status = await _comfy_node_status(session, base, prompt_id)
            if node_status and node_status.lower() not in ("ok", ""):
                raise ProviderError(f"ComfyUI 节点执行失败：{node_status}"[:300])
            raise ProviderError(
                "ComfyUI 节点返回了空文本。"
                "请检查「LLM 服务地址」是否正确（openai_compatible 后端需要以 /v1 结尾）。")
    raise ProviderError("ComfyUI 节点执行超时（300 秒）")


def _comfy_extract_text(outputs: dict) -> str:
    """
    从 ComfyUI history 的 outputs 里精确取出 LLM 节点的文本输出。

    VRGDG_LocalLLM 有 5 个输出：text / used_backend / used_model / status / image。
    工作流里：
      "20" 接的是 text   —— 要的就是它
      "21" 接的是 status —— 是诊断信息，不是提示词
    所以必须按节点 id 取，不能遍历所有输出（会拿到 "ok" 或 "None"）。
    """
    outputs = outputs or {}
    # 优先按已知节点 id 取
    for node_id in ("20",):
        node_outputs = outputs.get(node_id)
        if isinstance(node_outputs, dict):
            texts = node_outputs.get("text")
            if isinstance(texts, list):
                for item in texts:
                    if isinstance(item, str) and item.strip():
                        return item.strip()
            elif isinstance(texts, str) and texts.strip():
                return texts.strip()
    # 回退：取所有节点里最长的字符串（提示词一定比 status/None 长）
    candidates = []
    for node_outputs in outputs.values():
        if not isinstance(node_outputs, dict):
            continue
        for value in node_outputs.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip())
            elif isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    if not candidates:
        return ""
    good = [c for c in candidates
            if len(c) >= 20 and c.lower() not in ("none", "null", "ok", "true", "false")]
    if good:
        return max(good, key=len)
    return ""


async def _comfy_node_status(session: ClientSession, base: str, prompt_id: str) -> str:
    """
    取 VRGDG_LocalLLM 节点的 status 输出（第 4 个返回值），用于诊断。
    节点会吞掉异常并把错误原因放在 status 里（text 则为空）。
    """
    async with session.get(f"{base}/history/{prompt_id}") as resp:
        if resp.status != 200:
            return ""
        body = await resp.json()
    entry = body.get(prompt_id) or {}
    outputs = entry.get("outputs") or {}
    # 节点 "21" 接的是 status（第 4 个返回值）
    node21 = outputs.get("21")
    if isinstance(node21, dict):
        texts = node21.get("text")
        if isinstance(texts, list) and texts and isinstance(texts[0], str):
            return texts[0]
    # 回退：扫描所有输出，找看起来像状态的那个
    for node_outputs in outputs.values():
        if not isinstance(node_outputs, dict):
            continue
        texts = node_outputs.get("text")
        if isinstance(texts, list) and texts and isinstance(texts[0], str):
            value = texts[0].strip()
            if value.lower() in ("ok", "error") or value.startswith("error:"):
                return value
    return ""


async def _comfy_upload_images(session: ClientSession, base: str, images: list) -> list:
    """上传本地图片到 ComfyUI input 目录，返回可用的文件名列表。"""
    import aiohttp

    names = []
    for path in images:
        filename = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        data = aiohttp.FormData()
        with open(path, "rb") as handle:
            data.add_field("image", handle, filename=filename,
                           content_type="application/octet-stream")
        data.add_field("subfolder", "h3studio")
        data.add_field("overwrite", "true")
        async with session.post(f"{base}/upload/image", data=data) as resp:
            raw = await resp.text()
            if resp.status != 200:
                raise ProviderError(f"上传参考图到 ComfyUI 失败：{raw[:200]}")
            body = json.loads(raw)
        sub = body.get("subfolder", "")
        name = body.get("name", filename)
        names.append(f"{sub}/{name}" if sub else name)
    return names
