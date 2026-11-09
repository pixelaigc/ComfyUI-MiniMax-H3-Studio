# -*- coding: utf-8 -*-
"""
H3 Studio 内置多模态 Agent —— 服务层。

【为什么需要它】
项目里已有的本地模型能力都是「一问一答」：
  * enhancer   —— 拿 prompt 扩写一次就返回
  * analysis   —— 拿素材描述一次就返回
用户诉求是「本地模型光一问一答无法正确处理内容」。
所以这里做的不是「再造一次模型调用」，而是加一层**编排**：
让模型能调工具（查素材 / 看模式 / 扩写 / 下发任务），多步地把一句自然语言
变成**可执行的生成任务**。

【设计原则 —— 不重复造轮子】
本模块**不自己**做下面任何一件事：
  * 不自己发 HTTP 调模型      -> 走 h3studio.llm.call_provider_with_tools
  * 不自己配 Provider         -> 走 h3studio.providers（复用「本地模型配置」页那一份）
  * 不自己拼 ComfyUI 工作流    -> 走 h3studio.workflows.BUILDERS（模式白名单即真源）
  * 不自己建任务              -> 走 server 侧与 /api/jobs 同一个建任务函数
  * 不自己写提示词模板        -> 走 h3studio.enhancer（与「增强」按钮同一套模板+校验）
  * 不自己查素材              -> 走 h3studio.asset_repo.search_assets
Agent 自己新增的只有：**工具注册表 + 编排器 + 结构化输出**。

【实测得到的关键约束（2026-11-09，本机 qwen3.8-flash-next-iq3_xxs）】
  1. 本地模型**支持** OpenAI 原生 tools 协议（tool_calls 正常返回）。
  2. **单步耗时 20~120 秒** —— 所以总超时给到 300 秒，步骤上限 6。
  3. **模型会原地打转**：检索结果不满足时会连续 6 步调同一个工具。
     -> 必须做「同名工具 + 相同参数」的重复检测，否则一次请求能烧掉十几分钟。
  4. thinking 模型会把推理混进 content -> 沿用 llm.py 的 enable_thinking=false。

【结构化输出契约】
返回给前端的固定结构见 `run_agent()` 的 docstring。要点：
  * jobs[] 里的 id **必须**是服务端真实返回的（模型编不出来）；
  * plan.mode **必须**在 workflows.BUILDERS 里（后端真实注册的模式）；
  * 拿不到的字段一律省略，不填占位假值。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

from . import llm as llm_client
from . import enhancer as enh
from . import gen_types
from . import providers as prov
from . import workflows as wf

# ------------------------------------------------------------------ 限额

#: 单次请求总超时（秒）。实测单步 60~120 秒（iq3 量化小模型 + 大工具 schema），
#: 4~5 步的正常流程就要 4~6 分钟。给到 420 秒是「够跑完正常流程」与
#: 「不让请求无限挂着」之间的平衡点。
TOTAL_TIMEOUT = 420.0
#: 单次模型调用超时（秒）。与 llm.CHAT_TIMEOUT 一致。
STEP_TIMEOUT = 180.0
#: 最大步骤数（= 模型轮数，含最后一轮「给结论」）。
#:
#: 为什么是 8 而不是 6：实测一条**成功**的完整链路要 6 轮
#:   search_assets ×2（第一次没命中，换关键词再搜）
#:   -> list_modes（查有哪些模式）
#:   -> enhance_prompt（扩写提示词）
#:   -> create_job（下发任务）
#:   -> respond（交结论）
#: 上限卡在 6 的话，这条链路**刚好**用完，一点余量都没有 ——
#: 模型只要多搜一次就拿不到结论，用户看到的是「已截断」而不是一个完整的回答。
#: 真正兜底的是 TOTAL_TIMEOUT（420 秒），所以步数上限给到 8 不会让请求无限跑。
MAX_STEPS = 8
#: 最大工具调用数。
MAX_TOOL_CALLS = 8
#: 同时运行的 Agent 会话数上限。本地模型单卡，并发只会互相拖慢。
MAX_CONCURRENT = 2
#: 单个工具结果回给模型时的最大字符数（防止素材检索的大 JSON 撑爆上下文）。
#:
#: 为什么是 1200 而不是 600：实测 list_modes 的完整结果约 1250 字符，
#: 600 会把它截断成「详情被截断」—— 模型据此以为工具坏了，
#: 然后重复调同一个工具（实测打转 3 次被重复检测收尾）。
#: 1200 能把 list_modes 完整传过去；素材检索那种大结果仍会被裁。
TOOL_RESULT_LIMIT = 1200
#: 同名工具 + 相同参数连续出现几次判定为「打转」。
#
# 为什么是 3 而不是 2：本地小模型在「检索结果为空」时经常原样重搜一次
# （这是合理的重试，不是打转）。第 2 次时先**给一次提示**（见 _loop 里的 nudge），
# 让模型自己换关键词或直接汇报；第 3 次仍完全相同才判定打转并收尾。
REPEAT_LIMIT = 3


class AgentError(RuntimeError):
    """Agent 错误。message 面向用户，直接可读。"""

    def __init__(self, message, status=400, code="agent_error", extra=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


# ------------------------------------------------------------------ 系统提示词

SYSTEM_PROMPT = """你是 OmniStudio（ComfyUI-MiniMax-H3-Studio）内置的多模态创作 Agent。
你的职责是处理**图像 / 视频 / 音频**相关的创作任务，把用户的自然语言意图
转成**可执行的生成任务**。

## 你能用的工具
- search_assets(query, category?)  在素材库里检索素材。category 取 image/video/audio
- list_modes()                  列出本工作台真实支持的生成模式
- describe_asset(asset_id)       查看某个素材的详情与已有分析结论
- enhance_prompt(prompt, mode?)  用工作台的提示词增强模板扩写提示词
- create_job(mode, prompt, ...)  创建一个生成任务（真正下发到 ComfyUI 队列）
- respond(summary, ...)          提交最终结论

## 工作方式
1. 需要素材时先 search_assets；**同一工具不要连续重复调用超过 2 次**，
   没找到就换关键词或直接告诉用户没有。
2. 需要下发生成时才调 create_job。参数只填用户表达出来的意思，
   没说的（分辨率、比例、时长、种子）**不要瞎编**，留空由工作台用官方默认值。
3. 任务完成后必须调 respond，把结论和 job_id 交给用户。
4. 如果用户的需求超出工具能力（例如要分析视频内容、要看生成结果），
   直接在 respond 里说明边界，不要假装做到了。

## 输出要求
- 全中文。
- **绝对不要用文字描述你将要做什么**（例如「我先增强提示词，再下发生成任务」）。
  这类话对用户毫无意义 —— 用户要的是**已经下发好的任务**。
  想做一件事，就**直接调用对应的工具**。
- 除了最后的 respond 结论，其余轮次一律只输出工具调用。
- 结论要具体：说清楚做了什么、生成了什么、用了哪些素材。
- 如果某件事做不了（比如参数超出范围），直接用 respond 说明原因和可选方案，
  不要在正文里反复解释。
"""


# ------------------------------------------------------------------ 工具注册表
#
# 每个工具 = {"name", "description", "parameters"(JSON Schema), "handler"}
# handler 签名：async (ctx, args) -> dict
#   ctx 是 AgentContext（带 db 连接、job 创建回调、已用工具计数等）
#   返回值会被序列化后作为 tool 消息回给模型


def _schema(properties: dict, required: list | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or []}


TOOLS: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict):
    """注册一个工具的装饰器。"""
    def wrap(fn):
        TOOLS[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "handler": fn,
        }
        return fn
    return wrap


# ------------------------------------------------------------------ 上下文


class AgentContext:
    """
    一次 Agent 运行的可变状态。

    刻意做成显式对象而不是到处传参 —— 编排器要跨步骤共享这些东西，
    而函数签名塞 6 个参数会让「加一个字段」变成改一串调用点。
    """

    def __init__(self, server_module=None, provider: dict | None = None):
        self.server = server_module
        self.provider = provider or {}
        #: 本次运行创建的任务（job_id -> 结构化条目）
        self.jobs: list[dict] = []
        #: 命中的素材（asset_id -> 精简条目）
        self.assets: dict[str, dict] = {}
        #: 增强产物
        self.enhanced: dict | None = None
        #: 已调用的工具次数
        self.tool_calls = 0
        #: 模型给的最终结论
        self.answer: str = ""
        #: 模型调用 respond 时带的额外信息
        self.respond_payload: dict = {}
        #: 步骤轨迹
        self.steps: list[dict] = []

    def record(self, step: dict) -> None:
        self.steps.append(step)


# ------------------------------------------------------------------ 工具实现


@tool(
    "search_assets",
    "在素材库里检索素材。可以按关键词搜索，也可以按类型过滤（image/video/audio）。",
    _schema({
        "query": {"type": "string", "description": "搜索关键词，如「雨夜」「雷神」"},
        "category": {"type": "string",
                     "enum": ["image", "video", "audio"],
                     "description": "素材类型，不填则不限"},
        "limit": {"type": "integer", "description": "返回条数上限，默认 8"},
    }, ["query"]),
)
async def _tool_search_assets(ctx: AgentContext, args: dict) -> dict:
    from . import asset_repo
    from . import assets as asset_store

    query = str(args.get("query") or "").strip()
    if not query:
        raise AgentError("search_assets 需要 query 参数")
    category = str(args.get("category") or "").strip()
    if category and category not in asset_store.CATEGORIES:
        raise AgentError(
            f"category 只能是 {'/'.join(asset_store.CATEGORIES)}，收到 {category}")
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(20, limit))

    filters = {"q": query, "category": category, "sort": "created_desc",
               "page": 1, "page_size": limit}
    with asset_store.connect() as conn:
        result = asset_repo.search_assets(conn, filters)

    items = []
    for asset in result.get("assets", []):
        entry = {
            "asset_id": asset["id"],
            "name": asset.get("name") or "",
            "category": asset.get("category") or "",
            "tags": asset.get("tags") or [],
            # 分析摘要是素材库里**已有的**模型分析结论，不是 Agent 现生成的
            "description": (asset.get("analysis_summary") or "").strip(),
            "analyzed": bool(asset.get("analyzed")),
        }
        items.append(entry)
        ctx.assets[entry["asset_id"]] = entry

    total = result.get("total", len(items))
    if not items:
        # 空结果必须**明确告诉模型「没找到」**，并给出下一步该怎么做 ——
        # 否则本地小模型会以为工具坏了，原样再搜一次（实测会原地打转）。
        return {
            "total": 0,
            "assets": [],
            "hint": (f"素材库里没有匹配「{query}」的素材。"
                     "请换一个关键词再搜一次；如果换了关键词仍然没有，"
                     "就直接用 respond 告诉用户没有找到合适的素材，"
                     "不要重复调用本工具。"),
        }
    return {"total": total, "assets": items,
            "hint": "如需查看某个素材的详情，用 describe_asset 传入它的 asset_id。"}


@tool(
    "list_modes",
    "列出本工作台真实支持的生成模式，以及每个模式的可用参数范围。",
    _schema({}),
)
async def _tool_list_modes(ctx: AgentContext, args: dict) -> dict:
    """
    列出 Agent **能实际下发**的生成模式。

    注意这里刻意不用 gen_types.registered_modes()（它含 longfilm）——
    longfilm 走的是自己的子进程链路，不在 workflows.BUILDERS 里，
    create_job 建不了。报出来就是「看得到、用不了」，模型会因此卡住。
    模式白名单 = create_job 的白名单 = workflows.BUILDERS。
    """
    modes = []
    for mode in wf.BUILDERS.keys():
        info = gen_types.type_for_mode(mode)
        entry = {
            "mode": mode,
            "label": info.get("label") or mode,
            "media": info.get("media") or "other",
        }
        if mode in ("t2i", "t2i_qwen", "t2i_moss"):
            entry["aspects"] = list(wf.ASPECT_MAP.keys())
        if mode in ("t2v", "flf", "r2v"):
            entry["aspects"] = list(wf.ASPECT_MAP.keys())
            # 时长是**任意秒数**（后端 workflows 直接 float(params["duration"])），
            # 面板滑块给的是 5~15 秒。之前这里硬编码成 [5,10,15]，
            # 模型据此以为「8 秒不支持」而拒绝执行 —— 这是凭空造的约束。
            entry["duration_range"] = [5, 15]
            entry["duration_note"] = "单位：秒，取 5~15 之间的整数，面板滑块范围"
        if mode == "t2i":
            entry["models"] = list(wf.Z_IMAGE_MODELS.keys())
            entry["resolutions"] = list(wf.Z_IMAGE_RES_TIERS.keys())
        if mode == "t2i_qwen":
            entry["models"] = list(wf.QWEN_IMAGE_MODELS.keys())
            entry["resolutions"] = list(wf.QWEN_IMAGE_RES_TIERS.keys())
        if mode == "t2i_moss":
            entry["models"] = list(wf.MOSS_IMAGE_MODELS.keys())
            entry["resolutions"] = list(wf.MOSS_IMAGE_RES_TIERS.keys())
        if mode == "i2i_qwen":
            entry["models"] = list(wf.QWEN_IMAGE_MODELS.keys())
            entry["requires"] = "参考图必须来自素材库（用 asset_id 指定）"
        if mode in ("t2a", "a2a"):
            # 音乐时长由 music_yue2._clamp_duration 收敛到 [4, 360] 秒
            entry["duration_range"] = [4, 360]
            entry["duration_note"] = "单位：秒，范围 4~360"
        modes.append(entry)

    # 压缩：把重复的「比例列表」「时长区间」提到顶层，模式条目只留差异。
    #
    # 为什么：工具结果会被截到 TOOL_RESULT_LIMIT（600 字符）。
    # 之前每个模式都带一份完整的 8 项比例列表 + 时长说明，
    # 9 个模式加起来超过 1200 字符 -> 被截断 -> 模型看到的���
    # 「详情被截断，未逐条列出参数」-> 然后它就去重复调 list_modes（实测打转 3 次）。
    # 压缩后同一份信息只要 ~400 字符，能完整传过去。
    shared_aspects = list(wf.ASPECT_MAP.keys())
    compact = []
    for entry in modes:
        item = {"mode": entry["mode"], "label": entry["label"],
                "media": entry["media"]}
        # 只给「可选项的数量 + 默认值」，不给完整 key 列表 ——
        # 完整列表太长（模型名 + 档位名加起来能到 700+ 字符），
        # 会把工具结果顶到截断线以上，模型看到的就是「详情被截断」。
        if entry.get("models"):
            item["model_count"] = len(entry["models"])
            item["default_model"] = entry["models"][0]
        if entry.get("resolutions"):
            item["resolution_count"] = len(entry["resolutions"])
            item["default_resolution"] = entry["resolutions"][0]
        if entry.get("requires"):
            item["requires"] = entry["requires"]
        compact.append(item)
    return {
        "modes": compact,
        # 共用参数（原来每个模式重复一份，撑爆了工具结果长度上限）
        "aspects": shared_aspects,
        "video_duration_range": [5, 15],
        "music_duration_range": [4, 360],
        "notes": ("比例所有模式通用；视频 5~15 秒、音乐 4~360 秒，均为任意秒数；"
                  "model/resolution 留空即用默认值，其它未指定参数由工作台补齐官方默认值"),
    }


@tool(
    "describe_asset",
    "查看素材库里某个素材的详情：类型、标签、以及已有的模型分析结论。",
    _schema({"asset_id": {"type": "string", "description": "素材 ID"}}, ["asset_id"]),
)
async def _tool_describe_asset(ctx: AgentContext, args: dict) -> dict:
    from . import asset_repo
    from . import assets as asset_store

    asset_id = str(args.get("asset_id") or "").strip()
    if not asset_id:
        raise AgentError("describe_asset 需要 asset_id 参数")
    with asset_store.connect() as conn:
        asset = asset_repo.get_asset(conn, asset_id, include_deleted=True)
        if not asset:
            raise AgentError(f"素材不存在：{asset_id}", status=404)
        if asset.get("deleted_at"):
            raise AgentError(f"素材已在素材库回收站中：{asset.get('name') or asset_id}")
        version = asset_repo.current_version(conn, asset_id)
        analysis = asset_repo.latest_analysis(conn, asset_id)
        tags = asset_repo.tags_of(conn, asset_id)
    entry = {
        "asset_id": asset_id,
        "name": asset.get("name") or "",
        "category": asset.get("category") or "",
        "subtype": asset.get("subtype") or "",
        "tags": tags,
        "width": (version or {}).get("width"),
        "height": (version or {}).get("height"),
        "duration_sec": (version or {}).get("duration_sec"),
        "description": (analysis or {}).get("text") or "",
        "analyzed": bool(analysis),
    }
    ctx.assets[asset_id] = entry
    return entry


@tool(
    "enhance_prompt",
    "用工作台内置的提示词增强模板扩写提示词（带硬约束校验，不会产出违规内容）。",
    _schema({
        "prompt": {"type": "string", "description": "原始提示词"},
        "mode": {"type": "string",
                 "description": "目标生成模式，用于选合适的增强模板"},
    }, ["prompt"]),
)
async def _tool_enhance_prompt(ctx: AgentContext, args: dict) -> dict:
    original = str(args.get("prompt") or "").strip()
    if not original:
        raise AgentError("enhance_prompt 需要 prompt 参数")
    if len(original) > 4000:
        raise AgentError("提示词过长（上限 4000 字）")

    mode = str(args.get("mode") or "").strip()
    spec_id = enh.default_spec_for_mode(mode) if mode else None
    if not spec_id:
        spec_id = enh.default_spec_for_mode("t2i") or "image"

    # 与 /api/enhance 同一套模板与参数来源（用户自定义覆盖也生效）
    saved = _load_enhancer_overrides(ctx)
    spec = enh.get_spec(spec_id, saved)
    params = enh.resolve_params(None, saved, spec)
    messages = enh.build_messages(spec, original, params.get("language", "auto"))

    provider = ctx.provider
    if not provider:
        raise AgentError("没有可用的本地模型 Provider", status=400,
                         code="no_provider")
    try:
        raw = await llm_client.call_provider(
            provider, messages,
            options={"temperature": params["temperature"],
                     "top_p": params["top_p"],
                     "max_tokens": params["max_tokens"],
                     "do_sample": params.get("do_sample", True),
                     "timeout": STEP_TIMEOUT})
    except llm_client.ProviderError as exc:
        raise AgentError(f"提示词增强失败：{exc}", status=502,
                         code="provider_error") from exc

    raw = enh.strip_thinking(raw)
    enhanced = enh.strip_wrapping(raw)
    if not enhanced:
        raise AgentError("模型返回了空内容，未做修改", status=502,
                         code="provider_error")
    if enh.looks_like_echo(enhanced, spec["system_prompt"]):
        raise AgentError(
            f"Provider「{provider.get('name')}」返回的是系统提示词复读，"
            "不是有效的提示词。该模型可能太小或未按指令输出，请换用更强的模型。",
            status=502, code="provider_error")

    validation = enh.validate_output(enhanced, original,
                                     spec.get("validate_mode", "pe"))
    if not validation.get("ok", True):
        raise AgentError(
            "扩写结果违反工作台的提示词硬约束："
            + "；".join(validation.get("errors", [])),
            status=502, code="provider_error")

    ctx.enhanced = {"original": original, "enhanced": enhanced,
                    "spec_id": spec_id, "validation": validation}
    return {"original": original, "enhanced": enhanced,
            "spec_id": spec_id, "validation_ok": validation.get("ok", True)}


@tool(
    "create_job",
    "创建一个生成任务并放进 ComfyUI 队列。任务会出现在工作台的「生成」列表里。",
    _schema({
        "mode": {"type": "string",
                 "description": "生成模式，必须是 list_modes 里列出的那些",
                 "enum": sorted(wf.BUILDERS.keys())},
        "prompt": {"type": "string", "description": "生成提示词"},
        "asset_id": {"type": "string",
                     "description": "参考素材 ID（来自 search_assets）。"
                                    "视频模式可传多次作为多张参考图"},
        "aspect": {"type": "string", "description": "画面比例，如 16:9 / 9:16 / 1:1"},
        "duration": {"type": "number", "description": "时长（秒），视频/音乐模式"},
        "resolution": {"type": "string", "description": "分辨率档位，如 1mp / 720p"},
        "batch_count": {"type": "integer", "description": "一次生成几张（图像模式）"},
        "summary": {"type": "string", "description": "一句话说明这个任务要做什么"},
    }, ["mode", "prompt"]),
)
async def _tool_create_job(ctx: AgentContext, args: dict) -> dict:
    mode = str(args.get("mode") or "").strip()
    if mode not in wf.BUILDERS:
        raise AgentError(
            f"未知模式 {mode}。本工作台支持：{'、'.join(sorted(wf.BUILDERS.keys()))}")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        # 文案与面板提交路径一致（server.prepare_job_params 的「提示词不能为空」）
        raise AgentError("提示词不能为空")

    params: dict = {"prompt": prompt}
    aspect = str(args.get("aspect") or "").strip()
    if aspect:
        if aspect not in wf.ASPECT_MAP:
            raise AgentError(
                f"未知画面比例 {aspect}。可用：{'、'.join(wf.ASPECT_MAP.keys())}")
        params["aspect"] = aspect
    resolution = str(args.get("resolution") or "").strip()
    if resolution:
        params["resolution"] = resolution
    if args.get("duration") is not None:
        try:
            params["duration"] = float(args["duration"])
        except (TypeError, ValueError):
            raise AgentError("duration 必须是数字")
    if args.get("batch_count") is not None:
        try:
            params["batch_count"] = int(args["batch_count"])
        except (TypeError, ValueError):
            raise AgentError("batch_count 必须是整数")

    # 参考素材：asset_id 列表 -> 素材库引用（与 /api/jobs 的 asset_refs 同一格式）
    raw_assets = args.get("asset_ids")
    if raw_assets is None:
        single = str(args.get("asset_id") or "").strip()
        raw_assets = [single] if single else []
    if isinstance(raw_assets, str):
        raw_assets = [raw_assets]
    asset_refs = []
    for index, item in enumerate(raw_assets):
        asset_id = str(item or "").strip()
        if not asset_id:
            continue
        asset_refs.append({"asset_id": asset_id, "slot": "ref_image",
                           "ordinal": index})

    # 走服务端**同一套**校验（与面板点「生成」完全一致）：
    # 模式白名单 / 参考图数量 / <imageN> 占位注入 / 参考音频数量 / 提示词非空。
    # 这里不重写一份 —— 两份实现必然漂移（见 server.prepare_job_params 的说明）。
    prepare = getattr(ctx.server, "prepare_job_params", None)
    if prepare is None:
        raise AgentError("服务端缺少 prepare_job_params，无法创建任务", status=500)

    # 图片编辑（i2i_qwen）的参考图走**另一条**参数通道：
    # 它要的是 params["ref_images"]（带描述/标签的参考图元数据），
    # 而其它模式走 params["asset_refs"]（只有 asset_id + 槽位）。
    # 这里用素材库自己���解析函数（ref_api._ref_entry）把 asset_id 变成元数据 ——
    # 描述取自素材库已有的分析结果，与面板选参考图时看到的完全一致。
    if mode == "i2i_qwen":
        ref_specs = await _resolve_ref_specs(ctx, asset_refs)
        if not ref_specs:
            raise AgentError(
                "图片编辑必须指定参考图（用 asset_id，来自 search_assets 的结果）。"
                "参考图只能来自素材库。", status=400, code="ref_required")
        params["ref_images"] = ref_specs

    try:
        asset_refs = prepare(mode, params, asset_refs, None)
    except Exception as exc:
        # 服务端的校验异常带 message / code —— 转成 Agent 的可读错误。
        # 这里不 import server（会循环依赖），用鸭子类型读属性。
        message = getattr(exc, "message", None) or str(exc)
        code = getattr(exc, "code", "bad_request")
        raise AgentError(message, status=400, code=code) from exc

    # 素材库引用写进 params —— worker 跑的时候据此把素材上传到 ComfyUI。
    # 这一步不能省：少了它任务照样入队，但参考图是空的（静默失效，最难查）。
    if asset_refs:
        params["asset_refs"] = asset_refs

    job_id = _create_job_record(ctx, mode, params)
    entry = {
        "job_id": job_id,
        "mode": mode,
        "label": gen_types.type_for_mode(mode).get("label") or mode,
        "prompt": prompt,
        "params": {k: v for k, v in params.items() if k != "asset_refs"},
        "asset_refs": asset_refs,
        "status": "queued",
        "summary": str(args.get("summary") or "").strip(),
    }
    ctx.jobs.append(entry)
    return {"job_id": job_id, "mode": mode, "status": "queued",
            "label": entry["label"]}


@tool(
    "respond",
    "提交最终结论给用户。任务完成时必须调用这个工具。",
    _schema({
        "summary": {"type": "string", "description": "给用户看的中文结论"},
        "job_ids": {"type": "array", "items": {"type": "string"},
                    "description": "本次创建的任务 ID 列表"},
        "asset_ids": {"type": "array", "items": {"type": "string"},
                      "description": "本次用到的素材 ID 列表"},
        "needs_clarification": {"type": "boolean",
                                "description": "用户的需求不明确时置 true"},
    }, ["summary"]),
)
async def _tool_respond(ctx: AgentContext, args: dict) -> dict:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise AgentError("respond 需要 summary 参数")
    ctx.answer = summary
    ctx.respond_payload = {
        "job_ids": [str(x) for x in (args.get("job_ids") or [])],
        "asset_ids": [str(x) for x in (args.get("asset_ids") or [])],
        "needs_clarification": bool(args.get("needs_clarification")),
    }
    return {"ok": True}


# ------------------------------------------------------------------ 辅助


async def _resolve_ref_specs(ctx: AgentContext, asset_refs: list) -> list[dict]:
    """
    把 asset_id 列表解析成图片编辑（i2i_qwen）要的参考图元数据。

    复用 h3studio.ref_api._ref_entry —— 与面板「从素材库添加参考图」
    走的是同一个解析函数，所以描述/标签/失效标记的语义完全一致
    （描述来自素材库已有的模型分析结果，不是 Agent 现编的）。
    """
    from . import assets as asset_store
    from . import ref_api

    specs = []
    with asset_store.connect() as conn:
        for item in asset_refs:
            asset_id = str(item.get("asset_id") or "").strip()
            if not asset_id:
                continue
            specs.append(ref_api._ref_entry(conn, asset_id))
    return specs


def _load_enhancer_overrides(ctx: AgentContext) -> dict:
    """读「本地模型配置」页里保存的增强模板覆盖（与 /api/enhance 同一份）。"""
    server = ctx.server
    if server is None:
        return {}
    try:
        with server.db() as conn:
            row = conn.execute(
                "SELECT value FROM model_provider_prefs WHERE key=?",
                ("enhancer",)).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        return json.loads(row["value"] or "{}")
    except Exception:
        return {}


def _create_job_record(ctx: AgentContext, mode: str, params: dict) -> str:
    """
    建一条生成任务记录。

    刻意**复用** server 侧的建任务逻辑（server.create_job_record），
    而不是在这里另写一份 SQL —— 两份实现必然会漂移（字段默认值、批次拆分、
    gen_type 写入都会不一致）。
    """
    server = ctx.server
    if server is None:
        raise AgentError("Agent 未接入服务端环境，无法创建任务", status=500)
    creator = getattr(server, "create_job_record", None)
    if creator is None:
        raise AgentError("服务端缺少 create_job_record，无法创建任务", status=500)
    try:
        return creator(mode, params)
    except AgentError:
        raise
    except ValueError as exc:
        raise AgentError(str(exc), status=400) from exc


# ------------------------------------------------------------------ 编排器


def _tool_specs() -> list[dict]:
    """给模型的工具定义（OpenAI tools 协议格式）。"""
    return [{
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    } for spec in TOOLS.values()]


#: 判定「模型只是在叙述接下来要做什么」的关键词。
#
# 本地小模型常见坏习惯：用自然语言说「我先增强提示词，再下发任务」然后就停了，
# 看上去像完成了，实际一个工具都没调 —— 用户收到的是一句空头承诺。
# 判据要**窄**：只匹配明确的未来时叙述，且不匹配真正的结论。
_NARRATION_MARKERS = (
    "我先", "接下来", "然后我", "我现在就", "我这就去", "我会先", "我将",
    "再下发", "再去", "下一步我", "接下来我", "然后再", "接着我", "我现在先",
)


def _looks_like_intent_narration(text: str) -> bool:
    """
    这段文本是不是「我接下来要做什么」的叙述，而不是一个真正的结论？

    刻意保守：宁可漏判（让模型正常收尾），也不误判（把真结论踢回去再问一轮，
    白花 60~120 秒）。
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 400:
        return False
    if any(marker in stripped for marker in _NARRATION_MARKERS):
        return True
    return False


def _clip(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    """
    把工具结果裁到 limit 字符以内。

    裁剪保留**头 + 尾**（不是只留头）：JSON 的结尾（闭合括号、notes 段）
    对模型理解结构很有用；只留头会让模型看到一段残缺 JSON。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return (text[:head] + "…（结果已截断）…" + text[-tail:])


def _resolve_provider(provider_id: str | None, server_module) -> dict:
    """
    选 Provider：显式指定 > 默认多模态 > 默认。

    与 /api/enhance 的选择逻辑一致 —— 用户不需要为 Agent 单独配一份模型。
    """
    with server_module.db() as conn:
        if provider_id:
            item = prov.get_provider_full(conn, provider_id)
            if not item:
                raise AgentError(f"指定的 Provider 不存在：{provider_id}", status=404)
            if not item["enabled"]:
                raise AgentError(f"Provider「{item['name']}」已停用", status=400)
            return item
        item = prov.resolve_default(conn, "default")
        if not item:
            raise AgentError(
                "尚未配置可用的本地模型 Provider。"
                "请先在「本地模型配置」页添加并启用一个 Provider。",
                status=400, code="no_provider")
        return item


async def run_agent(server_module, message: str, session_id: str | None = None,
                    provider_id: str | None = None,
                    max_steps: int = MAX_STEPS,
                    history: list | None = None,
                    session_store=None) -> dict:
    """
    跑一次 Agent。返回结构化结果。

    返回结构（这是对外契约，前端按这个渲染）：
    {
      "session_id": str,
      "status": "completed" | "needs_clarification" | "truncated" | "failed",
      "answer": str,                 # 给用户看的中文结论
      "jobs": [...],                 # 已创建的任务（job_id 由服务端生成，模型编不出来）
      "assets": [...],               # 命中的素材
      "prompt": {"original","enhanced"} | None,   # 增强产物
      "steps": [{"step","tool","ok","ms"}],
      "usage": {"steps","tool_calls","elapsed_ms"},
      "provider": {"name","model"},  # 实际用的模型
      "error": str | None,           # 失败/截断原因（可读）
    }
    """
    message = (message or "").strip()
    if not message:
        raise AgentError("请输入要处理的内容", status=400, code="bad_request")
    if len(message) > 4000:
        raise AgentError("请求内容过长（上限 4000 字）", status=400,
                         code="bad_request")

    steps_limit = max(1, min(int(max_steps or MAX_STEPS), MAX_STEPS))
    provider = _resolve_provider(provider_id, server_module)
    ctx = AgentContext(server_module=server_module, provider=provider)

    # 构造消息：历史 + 本轮用户输入
    messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
    for item in (history or []):
        role = item.get("role")
        content = item.get("content") or ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    tools = _tool_specs()
    started = time.time()
    steps: list[dict] = []
    tool_calls = 0
    status = "completed"
    error = None
    repeat_signature = None
    repeat_count = 0
    truncated_reason = None

    async def _one_turn():
        """一轮模型调用（含 tools）。"""
        return await llm_client.call_provider_with_tools(
            provider, messages, tools,
            options={"temperature": 0.2, "top_p": 0.95, "max_tokens": 1024,
                     "timeout": STEP_TIMEOUT})

    async def _loop():
        """主循环。返回 (status, truncated_reason)。"""
        nonlocal tool_calls, repeat_signature, repeat_count
        for step_index in range(1, steps_limit + 1):
            turn_started = time.time()
            response = await _one_turn()
            calls = response.get("tool_calls") or []
            content = response.get("content")

            if not calls:
                # 模型没再调工具 —— 正常情况下这就是它的最终结论（respond）。
                #
                # 但实测本地小模型有个坏习惯：**用自然语言叙述它「将要」做什么**
                # 而不真的调工具（实测原话：「我先增强提示词，再下发生成任务。」），
                # 然后就停了 —— 用户看到的是一句承诺，不是一个任务。
                #
                # 判据：文本里出现「将要/接下来/我先/然后/再…」这类**未来时叙述**，
                # 且本轮之前**已经用过工具**（说明它在做一件多步的事，只是没调完）。
                # 这时候推它一把（把这句话当成 tool 消息回给它），让它继续调工具。
                # 判据要看**本轮之前是否用过工具**（steps 里已有 tool 条目）
                # —— 纯闲聊场景不该被推去调工具。
                used_tools = [s.get("tool") for s in steps if s.get("tool")]
                if content and _looks_like_intent_narration(content) and used_tools:
                    nudge = {
                        "warning": ("你刚才只是用文字描述了接下来要做什么，"
                                    "但没有真正调用工具。请**直接调用工具**继续："
                                    "需要素材用 search_assets，需要扩写用 enhance_prompt，"
                                    "需要下发任务用 create_job，最后用 respond 交结论。"),
                        "current_progress": {
                            "tools_used": used_tools,
                            "jobs_created": len(ctx.jobs),
                        },
                    }
                    steps.append({
                        "step": step_index, "tool": None, "ok": True,
                        "ms": int((time.time() - turn_started) * 1000),
                        "note": "模型只做了口头承诺，已推它继续调工具",
                    })
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": f"nudge_{step_index}",
                        "content": _clip(json.dumps(nudge, ensure_ascii=False)),
                    })
                    continue
                if content:
                    ctx.answer = content
                steps.append({"step": step_index, "tool": None, "ok": True,
                              "ms": int((time.time() - turn_started) * 1000),
                              "note": "模型给出结论"})
                return "completed", None

            # 有工具调用 —— assistant 消息必须原样带回，否则模型失去上下文
            messages.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [{
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": call["name"],
                                 "arguments": json.dumps(call["arguments"],
                                                         ensure_ascii=False)},
                } for call in calls],
            })

            stop_after_step = False
            for call in calls:
                if tool_calls >= MAX_TOOL_CALLS:
                    return "truncated", (
                        f"已达到工具调用上限（{MAX_TOOL_CALLS} 次），"
                        "返回已完成的部分结果")
                tool_calls += 1
                name = call["name"]
                spec = TOOLS.get(name)
                signature = (name, json.dumps(call["arguments"], sort_keys=True,
                                              ensure_ascii=False))
                if signature == repeat_signature:
                    repeat_count += 1
                else:
                    repeat_signature = signature
                    repeat_count = 1

                if spec is None:
                    result = {"error": f"未知工具：{name}",
                              "hint": "可用工具：" + "、".join(TOOLS.keys())}
                    ok = False
                else:
                    try:
                        result = await spec["handler"](ctx, call["arguments"])
                        ok = True
                    except AgentError as exc:
                        result = {"error": exc.message}
                        ok = False
                    except Exception as exc:  # 工具内部意外错误
                        result = {"error": f"{type(exc).__name__}: {exc}"[:300]}
                        ok = False

                steps.append({
                    "step": step_index, "tool": name, "ok": ok,
                    "ms": int((time.time() - turn_started) * 1000),
                    "args": _clip(json.dumps(call["arguments"], ensure_ascii=False), 200),
                    "error": (result.get("error") if isinstance(result, dict)
                              else None),
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": _clip(json.dumps(result, ensure_ascii=False)),
                })

                # respond 之后模型已经交卷 —— 再问一轮要付 20~120 秒，
                # 而拿到的多半还是同一句话。直接收尾。
                if name == "respond":
                    stop_after_step = True
                    break

                # 重复调用检测：模型原地打转时强制收尾（实测会连续 6 步调同一个工具）。
                #
                # 第 2 次完全相同的调用 -> 先**给一次提示**（nudge）让模型自己纠正：
                # 换关键词 / 换工具 / 直接汇报。第 3 次仍完全相同才判定打转并收尾。
                #
                # 注意：nudge 必须**并进同一个 tool 消息**里，不能再追加一条
                # tool 消息 —— 一个 tool_call_id 对应两条 tool 消息是非法消息序列，
                # 有些推理后端会直接报错。
                if repeat_count >= REPEAT_LIMIT:
                    return "truncated", (
                        f"模型重复调用同一工具「{name}」{repeat_count} 次且参数完全相同，"
                        "已停止以避免空转。请把话说得更具体，或直接用面板手动下发任务。")
                if repeat_count == REPEAT_LIMIT - 1:
                    nudge = {
                        "warning": (f"你刚刚用完全相同的参数调用了「{name}」，"
                                    "请不要再重复同样的调用。"),
                        "next_steps": [
                            "如果是 search_assets：换一个关键词，或去掉 category 过滤再试一次",
                            "如果是 list_modes：直接按返回的模式继续下一步",
                            "如果确实没有可用素材：用 respond 告诉用户「没有找到」并给出建议",
                            "无论哪种情况，最后都必须调用 respond 给出结论",
                        ],
                    }
                    if isinstance(result, dict):
                        result = dict(result)
                        result["_nudge"] = nudge
                    else:
                        result = {"result": result, "_nudge": nudge}
                    # 把 nudge 合并进已发出的那条 tool 消息（不新增消息）
                    for msg in reversed(messages):
                        if (msg.get("role") == "tool"
                                and msg.get("tool_call_id") == call["id"]):
                            msg["content"] = _clip(json.dumps(result, ensure_ascii=False))
                            break
                    steps.append({
                        "step": step_index, "tool": name, "ok": ok,
                        "ms": int((time.time() - turn_started) * 1000),
                        "args": _clip(json.dumps(call["arguments"], ensure_ascii=False), 200),
                        "error": None,
                        "note": "重复调用，已给出纠偏提示",
                    })
                    # 注意：这里**不** break，也**不**清零计数 ——
                    # 让模型再走一轮，看它是否接受提示。
                    # 这一轮如果它又用同样的参数，repeat_count 变成 3，
                    # 命中上面的 repeat_count >= REPEAT_LIMIT 而收尾。
                    # （如果它换了参数，signature 变了，repeat_count 自然归 1。）
                    continue
            if stop_after_step:
                return "completed", None
        return "truncated", (truncated_reason
                             or f"已达到步骤上限（{steps_limit} 步），"
                             "返回已完成的部分结果")

    try:
        # 整体超时用 wait_for 兜住 —— 不用「每步都很快」来假设总时长可控
        # （实测单步就能到 120 秒）。
        status, truncated_reason = await asyncio.wait_for(
            _loop(), timeout=TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        status = "truncated"
        truncated_reason = (
            f"单次请求超过总时限（{int(TOTAL_TIMEOUT)} 秒），已返回已完成的部分结果")
    except llm_client.ProviderError as exc:
        raise AgentError(str(exc), status=502, code="provider_error") from exc

    elapsed_ms = int((time.time() - started) * 1000)

    # 没调 respond 也没给结论 —— 退化成「截断」，并说明原因
    if not ctx.answer:
        if ctx.jobs:
            ctx.answer = (f"已创建 {len(ctx.jobs)} 个生成任务，"
                          "可在左侧「生成」列表查看进度。")
        elif status != "truncated":
            status = "truncated"
            truncated_reason = truncated_reason or "模型没有给出最终结论"
        elif truncated_reason:
            ctx.answer = truncated_reason

    if status == "truncated" and truncated_reason:
        error = truncated_reason

    result = {
        "session_id": session_id or "",
        "status": status,
        "answer": ctx.answer,
        "jobs": ctx.jobs,
        "assets": list(ctx.assets.values()),
        "prompt": ctx.enhanced,
        "steps": steps,
        "usage": {"steps": len(steps), "tool_calls": tool_calls,
                  "elapsed_ms": elapsed_ms},
        "provider": {"name": provider.get("name"), "model": provider.get("model")},
        "error": error,
    }
    if ctx.respond_payload.get("needs_clarification"):
        result["status"] = "needs_clarification"
    return result
