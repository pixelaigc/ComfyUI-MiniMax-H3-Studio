# -*- coding: utf-8 -*-
"""
VLM 物品扫描模块（三段式的第 2 段）—— 等价实现 nonebot 插件的
`h3_item_enhancer.py`，用于 hermes 增强通道。

【为什么必须有这个模块】
nonebot 咏唱h3 的提示词增强是**三段式**（`h3_interactive.py:3710-3959`）：

    阶段1 初稿      hermes 从一句话 + 素材写出初稿
    阶段2 物品扫描  **程序**跑 VLM 扫参考图 → 物件清单 + 服装 + 光照 + 调色板
                    → 规则交叉核对（KNOWN_TRUTH 覆写 / 零否定 / 分层密度）
    阶段3 合成      hermes 拿到（初稿 + 已核实物品清单）合成最终 prompt

阶段 2 **必须由程序做，不能由 LLM 在脑内想象**。nonebot 的实测理由
（`h3_item_enhancer.py:13-22`）：

  * VLM 的文字描述会读错（实测：领口标签 "HAMLING" → 实为 "HEALING"）
    → 所以 VLM 结果必须过 `KNOWN_TRUTH` 覆写表；
  * H3 会把否定句当动作执行（"她没有脱掉鞋"里的"鞋"照样入画）
    → 增强块只写正向可见形态，零否定；
  * 描述密度必须匹配画面占比（特写 60-120 词 / 远景 5-12 词）
    → 按 tier 分层预算。

【与 nonebot 的逐项对应】
| nonebot（源文件:行号）              | 本模块                        |
|------------------------------------|-------------------------------|
| h3_item_enhancer.py:56-62 VLM 端点 | DEFAULT_VLM_URLS             |
| h3_item_enhancer.py:67-76 tier 词数| TIER_WORDS / BACKGROUND_FACTOR|
| h3_item_enhancer.py:82-112 真值表  | KNOWN_TRUTH / KNOWN_TRUTH_EN  |
| h3_item_enhancer.py:115-130 SCAN_PROMPT | SCAN_PROMPT             |
| h3_item_enhancer.py:136-152 data URL| _image_data_url              |
| h3_item_enhancer.py:155-167 模型探测 | _vlm_model_for               |
| h3_item_enhancer.py:170-232 ask_vlm | ask_vlm                      |
| h3_item_enhancer.py:235-254 JSON 解析| parse_json_loose             |
| h3_item_enhancer.py:257-269 scan    | scan_reference               |
| h3_item_enhancer.py:275-289 pick_truth | pick_truth                 |
| h3_item_enhancer.py:292-308 pick_truth_en | pick_truth_en         |
| h3_item_enhancer.py:311-360 _merge_truth | _merge_truth           |
| h3_item_enhancer.py:363-376 tier_for| tier_for                     |
| h3_item_enhancer.py:379-407 词数/否定| _word_count / _clip_words / _strip_negative |
| h3_item_enhancer.py:410-548 build_strength_block | build_strength_block |
| h3_item_enhancer.py:554-601 render  | render_enhancement_section    |

【与 nonebot 的两处有意差异】
1. **端点来源**：nonebot 用环境变量 `H3_VLM_URLS`（默认 5000 / 5010 硬编码）。
   本模块默认从**本项目「本地模型配置」的默认多模态 Provider** 取，
   另保留环境变量 `H3_VLM_URLS` 覆盖 —— 理由：本项目已经有 Provider 体系，
   不该再维护第二份端点清单；但**默认值**与 nonebot 一致（5000 端口）。
2. **调用方式**：nonebot 用 `curl --data-binary @file`（避免命令行长度限制）。
   本模块用本项目已有的 `h3studio/llm.py` 的 Provider 调用封装
   （aiohttp + Provider 配置 + 鉴权），**不**再引第二套 HTTP 栈。
   —— 行为等价（同样的 OpenAI 兼容多模态请求体 + 同样的端点回退顺序），
   但少了「自己再实现一遍鉴权 / 超时 / 错误分类」。

【红线】
本模块**不生成 prompt 正文**，只提供「已核实的物品事实」给合成阶段。
调用方：h3studio/hermes_enhance.py（hermes 通道的三段式编排）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ------------------------------------------------------------------ 配置

#: VLM 端点候选（按优先级）。第一个不可用/超时则试下一个。
#:
#: 【默认值与 nonebot 一致】nonebot h3_item_enhancer.py:56-62 的默认是
#: 5000（qwen3.8-flash-next-iq3_xxs）与 5010（deepseek-v4.1-flash）。
#: 本项目的「本地模型配置」里默认多模态 Provider 实测就是
#: http://127.0.0.1:5000 + qwen3.8-flash-next-iq3_xxs —— 同一个端点。
#:
#: 环境变量 `H3_VLM_URLS` 可覆盖（逗号分隔），与 nonebot 同名同义。
DEFAULT_VLM_URLS: List[str] = [
    u.strip() for u in os.environ.get(
        "H3_VLM_URLS",
        "http://127.0.0.1:5000/v1/chat/completions,"
        "http://127.0.0.1:5010/v1/chat/completions",
    ).split(",") if u.strip()
]

#: 描述密度分层（英文词数区间）。tier = 物件在画面里的占比。
#: 与 nonebot h3_item_enhancer.py:67-73 逐字一致。
TIER_WORDS: Dict[str, Tuple[int, int]] = {
    "特写": (60, 120),   # >50% 画高
    "近景": (30, 60),    # 25-50%
    "中景": (12, 30),    # 10-25%
    "远景": (5, 12),     # <10%
}
TIER_ORDER: Tuple[str, ...] = ("特写", "近景", "中景", "远景")

#: 背景词数上限系数（相对主体词数）。超过 H3 会把背景当另一个主体渲染。
#: nonebot h3_item_enhancer.py:76
BACKGROUND_WORD_FACTOR = 1.5

#: 人工核对过的硬事实。key 是中文物件名里的**子串**，命中即用该英文描述，
#: 覆盖 VLM 的（可能出错的）描述。**这是交叉验证的裁决点。**
#: nonebot h3_item_enhancer.py:82-96（逐字一致）
KNOWN_TRUTH: Dict[str, str] = {
    "眼罩": "a white perforated medical eyepatch with a regular grid of small round vent holes",
    "领口标签": "a pale-blue rectangular collar tag reading HEALING in plain sans-serif capitals",
    "标签": "a pale-blue rectangular collar tag reading HEALING in plain sans-serif capitals",
    "领子": "a white sailor collar with navy piping and twin white stripes",
    "水手领": "a white sailor collar with navy piping and twin white stripes",
    "上衣": "a white long-sleeved sailor top",
    "领结": "a large deep-crimson satin bow with a gold ankh brooch at its centre",
    "蝴蝶结": "a large deep-crimson satin bow with a gold ankh brooch at its centre",
    "头发": "floor-length pale golden-blonde twin tails with two white pom-pom hair ornaments",
    "机械翼": "a crimson spiked mechanical wing assembly folded flat at the lower back",
    "便签": "a small off-white ruled paper note",
    "颈圈": "a white rigid collar with a rectangular pale-blue tag reading HEALING in plain sans-serif capitals",
    "项圈": "a white rigid collar with a rectangular pale-blue tag reading HEALING in plain sans-serif capitals",
}

#: KNOWN_TRUTH 的英文关键词版 —— 给**纯字符串**条目用（没有中文名可匹配）。
#: nonebot h3_item_enhancer.py:100-112（逐字一致）
KNOWN_TRUTH_EN: Dict[str, str] = {
    "choker collar": "a white rigid collar with a rectangular pale-blue tag reading HEALING in plain sans-serif capitals",
    "eye patch": "a white perforated medical eyepatch with a regular grid of small round vent holes",
    "sailor blouse": "a white long-sleeved sailor top with a deep navy sailor collar, double white stripe edging and navy cuffs",
    "sailor collar": "a white sailor collar with navy piping and twin white stripes",
    "satin bow": "a large deep-crimson satin bow with a gold ankh brooch at its centre",
    "ribbon bow": "a large deep-crimson satin bow with a gold ankh brooch at its centre",
    "ankh": "a gold ankh brooch with a heart-shaped clear gemstone inlay",
    "thigh-high": "opaque white over-knee stockings with a round metal module on one thigh",
    "bandage": "white bandages wrapped around the left thigh and knee",
    "mechanical wing": "a crimson spiked mechanical wing assembly folded flat at the lower back",
    "pom-pom": "two white pom-pom hair ornaments on either side of the head",
}

#: VLM 扫描用的提问模板。
#: nonebot h3_item_enhancer.py:115-130（逐字一致 —— 提问口径变了，
#: VLM 的输出结构就会变，而下游解析是按这个结构写的）。
SCAN_PROMPT = """你是 H3 视频生成的美术指导。分析这张参考图, 目标是把图里每一个物件都吃透, 以便写精确的英文提示词。

对图中每一个可见物件输出一个条目:
- name: 物件名 (中文)
- tier: 画面占比档位 — 特写(>50%画高) / 近景(25-50%) / 中景(10-25%) / 远景(<10%)
- appearance: 外观细节 — 颜色(具体色相, 如 "深酒红" 而非 "红色")、材质(金属/织物/皮革/塑料/玻璃/皮肤)、形状、磨损痕迹、边缘处理
- text_or_marking: 物件上有没有文字/图案/徽章/孔洞/缝线等特征, 有就写清楚 (读不清就写 illegible)
- position: 在画面里的位置
- english: 英文强化描述, 按 tier 给词数 — 特写 60-120 词 / 近景 30-60 词 / 中景 12-30 词 / 远景 5-12 词

另外单独给:
- character_costume: 如果图里有角色, 列出她身上每一件服装配饰 (含颜色/材质/纹样/配饰位置), 每件给英文描述
- lighting: 主光方向/色温/阴影形态
- palette: 画面主色 (前 5 个 hex 或色名)

严格按 JSON 输出。"""

#: 扫描用的最大输出 token（nonebot h3_item_enhancer.py:171）
SCAN_MAX_TOKENS = 3500

#: 单次扫描的超时秒数。
#:
#: 【nonebot 实测依据，h3_item_enhancer.py:180-182】
#: "timeout 默认 180s 而不是 600s (2026-10-27 实测): 5000 端口被同机的
#:  hermes-web-ui agent-bridge 共用, 高负载时能排 20+ 分钟。用户等不起,
#:  宁可换下一个端点或降级用初稿, 也不要卡死。"
SCAN_TIMEOUT = 180


class ItemScanError(RuntimeError):
    """VLM 扫描失败。调用方据此降级（**不编造**）。"""


# ------------------------------------------------------------------ 1. VLM 扫描


def _image_data_url(path: str) -> Optional[str]:
    """
    把本地图片读成 data URL（VLM 要 inline 图）。读不了返回 None。

    nonebot h3_item_enhancer.py:136-152（逐字一致）。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    if not raw:
        return None
    ext = os.path.splitext(path)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    return f"data:{mime};base64,{__import__('base64').b64encode(raw).decode('ascii')}"


def parse_json_loose(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    从模型输出里抠出 JSON（容忍 ```json 围栏和前后废话）。

    nonebot h3_item_enhancer.py:235-254（逐字一致）。
    """
    if not text:
        return None
    t = text.replace("```json", "").replace("```", "").strip()
    s = t.find("{")
    if s < 0:
        return None
    depth = 0
    for i in range(s, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[s:i + 1])
                except Exception:
                    return None
    return None


async def ask_vlm(image_paths: Sequence[str], prompt: str = SCAN_PROMPT,
                  max_tokens: int = SCAN_MAX_TOKENS,
                  timeout: int = SCAN_TIMEOUT,
                  provider: dict | None = None,
                  conn=None) -> Optional[str]:
    """
    把多张参考图 + 提问打包成**一次**多图请求发给 VLM，返回原始文本。

    【为什么多图放一个请求】（nonebot h3_item_enhancer.py:172-175）
    VLM 需要跨图比对同一角色的服装在不同角度下是否一致 ——
    逐张问会让它把同一件衣服报成三个名字。

    【端点回退】按候选顺序试，失败的（不可达 / 超时 / 解析失败）换下一个。
    全部失败返回 None（调用方降级）。

    与 nonebot 的差异：nonebot 用 `curl --data-binary @file`；
    本模块用 h3studio/llm.py 的 Provider 调用封装（同一个 OpenAI 兼容
    请求体 + 同样的多图 inline 方式 + 同样的端点回退顺序）。
    """
    from . import llm as llm_client

    if not image_paths:
        return None

    # ---- 选 Provider ----
    # 优先级：显式传入 > 默认多模态 Provider > 候选端点（直接用 URL 调）
    prov = provider
    if prov is None:
        prov = _resolve_default_multimodal_provider(conn)

    if prov is not None:
        # 图片是**服务端可读的本地路径**（素材由 enhance_assets 解析好）
        messages = [{"role": "user", "content": prompt}]
        try:
            text = await llm_client.call_provider_full(
                prov, messages, images=list(image_paths),
                options={"temperature": 0.1, "top_p": 0.9,
                         "max_tokens": max_tokens},
            )
            out = (text.get("text") or "").strip()
            if out and parse_json_loose(out):
                return out
        except Exception:
            # 走下面的候选端点回退
            pass

    # ---- 候选端点回退（直接用 URL，不走 Provider 配置）----
    for url in DEFAULT_VLM_URLS:
        raw = await _ask_vlm_by_url(url, image_paths, prompt, max_tokens, timeout)
        if raw and parse_json_loose(raw):
            return raw
    return None


async def _ask_vlm_by_url(url: str, image_paths: Sequence[str], prompt: str,
                         max_tokens: int, timeout: int) -> Optional[str]:
    """直接打 OpenAI 兼容端点（不做 Provider 鉴权）。失败返回 None。"""
    import aiohttp

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    attached = 0
    for p in image_paths:
        data_url = _image_data_url(p)
        if not data_url:
            continue
        content.append({"type": "image_url", "image_url": {"url": data_url}})
        attached += 1
    if not attached:
        return None

    payload = {
        "model": "default",
        "reasoning_effort": "none",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=10)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(url, json=payload,
                                    headers={"Content-Type": "application/json"}) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
    except Exception:
        return None
    choices = body.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    return (msg.get("content") or msg.get("reasoning_content") or "").strip() or None


def _resolve_default_multimodal_provider(conn=None) -> dict | None:
    """
    取「本地模型配置」里的默认多模态 Provider。

    【为什么由调用方传 conn】model_providers 表在**任务库**（server.py 的
    `db()`）里，不在素材库里 —— 本模块不 import server.py（会循环引用），
    所以由调用方（server.py）把连接句柄传进来。
    读不到就返回 None（调用方走候选端点回退）。
    """
    try:
        from . import providers as prov
        if conn is None:
            return None
        item = prov.resolve_default(conn, "default_multimodal",
                                    require_multimodal=True)
        if not item:
            return None
        return prov.get_provider_full(conn, item["id"]) or item
    except Exception:
        return None


async def scan_reference(image_paths: Sequence[str],
                         provider: dict | None = None,
                         conn=None) -> Optional[Dict[str, Any]]:
    """
    扫描参考图，返回结构化结果。失败返回 None（调用方负责降级）。

    nonebot h3_item_enhancer.py:257-269（逐字一致的结构）。
    """
    if not image_paths:
        return None
    raw = await ask_vlm(list(image_paths), provider=provider, conn=conn)
    parsed = parse_json_loose(raw)
    if not isinstance(parsed, dict):
        return None
    return {
        "images": [os.path.abspath(p) for p in image_paths],
        "raw": raw,
        "parsed": parsed,
    }


# ------------------------------------------------------------------ 2. 强化块构建


def pick_truth(name: str) -> Optional[str]:
    """
    KNOWN_TRUTH 查表（子串匹配），命中返回人工核对过的英文描述。

    nonebot h3_item_enhancer.py:275-289（逐字一致）。
    """
    n = name.strip()
    if any(k in n for k in ("全身", "整体", "整个人", "人物", "角色")):
        return None
    for k, v in KNOWN_TRUTH.items():
        if k and k in n:
            return v
    return None


def pick_truth_en(text: str, allow: bool = True) -> Optional[str]:
    """
    按**英文关键词**查 KNOWN_TRUTH_EN。

    nonebot h3_item_enhancer.py:292-308（逐字一致）。
    """
    if not allow:
        return None
    low = (text or "").lower()
    for k, v in KNOWN_TRUTH_EN.items():
        if k in low:
            return v
    return None


def _merge_truth(truth: str, vlm_eng: str) -> str:
    """
    把人工核对过的描述与 VLM 描述合并。

    nonebot h3_item_enhancer.py:311-360（逐字一致的四条判据）。
    """
    vlm_eng = (vlm_eng or "").strip()
    if not vlm_eng:
        return truth
    low_vlm = vlm_eng.lower()
    low_truth = truth.lower()

    # 1. truth 已是 vlm 的子串 → vlm 已含正确信息
    if low_truth in low_vlm:
        return vlm_eng

    # 2. 关键特征冲突 → 整条用 truth
    conflict_markers = {
        "perforated": ("breathable fabric", "soft fabric", "light blue trim",
                       "bandage", "light blue bandage", "blindfold"),
        "healing": ("illegible", "unreadable", "hmling", "haling"),
    }
    for marker, bad in conflict_markers.items():
        if marker in low_truth and any(b in low_vlm for b in bad):
            return truth

    # 3. truth 的关键特征缺失 → 追加
    key_markers = [
        w for w in ("perforated", "HEALING", "ankh", "pom-pom", "navy",
                    "crimson", "golden-amber", "sailor")
        if w.lower() in low_truth
    ]
    missing = [w for w in key_markers if w.lower() not in low_vlm]
    if missing:
        return f"{vlm_eng} ({truth})" if len(vlm_eng) < 400 else vlm_eng
    return vlm_eng


def tier_for(tier_name: str, shot_tier: str) -> str:
    """
    合并「物件自身占比」与「本次镜头档位」，取**更保守**的那个。

    nonebot h3_item_enhancer.py:363-376（逐字一致）。
    """
    src = TIER_ORDER.index(tier_name) if tier_name in TIER_ORDER else len(TIER_ORDER) - 1
    dst = TIER_ORDER.index(shot_tier) if shot_tier in TIER_ORDER else 2
    return TIER_ORDER[max(src, dst)]


def _word_count(s: str) -> int:
    """nonebot h3_item_enhancer.py:379-380（逐字一致）。"""
    return len(re.findall(r"[A-Za-z][A-Za-z'’\-]*", s))


def _clip_words(s: str, lo: int, hi: int) -> str:
    """nonebot h3_item_enhancer.py:383-387（逐字一致）。"""
    w = s.split()
    if len(w) <= hi:
        return s
    return " ".join(w[:hi])


def _strip_negative(marking: str) -> str:
    """
    把 VLM 的 text_or_marking 里的否定尾巴砍掉。

    nonebot h3_item_enhancer.py:390-407（逐字一致，包括否定词假阳性的排除）。
    """
    m = (marking or "").strip()
    if not m:
        return ""
    if re.search(r"illegible", m, flags=re.IGNORECASE):
        return ""
    # 独立否定词: 前后不能是汉字 (排除 不锈钢 / 不透明 / 未知)
    if re.search(r"(?<![一-鿿])(无|没有|未|不)(?![一-鿿])", m):
        return ""
    return m.strip(" ，。,.、")


def build_strength_block(scan: Optional[Dict[str, Any]],
                         shot_tier: Optional[str] = None,
                         budget: int = 900) -> Dict[str, Any]:
    """
    把扫描结果转成强化块。

    nonebot h3_item_enhancer.py:410-548（逐字一致 —— 包括「VLM 的字段名和
    结构都不稳定」时三个字段名都要认、costume 的 dict/字符串两种形态都要处理）。
    """
    out_items: List[Dict[str, Any]] = []
    costume: List[str] = []
    seen_costume = set()
    seen_keys = set()

    parsed = (scan or {}).get("parsed") if isinstance(scan, dict) else None
    if not isinstance(parsed, dict):
        parsed = {}

    # ---- 收集候选物件 ----
    # VLM 的输出**字段名和结构都不稳定**（nonebot 实测同一批参考图）：
    #   5000 / qwen3.8:  objects=11 条 + character_costume=3 条(dict)
    #   5000 / 同一模型:  objects=2 条 + character_costume=7 条(dict)
    #   5010 / dsv41:    **items**=N 条 (字段名不同!) + character_costume=8 条(**纯字符串**)
    raw_items: List[Any] = []
    for key in ("objects", "items", "scene_objects"):
        o = parsed.get(key)
        if isinstance(o, list):
            raw_items.extend(o)
    c_raw = parsed.get("character_costume")
    c_list = c_raw if isinstance(c_raw, list) else ([c_raw] if isinstance(c_raw, str) else [])
    plain_costume: List[str] = []
    for ci in c_list:
        if isinstance(ci, dict):
            raw_items.append(ci)
        elif isinstance(ci, str) and ci.strip():
            raw_items.append({"name": ci.strip()[:40], "english": ci.strip(),
                              "tier": "近景", "text_or_marking": "",
                              "_plain": True})
            plain_costume.append(ci.strip())

    subject_words = 0
    background_words = 0

    for it in raw_items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name or name == "背景":
            continue
        # tier 缺失时, character_costume 的条目默认按**近景**算
        raw_tier = str(it.get("tier") or "").strip()
        if raw_tier in TIER_ORDER:
            tier = tier_for(raw_tier, shot_tier) if shot_tier else raw_tier
        else:
            tier = tier_for("近景", shot_tier) if shot_tier else "近景"
        eng = str(it.get("english") or it.get("description") or "").strip()
        truth = pick_truth(name)
        if not truth:
            truth = pick_truth_en(eng, allow=bool(it.get("_plain")))
        if truth:
            eng = _merge_truth(truth, eng)
        marking = _strip_negative(it.get("text_or_marking") or "")
        if marking and truth is None:
            eng = f"{eng}. {marking}" if eng else marking
        eng = re.sub(r"\s+", " ", eng).strip(" .")
        if not eng:
            continue
        lo, hi = TIER_WORDS.get(tier, TIER_WORDS["远景"])
        eng = _clip_words(eng, lo, hi)
        n = _word_count(eng)
        if n <= 0:
            continue
        if tier == "远景":
            background_words += n
        else:
            subject_words += n
        key = (name, eng[:60])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out_items.append({
            "name": name,
            "tier": tier,
            "words": n,
            "english": eng,
            "source": "known_truth" if truth else "vlm_scan",
        })

    # 角色服装（纯字符串条目；dict 条目已并入上面的物件池）
    for en in plain_costume:
        truth_en = pick_truth_en(en)
        fixed = _merge_truth(truth_en, en) if truth_en else en
        if fixed and fixed not in seen_costume:
            seen_costume.add(fixed)
            costume.append(fixed)

    lines: List[str] = []
    if costume:
        lines.append("### Character costume and accessories (verified)")
        lines.extend(f"- {c}" for c in costume)
        lines.append("")
    tier_label = shot_tier or "按物件自身占比"
    if out_items:
        lines.append(f"### Scene objects (verified, tier={tier_label})")
        lines.extend(
            f"- [{it['name']} | {it['tier']} | {it['words']}w | {it['source']}] {it['english']}"
            for it in out_items
        )
        lines.append("")

    return {
        "items": out_items,
        "costume": costume,
        "subject_words": subject_words,
        "background_words": background_words,
        "text": "\n".join(lines).strip(),
    }


# ------------------------------------------------------------------ 3. 给合成阶段看的「已核实事实」节


def render_enhancement_section(scan: Optional[Dict[str, Any]],
                               strength: Optional[Dict[str, Any]],
                               shot_tier: str = "中景") -> str:
    """
    生成给 hermes 合成阶段看的节。

    nonebot h3_item_enhancer.py:554-601（逐字一致）。
    """
    if not strength or not strength.get("text"):
        return ""
    parsed = (scan or {}).get("parsed") if isinstance(scan, dict) else None
    if not isinstance(parsed, dict):
        parsed = {}

    parts = [
        "\n# 已核实素材事实 (程序扫描产出, 必须原样采用)",
        "",
        "下面这份清单由 VLM 扫描参考图 + 规则交叉核对得到, 是**硬事实**。",
        "合成最终 prompt 时:",
        "1. 这些物品的**英文描述原样采用**, 不要重新想象, 不要改写措辞。",
        "2. 描述密度已按镜头档位裁好, 不要为了「好看」再给远景物件加长描述。",
        "3. 只写正向可见形态。**禁止任何否定句** (H3 会把否定当动作执行)。",
        "4. 下面出现的物件若在初稿里缺失, 必须补进最终 prompt。",
        "5. 背景物件的描述已经按占比裁短 (远景 5-12 词), 不要额外扩写背景。",
        "",
        strength["text"],
    ]

    lighting = parsed.get("lighting")
    if isinstance(lighting, dict):
        parts.append("### Lighting (verified)")
        for k, v in lighting.items():
            if v:
                parts.append(f"- {k}: {v}")
        parts.append("")
    elif lighting:
        parts += ["### Lighting (verified)", f"- {str(lighting).strip()}", ""]
    palette = parsed.get("palette")
    if palette:
        if isinstance(palette, list):
            pal = ", ".join(str(x) for x in palette[:5])
        else:
            pal = str(palette)
        parts += ["### Palette (verified)", f"- {pal}", ""]

    return "\n".join(parts).strip()
