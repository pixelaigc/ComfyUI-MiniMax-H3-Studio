# -*- coding: utf-8 -*-
"""
内置 Agent 的**能力管理配置**（能力开关 / 搜索引擎 / 默认参数 / 步数上限）。

【为什么单独一个模块】
agent.py 是编排器（跑几轮、怎么喂工具结果），agent_tools.py 是工具实现。
「哪些能力开着、搜索引擎指向哪、文生视频默认用什么」是**配置**，
既不是编排逻辑也不是工具逻辑 —— 混进那两个文件会让两边都难读。
所以这里只放四件事：
  1. 配置的**单一真源**（默认值 + 合法取值 + 归一）
  2. 持久化（复用 server 的 jobs.db -> model_provider_prefs 表）
  3. 能力开关的**闸门**（关掉的工具模型连看都看不到）
  4. 搜索引擎的**目录**（DDG / 局域网 9898 聚合引擎 / 自定义 HTTP 接口）

【持久化口径】
存 server 的 model_provider_prefs 表，key = "agent_settings"。
理由与 /api/video-config 一致：局域网客户端共用同一份配置，
不依赖浏览器 localStorage —— 换台电脑打开还是同一份设置。

【默认值为什么是这个】
  * file_read  默认开：只读、且路径被限制在允许根目录内（配置项 file_roots）。
  * web_search 默认开：外部搜索是只读能力，失败也只是返回错误。
  * code_exec  默认**关**：执行任意代码是本清单里唯一有副作用的能力，
    必须由用户显式打开（打开前会看到明确的风险提示）。
  * media_tools 默认开：ffmpeg 音视频处理是本工作台的核心场景。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import gen_defaults

#: 配置在 model_provider_prefs 表里的 key。
PREF_KEY = "agent_settings"

#: 步数上限的**硬顶**。用户只能在 [1, MAX_STEPS_HARD_CAP] 里调。
#:
#: 【为什么硬顶是 50】用户 2026-11-13 审查目标 4/5/6/7 明确要求
#: 「agent 工具调用步数上限从 8 步提升到至少 50 步」。
#: 50 是**上限**不是默认值 —— 用户可以在「agent 管理」里调低（省时间）。
MAX_STEPS_HARD_CAP = 50
MAX_STEPS_MIN = 1

#: 工具调用次数上限（跨所有步骤累计）。
#:
#: 【为什么跟着步数走】50 步里每步平均 1.5 次工具调用 -> 75 次。
#: 给 120 是「够跑完长链路 + 留余量」，真正的兜底仍是 TOTAL_TIMEOUT。
MAX_TOOL_CALLS = 120

#: 单次请求总超时（秒）。50 步 × 单步 20~120 秒 = 1000~6000 秒，
#: 所以总时限必须跟着抬 —— 否则步数上限提到 50 也跑不完第 20 步。
TOTAL_TIMEOUT = 2400.0

#: 总时限的**可调区间**（秒）—— 2026-11-14 需求 6。
#:
#: 【为什么要有上下限】
#:   * 下限 60 秒：比这更短的时限连一次模型调用都跑不完（实测单步 20~120 秒），
#:     等于「一进 Agent 就超时」，必然是误配置。
#:   * 上限 14400 秒（4 小时）：再长也只是让一个卡死的会话占用并发闸门更久
#:     （AGENT_CONCURRENCY 是全局闸门），对用户没有实际意义。
#:
#: 【为什么默认还是 2400】保持既有行为不变 —— 用户不改就是老样子。
TOTAL_TIMEOUT_MIN = 60
TOTAL_TIMEOUT_MAX = 14400
TOTAL_TIMEOUT_DEFAULT = int(TOTAL_TIMEOUT)

#: 能力清单（key -> 中文名 + 说明 + 是否需要外部依赖）。
#:
#: 「是否内置」= 代码在本仓库里（不需要额外装插件）；
#: 「外部依赖」= 需要本机装了什么（ffmpeg / 网络）。
CAPABILITIES: dict[str, dict] = {
    "file_read": {
        "label": "文件读取",
        "builtin": True,
        "requires": "无（受 file_roots 路径白名单限制）",
        "description": "读取工作台/仓库/素材库派生目录下的文本文件，"
                       "供 Agent 查配置、日志、脚本。",
    },
    "web_search": {
        "label": "外部搜索",
        "builtin": True,
        "requires": "网络可达的搜索引擎（DDG 或局域网 9898 聚合引擎）",
        "description": "按配置的搜索引擎做外部检索，把结果作为参考资料。",
    },
    "code_exec": {
        "label": "代码生成与执行",
        "builtin": True,
        "requires": "无（默认关闭，需用户显式启用）",
        "description": "运行一段 Python 代码并拿回 stdout/stderr。"
                       "有副作用，默认关闭。",
    },
    "media_tools": {
        "label": "ffmpeg 音视频处理",
        "builtin": True,
        "requires": "本机 ffmpeg / ffprobe（见 ffmpeg_path）",
        "description": "音视频合并拼接、转码、抽帧、混音、AV 合流"
                       "（真实调用本机 ffmpeg，非模拟）。",
    },
}

#: 能力 -> 依赖的外部命令/网络要求（给「能力清单」文档生成用）。
CAPABILITY_REQUIRES = {key: meta["requires"] for key, meta in CAPABILITIES.items()}

#: 默认启用的能力。code_exec 默认关（见模块 docstring）。
DEFAULT_CAPABILITIES = {
    "file_read": True,
    "web_search": True,
    "code_exec": False,
    "media_tools": True,
}

#: 搜索引擎类型白名单。
SEARCH_ENGINE_TYPES = ("ddg", "openwebui", "custom")

#: 默认搜索引擎清单。
#:
#: 【为什么默认带上 9898】用户自建的局域网聚合引擎就在本机 9898，
#: 它聚合了 Bing / Playwright 等多个源，比裸 DDG 稳定得多。
#: 【URL 用 127.0.0.1 还是 192.168.3.78】两者都通（同一个进程）。
#: 默认写 127.0.0.1（本机自用），局域网其它机器访问这台 Studio 时，
#: 用户可以在「agent 管理」里改成 192.168.3.78 —— 这就是为什么
#: URL 必须是**可填写**的字段，而不是写死的常量。
DEFAULT_SEARCH_ENGINES = [
    {
        "id": "local_9898",
        "name": "局域网聚合引擎（9898）",
        "type": "openwebui",
        "url": "http://127.0.0.1:9898/api/openwebui/tools/search_general",
        "method": "POST",
        "enabled": True,
    },
    {
        # DDG 的**裸 HTML** 端点在本机会被反爬拦（实测 HTTP 202 + anomaly
        # challenge，见 agent_websearch._search_ddg 的报错文案）。
        # 但 9898 聚合引擎背后**有** DDG 源，并且提供了单引擎入口 ——
        # 所以这里指向 9898 的 DDG 单引擎端点，既满足「支持接入 DDG」，
        # 又真的能出结果。
        "id": "ddg",
        "name": "DuckDuckGo（经 9898 代理）",
        "type": "custom",
        "url": "http://127.0.0.1:9898/api/openwebui/search/ddg",
        "method": "POST",
        "enabled": True,
    },
    {
        # 直连 DDG（备用）。本机实测会被反爬拦（HTTP 202 + anomaly），
        # 留在这里是为了「换个网络环境 / 反爬策略变了」时能直接切过去，
        # 也是「DDG 是可配置 URL 而不是硬编码」的证据。
        "id": "ddg_direct",
        "name": "DuckDuckGo（直连，可能被反爬拦截）",
        "type": "ddg",
        "url": "https://html.duckduckgo.com/html/",
        "method": "GET",
        "enabled": False,
    },
]

#: 搜索引擎数量上限（防止配置被塞爆）。
MAX_SEARCH_ENGINES = 8

#: 文生视频（MiniMax H3）**默认参数**。
#:
#: 【为什么默认是 kitchen + dmad】用户 2026-11-13 审查目标 5 明确要求：
#: 「agent 调用文生视频默认使用 comfy kitchen 注意力机制 + dmad 加速 LoRA」。
#:   * kitchen = video_models.VIDEO_ATTENTION_MODES["kitchen"]
#:     （节点 ModelAttentionBackend，inputs {"attention": "comfy kitchen attention"}）
#:   * dmad   = video_models.VIDEO_ACCEL_LORA_COMBOS["dmad_4step_critic"]
#:     （dmad_minimax_h3_4step_full_critic_comfyui.safetensors）
#: 两者都只是**默认值** —— 用户可以在「agent 管理」里改任意一项。
DEFAULT_T2V_DEFAULTS = {
    "model_key": "fl2va_pruned_int8",
    "attention": "kitchen",
    "accel_lora": "dmad_4step_critic",
    "aspect": "16:9",
    "megapixels": 0.4,
    "duration": 5.0,
}


#: t2v 的**完整**默认参数（= gen_defaults["t2v"]）。
#:
#: 【为什么要两个函数】DEFAULT_T2V_DEFAULTS 是老的 6 字段结构（外部脚本
#: 按这些键取值），而 gen_defaults["t2v"] 现在是 11 字段的完整集
#: （多了步数 / 采样器 / 调度器 / 两个 sigma shift）。
#: 老结构保留为**兼容镜像**的一部分 —— 两者取值一致。
#:
#: 【2026-11-16 第5轮代码审查 C-16 修正】
#: 第4轮报告把 ``default_t2v_mirror`` 记成「零引用」—— **现场核实不成立**：
#: 它被本文件下方的 ``defaults()`` 调用（``"t2v_defaults": default_t2v_mirror()``），
#: 是落库初始值的来源，不能删。
#: 真正零引用的只有 ``default_t2v_defaults()``（老结构的函数式包装），
#: 那个已在本轮删除 —— DEFAULT_T2V_DEFAULTS 常量本身保留
#: （tests/test_agent_power.py 仍在引用它，且它是对外承诺的兼容镜像）。
def default_t2v_mirror() -> dict:
    return dict(gen_defaults.default_values("t2v"))


#: 默认配置全量。
def defaults() -> dict:
    return {
        "max_steps": MAX_STEPS_HARD_CAP,
        "capabilities": dict(DEFAULT_CAPABILITIES),
        "search_engines": [dict(e) for e in DEFAULT_SEARCH_ENGINES],
        # 【2026-11-14 需求 A4】按生成类型分别配置的默认参数。
        "gen_defaults": gen_defaults.default_gen_defaults(),
        # 兼容镜像：老结构（t2v_defaults）。读取时它**优先**于
        # gen_defaults["t2v"]；写入时两者同步。
        "t2v_defaults": default_t2v_mirror(),
        "file_roots": [],
        "code_exec_timeout_sec": 30,
        "ffmpeg_path": "",
        # 【2026-11-14 需求 6】总时限（秒）—— 以前是写死的模块常量，
        # 用户在「agent 管理」里看得到但改不了（灰色 disabled）。
        # 现在它是一份**可持久化**的配置：改了 -> 存库 -> 下次运行按新值生效。
        "total_timeout": TOTAL_TIMEOUT_DEFAULT,
    }


# ------------------------------------------------------------------ 归一


def _clean_str(value, limit: int = 300) -> str:
    return str(value if value is not None else "").strip()[:limit]


def normalize_capabilities(raw) -> dict:
    """能力开关归一：只认登记过的 key，缺省用默认值。"""
    out = dict(DEFAULT_CAPABILITIES)
    if isinstance(raw, dict):
        for key in CAPABILITIES:
            if key in raw:
                out[key] = _as_bool(raw[key])
    return out


def _as_bool(value) -> bool:
    """把「看起来像布尔」的值规整成真 bool（模型/前端都可能传字符串）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text not in ("", "0", "false", "no", "off", "none", "null")


def normalize_search_engines(raw) -> list[dict]:
    """搜索引擎清单归一：类型白名单 + URL 必填 + 数量上限 + id 去重。"""
    out: list[dict] = []
    seen: set[str] = set()
    items = raw if isinstance(raw, list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _clean_str(item.get("url"), 400)
        if not url:
            continue
        etype = _clean_str(item.get("type"), 20).lower()
        if etype not in SEARCH_ENGINE_TYPES:
            etype = "custom"
        method = _clean_str(item.get("method"), 8).upper()
        if method not in ("GET", "POST"):
            method = "POST" if etype == "openwebui" else "GET"
        raw_id = _clean_str(item.get("id"), 60)
        eng_id = raw_id or f"engine_{len(out) + 1}"
        base = eng_id
        suffix = 2
        while eng_id in seen:
            eng_id = f"{base}_{suffix}"
            suffix += 1
        seen.add(eng_id)
        out.append({
            "id": eng_id,
            "name": _clean_str(item.get("name"), 80) or eng_id,
            "type": etype,
            "url": url,
            "method": method,
            "enabled": _as_bool(item.get("enabled", True)),
        })
        if len(out) >= MAX_SEARCH_ENGINES:
            break
    return out


def normalize_gen_defaults(raw, legacy_t2v=None,
                            legacy_explicit=None) -> dict:
    """
    按生成类型分别配置的默认参数归一。

    【为什么需要 legacy_explicit】
    归一时必须知道「客户端**真的**给了 t2v_defaults 的哪些键」。
    只有**显式给出的键**才优先于 gen_defaults["t2v"] ——
    否则老客户端只 PUT 一个 t2v_defaults（6 个字段）时，
    会把 gen_defaults["t2v"] 里新增的字段（步数/采样器/…）覆盖成默认值，
    用户在管理页里刚配好的值就被悄悄冲掉了。

    legacy_t2v 是**原始**（未归一）的 t2v_defaults；这里只取它的**键集合**
    来判断「显式」，取值仍然走同一套字段归一（脏值不进库）。
    """
    explicit = legacy_explicit
    if explicit is None:
        explicit = (legacy_t2v.keys()
                    if isinstance(legacy_t2v, dict) else ())
    out = gen_defaults.normalize_gen_defaults(raw)
    if "t2v" in out:
        # 【关键】老结构的取值先归一再合并 —— 不能把**脏值**原样搬进
        # gen_defaults["t2v"]（脏值会跟着落库，还会覆盖用户新配的值）。
        legacy_clean = gen_defaults.normalize_t2v_legacy(legacy_t2v)
        merged = gen_defaults.merge_t2v_with_gen(
            out["t2v"], legacy_clean, explicit)
        out["t2v"] = merged
    return out


def normalize_file_roots(raw) -> list[str]:
    """文件读取允许的根目录。绝对路径，去重，最多 8 个。"""
    out: list[str] = []
    for item in (raw if isinstance(raw, list) else []):
        text = _clean_str(item, 400)
        if not text:
            continue
        try:
            resolved = str(Path(text).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
        if resolved not in out:
            out.append(resolved)
        if len(out) >= 8:
            break
    return out


def normalize(payload: dict | None) -> dict:
    """把前端提交的配置归一成合法形状（非法项回落默认值，不整体拒绝）。"""
    base = defaults()
    if not isinstance(payload, dict):
        return base
    try:
        steps = int(payload.get("max_steps"))
    except (TypeError, ValueError):
        steps = base["max_steps"]
    base["max_steps"] = max(MAX_STEPS_MIN, min(MAX_STEPS_HARD_CAP, steps))
    base["capabilities"] = normalize_capabilities(payload.get("capabilities"))
    engines = payload.get("search_engines")
    base["search_engines"] = (normalize_search_engines(engines)
                               if isinstance(engines, list)
                               else base["search_engines"])
    # 【2026-11-14 需求 A4】按生成类型分别配置的默认参数。
    #
    # 【为什么先归一 legacy 再归一 gen】老结构 t2v_defaults 是**兼容镜像**：
    # 客户端显式给了的键优先于 gen_defaults["t2v"]，没给的键由 gen_defaults 补。
    # 两边都归一后才合并 —— 脏值不会落库。
    #
    # 【2026-11-15 修 —— 管理页改的值存不住】
    #
    # 【原来错在哪】legacy_explicit = raw_legacy.keys() —— 只要请求里带了
    # t2v_defaults，它的**每一个键**都算「显式给出」，于是合并时 legacy
    # 全量覆盖 gen_defaults["t2v"]。
    # 而 GET /api/agent/settings 的响应里**总是带着 t2v_defaults**（它是
    # 兼容镜像，随 gen_defaults 一起下发）—— 于是前端「读 -> 改 -> 存」
    # 的一次往返里，用户在管理页改的 gen_defaults["t2v"].steps
    # 被自己刚读到的旧镜像覆盖回去，存不进去。
    # 实测：GET 拿到 t2v.steps=4 -> 改 7 -> PUT -> GET 仍是 4。
    #
    # 【怎么区分老客户端和新客户端 —— 靠 legacy 载荷的「形状」】
    #   * 老客户端（只懂 t2v_defaults）只发**原来那 6 个字段**：
    #     model_key / attention / accel_lora / aspect / megapixels / duration。
    #   * 新客户端（懂 gen_defaults）会**把自己的 gen_defaults 原样回传**，
    #     并且它的兼容镜像是服务端下发的**完整 11 字段**那一份。
    # 于是「legacy 只带 6 个老字段」= 老客户端 -> legacy 全量生效；
    # 「legacy 带完整字段」= 新客户端回声 -> legacy **不参与合并**，
    # 以 gen_defaults 为准（legacy 只当镜像，写库时从 gen_defaults 同步）。
    #
    # 【为什么不能只看「有没有 gen_defaults」】老客户端也可能顺带把整份
    # settings 回传（里面有服务端下发的 gen_defaults），此时若按
    # 「有 gen_defaults 就算新客户端」判，老客户端的 t2v_defaults
    # 就会被忽略 —— 那会破掉既有契约（tests/test_a4_gen_defaults.py
    # 里 TestLegacyT2vCompat 那几条）。
    # 「legacy 载荷的形状」才是可靠的判据：老客户端**不可能**发出
    # 带 steps/sampler/scheduler 的 t2v_defaults（那 5 个字段是 A4 才加的）。
    _LEGACY6 = {"model_key", "attention", "accel_lora", "aspect",
                "megapixels", "duration"}
    raw_gen = payload.get("gen_defaults")
    raw_legacy = payload.get("t2v_defaults")
    legacy_keys = (set(raw_legacy.keys())
                   if isinstance(raw_legacy, dict) else set())
    legacy_is_old_client = bool(legacy_keys) and legacy_keys <= _LEGACY6
    if legacy_is_old_client:
        # 老客户端：只有 t2v_defaults，按它的键集显式合并
        base["gen_defaults"] = normalize_gen_defaults(
            raw_gen, raw_legacy, legacy_keys)
    else:
        # 新客户端（或没给 legacy）：gen_defaults 是权威源，
        # legacy 只当兼容镜像 —— 不反向覆盖用户改过的值。
        base["gen_defaults"] = normalize_gen_defaults(raw_gen)
    # 写库时两者同步 —— 老客户端 / 老脚本读到的永远是最新值。
    base["t2v_defaults"] = dict(base["gen_defaults"].get("t2v") or {})
    base["file_roots"] = normalize_file_roots(payload.get("file_roots"))
    try:
        timeout = int(payload.get("code_exec_timeout_sec"))
    except (TypeError, ValueError):
        timeout = base["code_exec_timeout_sec"]
    base["code_exec_timeout_sec"] = max(5, min(300, timeout))
    base["ffmpeg_path"] = _clean_str(payload.get("ffmpeg_path"), 400)
    # 【2026-11-14 需求 6】总时限归一：夹到 [60, 14400]，
    # 非法值（None / 字符串 / 0）回落到默认值 —— 不整体拒绝整份配置。
    raw_timeout = payload.get("total_timeout")
    try:
        total_timeout = int(raw_timeout) if raw_timeout is not None else base["total_timeout"]
    except (TypeError, ValueError):
        total_timeout = base["total_timeout"]
    base["total_timeout"] = max(TOTAL_TIMEOUT_MIN,
                                min(TOTAL_TIMEOUT_MAX, total_timeout))
    return base


# ------------------------------------------------------------------ 持久化


def load(conn) -> dict:
    """从 model_provider_prefs 读配置（读不到/坏了都回默认值）。"""
    try:
        row = conn.execute("SELECT value FROM model_provider_prefs WHERE key=?",
                           (PREF_KEY,)).fetchone()
    except Exception:
        return defaults()
    if not row or not row["value"]:
        return defaults()
    try:
        raw = json.loads(row["value"])
    except (TypeError, ValueError):
        return defaults()
    return normalize(raw)


def save(conn, payload: dict) -> dict:
    """写配置（归一后写库），返回真正生效的配置。"""
    clean = normalize(payload)
    conn.execute(
        "INSERT INTO model_provider_prefs (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (PREF_KEY, json.dumps(clean, ensure_ascii=False)))
    return clean


# ------------------------------------------------------------------ 派生信息


def ffmpeg_path(settings: dict | None = None) -> str:
    """
    ffmpeg 可执行文件路径。

    优先级：配置里的 ffmpeg_path > PATH 里的 ffmpeg > /usr/bin/ffmpeg。
    返回空串 = 本机找不到（工具会明确报错，不静默假装）。
    """
    configured = ""
    if isinstance(settings, dict):
        configured = _clean_str(settings.get("ffmpeg_path"), 400)
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("ffmpeg")
    if found:
        return found
    fallback = Path("/usr/bin/ffmpeg")
    return str(fallback) if fallback.exists() else ""


def ffprobe_path(settings: dict | None = None) -> str:
    """ffprobe 可执行文件路径（与 ffmpeg 同源目录优先）。"""
    configured = ""
    if isinstance(settings, dict):
        configured = _clean_str(settings.get("ffmpeg_path"), 400)
    if configured:
        sibling = Path(configured).parent / "ffprobe"
        if sibling.exists():
            return str(sibling)
    found = shutil.which("ffprobe")
    if found:
        return found
    fallback = Path("/usr/bin/ffprobe")
    return str(fallback) if fallback.exists() else ""


def capability_state(settings: dict | None) -> dict:
    """能力开关的**权威视图**（给 API 与前端渲染用）。"""
    caps = normalize_capabilities(
        (settings or {}).get("capabilities") if isinstance(settings, dict) else None)
    out = {}
    for key, meta in CAPABILITIES.items():
        out[key] = {
            "key": key,
            "label": meta["label"],
            "builtin": meta["builtin"],
            "requires": meta["requires"],
            "description": meta["description"],
            "enabled": bool(caps.get(key)),
        }
    return out


def snapshot(conn) -> dict:
    """给 Agent 运行期用的一次性快照（读配置 + 派生依赖路径）。"""
    settings = load(conn)
    return {
        "settings": settings,
        "ffmpeg_path": ffmpeg_path(settings),
        "ffprobe_path": ffprobe_path(settings),
    }
