# -*- coding: utf-8 -*-
"""
Qwen-Image 2.1「图片编辑」的参考图 —— 素材库来源约束 + 占位标记 + 智能排序。

为什么单独成文件：
  参考图这块的规则散在「素材库」「提示词」「工作流」三处，容易各处改一处就错位。
  这里把**唯一事实源**集中起来：
    1. 参考图**只能**来自素材库（asset_id + 版本快照），不接受裸路径；
    2. 占位标记编号与 ComfyUI image 插槽顺序**严格一致**（图1 == <image1>）；
    3. 移除参考图后重新编号，连续无空洞；
    4. 智能排序可降级为确定性基础排序，模型不可用不报错。

【占位标记用 <imageN> 而不是 <参考图N>】
  官方 PE-I2I 的 system prompt 原文（h3studio/skills/prompt-enhancer/assets/
  qwen21-pe-i2i.txt，摘自官方 ComfyUI 模板的 PrimitiveStringMultiline）写的是：
    "For Multi-Image Input (N >= 2), the rewritten instruction MUST use
     `<image1>`, `<image2>`, ... to refer to each input image.
     Do not use natural language references like "图1", "第一张图"..."
  而 ComfyUI 侧 comfy/text_encoders/qwen_image21.py:23 的 tokenize 也用
  "<image{n}>" 作为视觉块前缀（" ".join("<image{}>{}".format(i+1, VISION_BLOCK))）。
  -> 用中文「<参考图1>」模型认不出来，占位会失效。
  所以：编号标记用官方的 <imageN>；中文「参考图N」只作为**界面文案**。

【N=1 时不要用标签】
  同一份官方 PE-I2I system prompt 明确写：单图输入（N=1）**不要**用标签，
  自然表述（"图像"/"图片中"/"the image"）。所以只有 N>=2 时才注入 <imageN> 标记。
"""
from __future__ import annotations

import re

#: 参考图数量上限（与 workflows.QWEN_IMAGE_MAX_REF_IMAGES 保持一致；
#: 那边的值是「模型官方能力 10」与「ComfyUI 16 插槽」取小者 = 10）
MAX_REF_IMAGES = 10
MIN_REF_IMAGES = 1

#: 占位标记模板（官方 PE-I2I 语义）
PLACEHOLDER_RE = re.compile(r"<image(\d+)>")

#: 中文界面文案用的序号词（不是模型占位符）
REF_LABEL = "参考图"


# ------------------------------------------------------------------ 工具


def placeholder_for(index: int) -> str:
    """第 index 张（0 基）参考图对应的官方占位标记。"""
    return f"<image{index + 1}>"


def parse_placeholders(text: str) -> list[int]:
    """解析文本里出现的占位标记序号（1 基，已去重并升序）。"""
    found = {int(m) for m in PLACEHOLDER_RE.findall(text or "")}
    return sorted(found)


def renumber_placeholders(text: str, mapping: dict[int, int]) -> str:
    """
    按 mapping（旧序号 -> 新序号）重排文本里的占位标记。

    必须**单遍替换**：用 re.sub 的回调一次扫过，每个占位按它**原本**的序号
    查表替换。

    之前用「先替换大的、再替换小的」来避免连环替换 —— 那是错的：
    mapping={2:1, 3:2} 时，先把 <image3> 变成 <image2>，
    接着这一轮又把新产生的 <image2> 变成 <image1>，
    结果三个占位全变成 <image1>。单遍替换没有这个问题。
    """
    if not text:
        return text or ""
    if not mapping:
        return text

    def _replace(match: "re.Match[str]") -> str:
        old = int(match.group(1))
        new = mapping.get(old)
        return f"<image{new}>" if new is not None else match.group(0)

    return PLACEHOLDER_RE.sub(_replace, text)


def strip_placeholders(text: str) -> str:
    """移除所有占位标记（移除参考图时清掉悬空标记用）。"""
    cleaned = PLACEHOLDER_RE.sub("", text or "")
    # 移除标记后可能留下多余空格 / 空行
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_refs(refs) -> list[dict]:
    """
    归一化参考图列表：保证每项都是 dict，且带齐内部字段。

    内部字段（不直接暴露给前端，前端用的是同一份 dict 的子集）：
      asset_id     素材库条目 ID —— 必填，这是「参考图必须来自素材库」的锚点
      version      素材版本号（快照语义）
      name         素材名
      description  参考图描述（来自素材库分析结果；用户可手写覆盖）
      tags         素材标签列表
      created_at   素材入库时间（智能排序信号）
      source       描述来源：analysis（素材库分析）/ user（用户手写）/ empty（无）
      preview_url  缩略图
      missing      素材已删除/失效（True 时 UI 必须明确提示）
    """
    out = []
    for item in refs or []:
        if not isinstance(item, dict):
            continue
        asset_id = str(item.get("asset_id") or "").strip()
        if not asset_id:
            # 没有 asset_id 的条目一律丢弃 —— 参考图必须锚定到素材库条目
            continue
        source = str(item.get("description_source") or item.get("source_desc") or "").strip()
        description = str(item.get("description") or "").strip()
        if not source:
            source = "analysis" if description else "empty"
        if source == "empty":
            description = ""
        out.append({
            "asset_id": asset_id,
            "version": item.get("version"),
            "name": str(item.get("name") or ""),
            "description": description,
            "description_source": source,
            "tags": [str(t) for t in (item.get("tags") or []) if str(t).strip()],
            "created_at": item.get("created_at"),
            "preview_url": item.get("preview_url"),
            "missing": bool(item.get("missing")),
            "ordinal": item.get("ordinal"),
        })
    return out


# ------------------------------------------------------------------ 占位注入


def build_reference_block(refs: list[dict], style: str = "inline") -> str:
    """
    构造参考图占位 + 描述的文本块。

    style:
      inline   每行一条：`<image1> 一只橘猫侧坐在窗台，暖光`
      bullet   带项目符号，便于阅读

    N=1 时**不注入占位标记**（官方 PE-I2I system prompt 明确要求），
    但仍然给一行描述（自然表述，不带标签）。

    【占位编号按「原始列表下标」算，不按过滤后的下标】
    失效素材（missing）会被跳过，但**后面的图不前移** —— 否则
    「图1 已失效、图2 有效」时，图2 的占位会变成 <image1>，
    而模型看到的 image_1 插槽拿到的是图2 —— 编号与插槽错位。
    失效素材在提交时会被 validate_refs 拦下，所以不会真的带着错位跑。
    """
    total = len(refs)
    lines = []
    for index, ref in enumerate(refs):
        if ref.get("missing"):
            continue
        desc = (ref.get("description") or "").strip()
        marker = placeholder_for(index)
        if total < 2:
            # 单图：官方要求不用标签 -> 只给描述
            line = desc or (ref.get("name") or "")
        elif style == "bullet":
            line = f"- {marker} {desc}".rstrip()
        else:
            line = f"{marker} {desc}".rstrip()
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "\n".join(lines)


def inject_reference_block(prompt: str, refs: list[dict], style: str = "inline") -> str:
    """
    把参考图占位块注入提示词。

    注入位置：提示词**前面**（占位在前，像「素材说明」段）。
    官方 PE-I2I 的多图模板本身就是「<image1> 是画布图…」起手的，
    占位在前更符合官方写法。

    幂等：若提示词里已经有占位标记（用户自己写了 <image1>），**不重复注入**。
    这时**不动**用户写的编号 —— 用户写 <image2> 就是想指第 2 张，
    强行重编号会改坏用户的语义。编号对齐由前端在增删参考图时负责
    （见 web/qwen-refs.js 的 reindexPrompt）。
    """
    prompt = prompt or ""
    block = build_reference_block(refs, style=style)
    if parse_placeholders(prompt):
        # 用户手写了占位 -> 不注入第二份，也不动他的编号
        return prompt
    if not block:
        return prompt
    return f"{block}\n{prompt}" if prompt else block


# ------------------------------------------------------------------ 一致性校验


def validate_refs(refs: list[dict], prompt: str = "", require: bool = True) -> dict:
    """
    参考图一致性校验 —— 提交前/生成前都要过这一关。

    检查项：
      1. 数量在 [MIN, MAX] 内
      2. 每项都锚定到素材库 asset_id
      3. 没有失效素材（missing）
      4. 提示词里的占位编号与参考图数量**一致**（不多不少、连续）
      5. 占位编号没有空洞（1..N 连续）

    返回 {"ok": bool, "errors": [...], "warnings": [...]}
    """
    errors: list[str] = []
    warnings: list[str] = []

    count = len(refs)
    if require and count < MIN_REF_IMAGES:
        errors.append(f"参考图至少需要 {MIN_REF_IMAGES} 张，当前 {count} 张")
    if count > MAX_REF_IMAGES:
        errors.append(f"参考图最多 {MAX_REF_IMAGES} 张，当前 {count} 张")

    for index, ref in enumerate(refs):
        if not ref.get("asset_id"):
            errors.append(f"第 {index + 1} 张参考图没有素材库 ID（参考图必须来自素材库）")
        if ref.get("missing"):
            errors.append(
                f"第 {index + 1} 张参考图对应的素材已从素材库删除"
                f"（{ref.get('name') or ref.get('asset_id')}），请移除后再生成")

    placeholders = parse_placeholders(prompt)
    if count >= 2:
        expected = list(range(1, count + 1))
        if not placeholders:
            errors.append(
                f"有 {count} 张参考图，提示词里必须有 <image1>..<image{count}> 占位标记"
                f"（官方 PE-I2I 多图模板要求用 <imageN> 引用每张图）")
        elif placeholders != expected:
            missing = sorted(set(expected) - set(placeholders))
            extra = sorted(set(placeholders) - set(expected))
            detail = []
            if missing:
                detail.append("缺少 " + "、".join(f"<image{m}>" for m in missing))
            if extra:
                detail.append("多出 " + "、".join(f"<image{m}>" for m in extra))
            errors.append(
                f"提示词里的占位标记与参考图数量不一致"
                f"（{count} 张参考图，应为 1..{count} 连续）：" + "，".join(detail))
    elif count == 1 and placeholders:
        warnings.append("只有 1 张参考图，官方 PE-I2I 模板要求单图不使用 <imageN> 标签")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


# ------------------------------------------------------------------ 智能排序


def score_refs(refs: list[dict], prompt: str = "", use_model_score: bool = True) -> list[dict]:
    """
    给每张参考图打分（分数越高越靠前）。

    信号（全部是**本地可算**的，不依赖模型）：
      1. 与提示词意图的匹配度：描述/标签里的词与提示词的词重叠
      2. 标签相关度：标签命中提示词关键词的权重更高
      3. 入库时间：新的略靠前（素材库常用的「最近用的优先」直觉）
      4. 有描述的比没描述的略靠前（描述完整 = 更好被 PE 理解）

    use_model_score 保留给将来接模型打分；**没有模型时必须能降级**，
    所以它默认开启也不影响 —— 真正的模型分由调用方在拿不到时置 False。
    """
    prompt_words = _tokenize_words(prompt)
    scored = []
    now = max([float(r.get("created_at") or 0) for r in refs] or [0.0])
    for index, ref in enumerate(refs):
        desc = (ref.get("description") or "")
        tags = [str(t) for t in (ref.get("tags") or [])]
        haystack = f"{desc} {' '.join(tags)} {ref.get('name') or ''}"
        words = set(_tokenize_words(haystack))

        prompt_set = set(prompt_words)
        overlap = 0.0
        if prompt_set:
            hit = words & prompt_set
            overlap = len(hit) / max(1, len(prompt_set))

        tag_hit = 0.0
        if prompt_set and tags:
            tag_words: set[str] = set()
            for tag in tags:
                tag_words |= set(_tokenize_words(tag))
            tag_hit = len(tag_words & prompt_set) / max(1, len(tag_words))

        # 入库时间：0..1 归一化（越新越接近 1）
        recency = 0.0
        if now > 0 and ref.get("created_at"):
            recency = min(1.0, max(0.0, float(ref["created_at"]) / now))

        has_desc = 1.0 if desc.strip() else 0.0

        score = (overlap * 3.0) + (tag_hit * 1.5) + (recency * 0.8) + (has_desc * 0.4)
        if use_model_score:
            # 目前没有接模型打分；真正的模型分由调用方塞进 ref["model_score"]
            score += float(ref.get("model_score") or 0.0)

        scored.append({
            "index": index,
            "ref": ref,
            "score": round(score, 6),
            "signals": {
                "prompt_overlap": round(overlap, 4),
                "tag_relevance": round(tag_hit, 4),
                "recency": round(recency, 4),
                "has_description": has_desc,
                "model_score": float(ref.get("model_score") or 0.0),
            },
        })
    return scored


def smart_sort(refs: list[dict], prompt: str = "", use_model: bool = True) -> dict:
    """
    智能排序 —— 返回建议顺序 + 排序依据（可解释）。

    **降级保证**：use_model=False 或模型不可用时，退化为确定性基础排序
    （分数相同时按入库时间倒序，再按原顺序），绝不抛异常。
    """
    try:
        scored = score_refs(refs, prompt=prompt, use_model_score=use_model)
    except Exception:
        # 任何意外都不能让「排序」这个可选功能把整个提交流程搞挂
        scored = [{"index": i, "ref": r, "score": 0.0, "signals": {}}
                  for i, r in enumerate(refs)]

    # 稳定排序：分数降序 -> 入库时间降序 -> 原下标升序
    # 排序键本身也要兜底：ref 可能是被改坏的对象（.get 抛异常），
    # 这时退回 0，不能让异常冒出去。
    def _sort_key(item):
        try:
            created = float((item["ref"] or {}).get("created_at") or 0)
        except Exception:
            created = 0.0
        try:
            score = float(item.get("score") or 0)
        except Exception:
            score = 0.0
        return (-score, -created, item["index"])

    try:
        scored.sort(key=_sort_key)
    except Exception:
        scored = [{"index": i, "ref": r, "score": 0.0, "signals": {}}
                  for i, r in enumerate(refs)]
    ordered = [item["ref"] for item in scored]
    return {
        "refs": ordered,
        "ranking": [{
            "from": item["index"],
            "to": position,
            "score": item["score"],
            "signals": item["signals"],
        } for position, item in enumerate(scored)],
        "degraded": not use_model,
    }


def basic_sort(refs: list[dict]) -> list[dict]:
    """确定性基础排序：入库时间倒序（没有入库时间则保持原顺序）。"""
    indexed = list(enumerate(refs))
    indexed.sort(key=lambda pair: (-float((pair[1] or {}).get("created_at") or 0), pair[0]))
    return [ref for _, ref in indexed]


def _tokenize_words(text: str) -> list[str]:
    """
    轻量分词：中文按 2-gram、英文/数字按单词。

    不引第三方依赖 —— 排序是个可选功能，不能因为缺 jieba 就挂掉。
    """
    text = (text or "").lower()
    words: list[str] = []
    # 英文/数字词
    for token in re.findall(r"[a-z0-9_]+", text):
        if len(token) > 1:
            words.append(token)
    # 中文 2-gram
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            words.append(run)
            continue
        for i in range(len(run) - 1):
            words.append(run[i:i + 2])
    return words
