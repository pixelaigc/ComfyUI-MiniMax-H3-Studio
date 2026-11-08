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

    system_prompt = ""
    user_prompt = ""
    for message in messages:
        if message.get("role") == "system":
            system_prompt = message.get("content", "")
        elif message.get("role") == "user":
            user_prompt = message.get("content", "")

    node_id = "10"
    workflow = {
        node_id: {
            "inputs": {
                "backend": backend,
                "base_url": llm_base,
                "api_key": api_key,
                "model": model,
                "custom_model": "",
                "prompt": user_prompt,
                "system_prompt": system_prompt,
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
        for output in (entry.get("outputs") or {}).values():
            for value in output.values():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            return item.strip()
        # 节点跑完了但文本是空的 —— 常见于 LLM 服务地址不对（节点会静默吞掉 404）
        if status.get("completed") and not empty_reported:
            empty_reported = True
            node_status = await _comfy_node_status(session, base, prompt_id)
            detail = f"（节点状态：{node_status}）" if node_status else ""
            raise ProviderError(
                f"ComfyUI 节点返回了空文本{detail}。"
                f"请检查「LLM 服务地址」是否正确（openai_compatible 后端需要以 /v1 结尾）。")
    raise ProviderError("ComfyUI 节点执行超时（300 秒）")


async def _comfy_node_status(session: ClientSession, base: str, prompt_id: str) -> str:
    """取 VRGDG_LocalLLM 节点的 status 输出（第 4 个返回值），用于诊断。"""
    async with session.get(f"{base}/history/{prompt_id}") as resp:
        if resp.status != 200:
            return ""
        body = await resp.json()
    entry = body.get(prompt_id) or {}
    for output in (entry.get("outputs") or {}).values():
        texts = output.get("text")
        if isinstance(texts, list) and texts and isinstance(texts[0], str):
            return texts[0]
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
