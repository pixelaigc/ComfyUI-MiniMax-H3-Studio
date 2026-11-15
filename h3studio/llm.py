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

# 超时：本地模型冷启动可能较慢，给足时间。
# 注意：单次生成的超时必须**随 max_tokens 放大** —— 逐物体枚举档
# 一次要生成 2000+ tokens，本机实测 1646 tokens 用了 154.6 秒。
# 固定 180 秒会在模型**已经快写完**时被我们自己掐断，
# 表现为「输出被截断」但其实后端根本没截 —— 极易误判。
CHAT_TIMEOUT = ClientTimeout(total=180, connect=10)
HEALTH_TIMEOUT = ClientTimeout(total=8, connect=5)

# 单次生成的超时基数与每 token 的增量估算。
# 经验值：本机 llama.cpp（Qwen3.8 flash IQ3）实测约 10.6 tokens/秒
#   —— 1646 tokens 用了 154.6 秒（10.6 tok/s）；2254 tokens 用了 ~210 秒。
#   PER_TOKEN_TIMEOUT = 0.12 秒/token ≈ 8.3 tok/s，**留了约 25% 余量**
#   （用 0.094 会刚好卡在实测值上，不留余量）。
BASE_TIMEOUT = 120.0
PER_TOKEN_TIMEOUT = 0.12
MIN_TIMEOUT = 120.0
#: 单次调用的超时上限（秒）。
#:
#: 【2026-11-09 下调 1800 -> 600】
#: 上限从 1800 收到 600 的理由：/api/enhance 一次请求最坏会挂住整个
#: 连接（本机 llama.cpp 单槽串行），1800 秒意味着「一个请求能把服务
#: 拖住 30 分钟」。实测 2254 tokens 约 210 秒，600 秒有近 3 倍余量；
#: 并发闸门只能挡并发数、挡不住单次挂起时长。
#: 配合 server.py 的 ENHANCE_GATE 并发闸门（见该常量处的取值说明），
#: 最坏情况 = 并发上限 × 600 秒。
MAX_TIMEOUT = 600.0

#: 【2026-11-16 修 c9】共享单槽 Provider 的**排队余量**（秒）。
#:
#: 实测（本机 Strata，pid 15495，单槽串行）：我们自己的请求实际算力开销只有
#: `prompt_ms=434 + predicted_ms=67` ⇒ **0.5 秒**（`reasoning_content` 长度 = 0，
#: 说明 `enable_thinking=false` 是被上游尊重的），
#: 但 HTTP 端到端耗时 **90.5 秒** —— 差额全是**排队等别人的请求**。
#: 同期上游日志显示别的请求在跑 54,178 / 97,429 / 65,473 tokens 的 prompt、
#: 思考预算 24,000，单个请求能占槽好几分钟。
#:
#: ⇒ 「超时」不是模型慢，是**共享单槽 Provider 的排队时间**。
#: 超时预算里**必须**有这一项，否则一个只要 0.5 秒算力的请求
#: 也会因为排在别人后面而被我们自己掐断。
#:
#: 取值依据：实测单次占槽最久见过 ~5 分钟（65K prompt + 24K 思考预算），
#: 这里给 **240 秒**，覆盖绝大多数排队情形；再大就会让「真挂了」迟迟不报。
QUEUE_WAIT_ALLOWANCE = 240.0


def compute_timeout(max_tokens: int) -> float:
    """
    按 max_tokens 估算本次调用的超时秒数。

    180 秒固定值对大预算不够用（实测 1646 tokens = 154.6 秒，
    32768 上限会直接超时）。这里按 token 数线性放宽，并封顶 MAX_TIMEOUT（600 秒）。

    【审查返修 · 关键】**不按用户配置的上限（ceiling）算超时**，
    只按**本次实际给模型的 max_tokens** 算。
    旧实现把「上限」当成「本次预算」，
    于是 ceiling=32768 时即使本次只给 800 tokens，超时也被放大到封顶 1800 秒，
    表现为「模型 3 秒就写完了，前端却挂满 30 分钟」。
    现在：本次给 800 -> 超时 216 秒；本次给 32768 -> 超时 600 秒（封顶）。
    """
    try:
        mt = int(max_tokens)
    except (TypeError, ValueError):
        mt = 1024
    est = BASE_TIMEOUT + max(0, mt) * PER_TOKEN_TIMEOUT
    # 【2026-11-16 修 c9】加上共享单槽 Provider 的排队余量。
    # 不加这一项：max_tokens=1024 时只有 242.88s，而实测**排队**就可能吃掉
    # 90s+（别人的大 prompt 占槽），于是我们会在模型还没轮到之前就超时。
    est += QUEUE_WAIT_ALLOWANCE
    return max(MIN_TIMEOUT, min(est, MAX_TIMEOUT))

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


def _extract_finish_reason(body: dict) -> str:
    """
    取后端给的 finish_reason。

    这是**判断输出是否被 max_tokens 硬切的权威信号**：
      "length" —— 生成因为达到 max_tokens 上限而停止（内容必然不完整）
      "stop"   —— 模型自己说完了（内容完整）
      其他后端可能不给这个字段，返回空串。

    之前 _extract_openai_text 把 finish_reason 直接丢掉，
    导致上层无法区分「模型就写这么多」和「被上限切断了」——
    这正是「二次增强丢背景」缺陷能悄无声息通过校验的原因之一。
    """
    choices = body.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("finish_reason") or "")


def _extract_ollama_text(body: dict) -> str:
    message = body.get("message") or {}
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ProviderError("模型返回了空内容")
    return text.strip()


def _extract_ollama_finish_reason(body: dict) -> str:
    """
    Ollama 的 done_reason（等价于 OpenAI 的 finish_reason）。

    Ollama 用 `done_reason` 字段，值为 "length" 时表示
    生成因为达到 num_predict（= max_tokens）上限而停止 —— 内容不完整。
    部分版本还会在 body 顶层给 `done` + `done_reason`。
    """
    reason = str(body.get("done_reason") or "")
    if not reason and body.get("done"):
        # done=True 但没给 done_reason —— 无法断定是长度截断，
        # 保守地不当作 length 处理（避免误报）。
        reason = "stop"
    return reason


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

    调用方若需要知道「输出是否被 max_tokens 硬切」，
    请改用 call_provider_full() —— 它额外返回 finish_reason。
    """
    res = await call_provider_full(provider, messages, images, options)
    return res["text"]


async def call_provider_full(provider: dict, messages: list, images: list | None = None,
                             options: dict | None = None) -> dict:
    """
    调用 Provider，返回 {"text", "finish_reason", "usage"}。

    与 call_provider 的区别：**不丢 finish_reason**。
    finish_reason == "length" 表示生成因为达到 max_tokens 上限而停止，
    内容必然不完整 —— 调用方据此可以拦截并报错，而不是把半截内容当成功。
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

    # 超时随 max_tokens 放大（大预算需要更长时间，见 compute_timeout）
    timeout = ClientTimeout(total=compute_timeout(max_tokens), connect=10)
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
                return {"text": _extract_ollama_text(body),
                        "finish_reason": _extract_ollama_finish_reason(body),
                        "usage": body.get("usage") or {}}

            if ptype == "comfyui":
                text = await _call_via_comfyui(session, provider, messages, images,
                                               temperature, top_p, max_tokens)
                # ComfyUI 节点链路拿不到后端的 finish_reason，
                # 结构完整性由上层的 check_truncation 兜底判断。
                return {"text": text, "finish_reason": "", "usage": {}}

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
            return {"text": _extract_openai_text(body),
                    "finish_reason": _extract_finish_reason(body),
                    "usage": body.get("usage") or {}}
        except ProviderError:
            raise
        except asyncio.TimeoutError:
            raise ProviderError(
                f"调用 Provider「{name}」超时（max_tokens={max_tokens}，"
                f"超时上限 {compute_timeout(max_tokens):.0f} 秒；"
                f"模型可能未加载、推理过慢，"
                f"或在**排队等待**前面的请求（共享单槽 Provider）")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Provider「{name}」返回的不是合法 JSON：{exc}")
        except Exception as exc:
            raise ProviderError(
                f"调用 Provider「{name}」失败：{type(exc).__name__} {exc}"[:300])


# ------------------------------------------------------------------ 工具调用（tools 协议）
#
# 为什么单独一个函数而不是给 call_provider 加参数：
#   call_provider 的契约是「发消息拿一段文本」，增强 / 分析链路全都依赖这个契约。
#   Agent 需要的是「模型可能返回 tool_calls 而不是文本」—— 两种返回结构完全不同，
#   混在一个函数里会让调用方到处 if/else 判断「这次是不是工具调用」。
#   所以这里新增一个平行的函数，原 call_provider **一行不动**。


def _extract_openai_tool_calls(body: dict) -> list[dict]:
    """
    从 OpenAI 兼容响应里取出 tool_calls。

    返回统一结构：[
        {"id": "call_x", "name": "search_assets", "arguments": {...}},
    ]
    arguments 解析失败时返回 {"_raw": "<原始字符串>"} —— 不静默丢弃，
    上层可以把它当工具错误回给模型，让模型自己重试。
    """
    choices = body.get("choices") or []
    if not choices:
        return []
    message = choices[0].get("message") or {}
    raw_calls = message.get("tool_calls") or []
    out = []
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = fn.get("name") or ""
        if not name:
            continue
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": str(raw_args or "")[:400]}
        if not isinstance(args, dict):
            args = {"_raw": str(args)[:400]}
        out.append({
            "id": item.get("id") or f"call_{len(out)}",
            "name": name,
            "arguments": args,
        })
    return out


async def call_provider_with_tools(provider: dict, messages: list,
                                   tools: list, options: dict | None = None,
                                   images: list | None = None) -> dict:
    """
    调用 Provider 并支持 OpenAI 原生 tools 协议。

    返回 {"content": str|None, "tool_calls": [...], "raw": dict}
      content     模型这轮的文本内容（可能为 None —— 纯工具调用轮就是这样）
      tool_calls  统一结构的工具调用列表（见 _extract_openai_tool_calls）
      raw         原始响应体（调试用）

    失败一律抛 ProviderError（带可读原因），与 call_provider 语义一致。
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
    if not tools:
        raise ProviderError("call_provider_with_tools 需要至少一个工具定义")
    if ptype == "comfyui":
        # ComfyUI 的 VRGDG_LocalLLM 节点自己拼 payload，只支持「一次问一次答」，
        # 没有 tools 协议。明确报错，不静默降级成纯文本调用
        # （降级的话模型永远看不到工具，Agent 会变成一问一答 —— 正是要解决的问题）。
        raise ProviderError(
            f"Provider「{name}」是 ComfyUI 本地节点类型，不支持工具调用协议。"
            "请在「本地模型配置」里改用 OpenAI 兼容 / Ollama / llama.cpp 类型的 Provider。")

    temperature = float(options.get("temperature", 0.2))
    max_tokens = int(options.get("max_tokens", 1024))
    top_p = float(options.get("top_p", 0.95))

    if images and not provider.get("multimodal"):
        raise ProviderError(
            f"Provider「{name}」未标记多模态能力，无法读图。")

    # 【2026-11-16 修 c9】超时**必须**按 max_tokens 推导，不能写死 180。
    # 写死 180 的后果：Agent 走的就是这条工具调用路径，
    # 于是 `compute_timeout(1024)=243s` 形同虚设，实际卡在 180s 上，
    # 而上游共享单槽排队实测就要 90s+ ⇒ 必然超时。
    # 调用方显式传了 timeout 就以调用方为准。
    _explicit_timeout = options.get("timeout")
    timeout_sec = (float(_explicit_timeout) if _explicit_timeout is not None
                   else compute_timeout(max_tokens))
    timeout = ClientTimeout(total=timeout_sec, connect=10)
    async with ClientSession(timeout=timeout) as session:
        try:
            if ptype == "ollama":
                payload = {
                    "model": model,
                    "messages": _messages_to_ollama(messages, images),
                    "tools": tools,
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
                message = body.get("message") or {}
                text = message.get("content")
                if not isinstance(text, str):
                    text = text or None
                # Ollama 原生协议的 tool_calls 在 message.tool_calls 里
                raw_calls = message.get("tool_calls") or []
                calls = []
                for index, item in enumerate(raw_calls):
                    fn = (item.get("function") or {}) if isinstance(item, dict) else {}
                    fn_name = fn.get("name") or ""
                    if not fn_name:
                        continue
                    raw_args = fn.get("arguments")
                    if isinstance(raw_args, dict):
                        args = raw_args
                    else:
                        try:
                            args = json.loads(raw_args) if raw_args else {}
                        except (json.JSONDecodeError, TypeError):
                            args = {"_raw": str(raw_args or "")[:400]}
                    calls.append({"id": f"call_{index}", "name": fn_name,
                                  "arguments": args if isinstance(args, dict)
                                  else {"_raw": str(args)[:400]}})
                if text is None and not calls:
                    raise ProviderError("模型返回了空内容")
                return {"content": (text or "").strip() or None,
                        "tool_calls": calls, "raw": body}

            # openai / llamacpp
            payload = {
                "model": model,
                "messages": _messages_to_openai(messages, images),
                "tools": tools,
                "temperature": temperature,
                "top_p": top_p,
                "max_tokens": max_tokens,
                "stream": False,
            }
            # 与 call_provider 保持一致：thinking 模型会污染 content，优先关掉。
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

            calls = _extract_openai_tool_calls(body)
            choices = body.get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            text = message.get("content")
            if text is None:
                text = message.get("reasoning_content")
            if not isinstance(text, str):
                text = None
            text = _strip_thinking(text).strip() if text else None
            if not text and not calls:
                raise ProviderError("模型返回了空内容")
            return {"content": text, "tool_calls": calls, "raw": body}
        except ProviderError:
            raise
        except asyncio.TimeoutError:
            raise ProviderError(
                f"调用 Provider「{name}」超时（{max_tokens} tokens 上限，"
                f"超时上限 {timeout_sec:.0f} 秒；"
                f"模型可能未加载、推理过慢，"
                f"或在**排队等待**前面的请求（共享单槽 Provider）")
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Provider「{name}」返回的不是合法 JSON：{exc}")
        except Exception as exc:
            raise ProviderError(
                f"调用 Provider「{name}」失败：{type(exc).__name__} {exc}"[:300])


# ------------------------------------------------------------------ 流式调用

async def call_provider_with_tools_stream(
        provider: dict, messages: list, tools: list,
        options: dict | None = None, images: list | None = None,
        on_token=None) -> dict:
    """
    带 **token 级增量** 的工具调用轮。

    与 call_provider_with_tools 的区别：**真的逐 token 推送**。
    旧实现是「整段返回」—— agent.py 只能在拿到完整 content 后
    发一次 `_event("token", {"text": content})`，前端只能整段重渲染，
    用户看不到「边生成边出字」。

    on_token: 可选回调 ``on_token(text: str)`` —— 每收到一个增量就调一次。
              回调异常会被吞掉（流式展示不该有能力把主流程带崩）。

    返回 {"content": str|None, "tool_calls": [...], "raw": dict}
      —— 语义与 call_provider_with_tools 完全一致（content 已 strip、
         tool_calls 已统一结构），所以调用方可以无缝切换。

    ⚠️ 协议差异：流式模式下**不能**同时用 tools（部分本地推理服务
    在 stream + tools 组合下会拒绝或丢掉 tool_calls）。所以这里的策略是：
      1. 带 tools 时**先试一次非流式** —— 拿到完整结果；
         若 content 很长（超过 on_token_min_chars），再**二次**用流式
         重放同样的请求来获得增量（此时不带 tools，避免协议冲突）。
      2. 不带 tools 时直接流式。
    这个「两次调用」的做法有代价（多一次推理），所以只在
    `options["stream_replay"]` 为真时启用（Agent 默认开启，
    因为它要的就是「边生成边出字」）。
    """
    options = options or {}
    replay = bool(options.get("stream_replay"))
    if not replay or on_token is None:
        return await call_provider_with_tools(provider, messages, tools,
                                              options=options, images=images)

    # ---- 第一步：非流式拿完整结果（含 tool_calls）----
    result = await call_provider_with_tools(provider, messages, tools,
                                            options=options, images=images)
    content = result.get("content") or ""
    min_chars = int(options.get("stream_min_chars", 12))
    if not content or len(content) < min_chars:
        return result

    # ---- 第二步：流式重放，只取增量文本 ----
    try:
        await _replay_stream_text(provider, messages, images, options, on_token)
    except Exception:
        # 流式重放失败**不影响**主流程 —— 已经有完整 content 了
        pass
    return result


async def _replay_stream_text(provider: dict, messages: list,
                              images: list | None, options: dict,
                              on_token) -> None:
    """用 OpenAI 兼容的 SSE 流式接口重放一次请求，逐段回调 on_token。

    只推送**文本增量**（不处理 tool_calls —— 工具调用已在非流式那轮拿到）。
    """
    base = (provider.get("base_url") or "").rstrip("/")
    model = provider.get("model") or ""
    temperature = float(options.get("temperature", 0.2))
    max_tokens = int(options.get("max_tokens", 1024))
    top_p = float(options.get("top_p", 0.95))

    payload = {
        "model": model,
        "messages": _messages_to_openai(messages, images),
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": True,
    }
    payload["chat_template_kwargs"] = {"enable_thinking": False}

    # 【2026-11-16 修 c9】同工具调用路径：按 max_tokens 推导，不写死 180。
    _explicit_timeout = options.get("timeout")
    timeout_sec = (float(_explicit_timeout) if _explicit_timeout is not None
                   else compute_timeout(max_tokens))
    timeout = ClientTimeout(total=timeout_sec, connect=10)
    async with ClientSession(timeout=timeout) as session:
        async with session.post(f"{base}/v1/chat/completions", json=payload,
                                 headers=_auth_headers(provider)) as resp:
            if resp.status != 200:
                raw = await resp.text()
                raise ProviderError(f"流式请求返回 HTTP {resp.status}：{raw[:200]}")
            emitted = 0
            async for raw_line in resp.content:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                piece = delta.get("content")
                if not isinstance(piece, str) or not piece:
                    continue
                emitted += 1
                try:
                    on_token(piece)
                except Exception:
                    pass


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
