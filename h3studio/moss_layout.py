# -*- coding: utf-8 -*-
"""
MOSS-Image 结构化（layout）提示词增强 —— 外部链路实现。

【为什么在外部做，而不是直接用 ComfyUI-Moss 的 MossPrompt 节点】
官方插件的 MossPrompt 节点本身要连一个 OpenAI Chat Completions 兼容的**外部 API**
（默认 api.deepseek.com），才能把自然语言扩写成 MOSS layout JSON。
用户明确决定不接入该节点（会把 MOSS 工作流与外部 API 耦合）。
本模块的做法是：**复用本项目已有的「本地模型 Provider 配置 + prompt-enhancer
调用封装」**，把 MossPrompt 的能力在外部复现出来 —— 本地模型出草稿，
本地代码做校验与编译，产物是可读可编辑的 layout JSON + 最终文本提示词。

【来源与取舍（全部来自本机已安装的源码，不是臆造）】
来源插件：/home/ubuntu/comfyui/custom_nodes/ComfyUI-Moss
  - nodes.py      MossPrompt 节点的输入定义
  - engine.py     API 调用、草稿解析、编译、缓存、失败行为
  - config.py     API/Skill/脚本配置与校验
  - defaults/SKILL.md    官方 MOSS Skill（文生图提示词写法）
  - defaults/layout.md   layout 草稿 schema（字段的权威定义）
  - scripts/compile_prompt.py  草稿 -> MOSS 提示词的编译规则

官方插件的两种输出形态：
  natural     —— {"caption": "..."}，编译后就是一段纯文本提示词
  layer json  —— 完整 layout 草稿，编译后是一行 MOSS Layout JSON
                （bbox_coord_space=abs_centralized, bbox_format=yxyx）

【本项目的取舍】
  1. 官方插件把「草稿」（abs/xyxy，左上原点）与「编译后」（abs_centralized/yxyx，
     画布中心原点）分成两个阶段，坐标转换由本地脚本做。
     **本模块保留同样的两段式**：模型只出草稿，坐标转换与校验由本模块的
     compile_layout() 做 —— 与官方 compile_prompt.py 的算法逐条对齐。
  2. 官方插件的 layer json 产物**就是**送进 MOSS 的提示词本体（模型能直接读
     Layout JSON）。但本机 ComfyUI 0.37.0 的 MOSS 工作流里只有普通
     CLIPTextEncode，用户没有把 layout JSON 直接接进出图链路。
     因此本模块**两个产物都给**：
       - layout_json：可复制的官方 MOSS Layout JSON（喂给支持 layout 的下游）
       - final_prompt：由 layout JSON **确定性翻译**出的自然语言提示词
         （对象清单、位置关系、属性、材质、镜头/光照逐条落到自然语言），
         这是送进 CLIPTextEncode 的那一份。
     两者同源：final_prompt 一定与 layout_json 描述同一张图。
  3. 不引入任何对 OpenAI 官方 API 的硬依赖 —— 模型调用一律走本项目的
     provider 配置（h3studio/providers.py + h3studio/llm.py）。

【失败与回退（绝不静默失败）】
  - provider 不可达 / 超时     -> ProviderError，接口返回 502 并带可读原因
  - 模型返回空                 -> 502「模型返回空内容」
  - 模型返回的不是合法 JSON     -> 502 + 具体原因（哪一行/哪个字段）
  - JSON 合法但不符合 layout schema -> 502 + 逐条 schema 错误
  以上全部**不生成**任何 layout 产物；前端会明确报错，用户可以退回原提示词。
"""
import json
import math
import re

# ------------------------------------------------------------------ 常量

#: layout 草稿的根字段（与官方 defaults/layout.md 完全一致：恰好这六个）
LAYOUT_ROOT_FIELDS = {"width", "height", "bbox_coord_space", "bbox_format",
                      "summary", "items"}
LAYOUT_ROOT_REQUIRED = LAYOUT_ROOT_FIELDS

#: summary 的字段
SUMMARY_FIELDS = {"caption", "meta", "canvas", "palette"}
SUMMARY_REQUIRED = {"caption"}

#: item 的字段
ITEM_FIELDS = {"label", "caption", "bbox", "text", "tags", "palette"}
ITEM_REQUIRED = {"label", "caption", "bbox"}

#: item.label 的合法取值
ITEM_LABELS = ("object", "character", "text")

#: summary.canvas 的合法取值
CANVAS_KINDS = ("single", "multi_panel", "collage")

#: 画布宽高必须是 16 的倍数（官方 compile_prompt.py 的硬规则）
SIZE_MULTIPLE = 16


class LayoutError(ValueError):
    """layout 校验失败。message 面向用户，直接可读。"""

    def __init__(self, message: str, errors: list | None = None):
        super().__init__(message)
        self.errors = list(errors or [message])


# ------------------------------------------------------------------ 基础校验工具


def _is_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _nonempty_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value) -> bool:
    if not isinstance(value, list):
        return False
    return all(_nonempty_text(item) for item in value)


# ------------------------------------------------------------------ 草稿解析


def parse_draft(raw: str) -> dict:
    """
    把模型返回的原始文本解析成 layout 草稿 dict。

    与官方 engine.parse_draft 的行为对齐：
      - 允许 ```json ... ``` 代码围栏
      - 允许前后带说明文字？**不允许** —— 官方是 json.loads 全文，
        只有 natural 模式才把非 JSON 文本当 caption。这里是 layout 模式，
        所以解析失败必须报错，不做「尽力修复」（宁可报错也不要产出一份
        字段半残的 layout 冒充合法产物）。
      - 拒绝重复的 JSON 字段（官方用 object_pairs_hook=unique_object）
    """
    if not isinstance(raw, str) or not raw.strip():
        raise LayoutError("模型返回了空内容，未生成 layout 草稿")

    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n([\s\S]*?)\n```", text, flags=re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()

    def _unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复的 JSON 字段：{key}")
            result[key] = value
        return result

    def _reject_constant(value):
        raise ValueError(f"非法 JSON 数值：{value}")

    try:
        draft = json.loads(text, object_pairs_hook=_unique, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        raise LayoutError(
            f"模型未返回有效的 JSON（第 {error.lineno} 行第 {error.colno} 列）：{error.msg}",
            [f"JSON 解析失败：第 {error.lineno} 行第 {error.colno} 列 {error.msg}"],
        ) from None
    except ValueError as error:
        raise LayoutError(f"模型返回的 JSON 不合法：{error}") from None

    if not isinstance(draft, dict):
        raise LayoutError("模型返回的 layout 草稿必须是 JSON 对象")
    return draft


# ------------------------------------------------------------------ schema 校验


def validate_layout(draft: dict, width: int | None = None,
                    height: int | None = None) -> dict:
    """
    校验 layout 草稿是否符合官方 schema。

    返回 {"ok": bool, "errors": [...]}。
    errors 是给人看的短句列表 —— 逐条说明哪里不合法、为什么。

    width/height 传入时，额外要求草稿里的画布尺寸与之一致
    （官方 engine.parse_draft 就是这么做的：模型草稿的画布必须等于节点分辨率）。
    """
    errors: list[str] = []
    if not isinstance(draft, dict):
        return {"ok": False, "errors": ["layout 草稿必须是 JSON 对象"]}

    # ---- 根字段：恰好这六个，多一个少一个都不行
    keys = set(draft.keys())
    unknown = keys - LAYOUT_ROOT_FIELDS
    missing = LAYOUT_ROOT_REQUIRED - keys
    if unknown:
        errors.append(f"根对象含未知字段：{'、'.join(sorted(unknown))}")
    if missing:
        errors.append(f"根对象缺少字段：{'、'.join(sorted(missing))}")

    # ---- 画布尺寸
    for name in ("width", "height"):
        value = draft.get(name)
        if value is None:
            continue
        if type(value) is not int:
            errors.append(f"{name} 必须是整数（当前 {type(value).__name__}）")
        elif value <= 0:
            errors.append(f"{name} 必须是正整数（当前 {value}）")
        elif value % SIZE_MULTIPLE:
            errors.append(f"{name} 必须是 {SIZE_MULTIPLE} 的倍数（当前 {value}）")

    draft_w, draft_h = draft.get("width"), draft.get("height")
    if width is not None and draft_w is not None and draft_w != width:
        errors.append(f"画布宽度与目标分辨率不一致（草稿 {draft_w}，目标 {width}）")
    if height is not None and draft_h is not None and draft_h != height:
        errors.append(f"画布高度与目标分辨率不一致（草稿 {draft_h}，目标 {height}）")

    # ---- 坐标空间与格式
    if draft.get("bbox_coord_space") != "abs":
        errors.append("bbox_coord_space 必须是 \"abs\"（左上原点绝对像素）")
    if draft.get("bbox_format") != "xyxy":
        errors.append("bbox_format 必须是 \"xyxy\"（[x_min,y_min,x_max,y_max]）")

    # ---- summary
    summary = draft.get("summary")
    if summary is None:
        errors.append("缺少 summary 对象")
    elif not isinstance(summary, dict):
        errors.append("summary 必须是对象")
    else:
        s_keys = set(summary.keys())
        s_unknown = s_keys - SUMMARY_FIELDS
        s_missing = SUMMARY_REQUIRED - s_keys
        if s_unknown:
            errors.append(f"summary 含未知字段：{'、'.join(sorted(s_unknown))}")
        if s_missing:
            errors.append(f"summary 缺少字段：{'、'.join(sorted(s_missing))}")
        if "caption" in summary and not _nonempty_text(summary["caption"]):
            errors.append("summary.caption 必须是非空文本")
        if "canvas" in summary and summary["canvas"] not in CANVAS_KINDS:
            errors.append(
                "summary.canvas 只能是 single、multi_panel 或 collage"
                f"（当前 {summary['canvas']!r}）")
        for key in ("meta", "palette"):
            if key in summary and not _string_list(summary[key]):
                errors.append(f"summary.{key} 必须是非空字符串组成的数组")

    # ---- items
    items = draft.get("items")
    if items is None:
        errors.append("缺少 items 数组")
    elif not isinstance(items, list):
        errors.append("items 必须是数组")
    elif not items:
        errors.append("items 必须是非空数组")
    else:
        for index, item in enumerate(items):
            errors.extend(_validate_item(item, index, draft_w, draft_h))

    return {"ok": not errors, "errors": errors}


def _validate_item(item, index: int, width, height) -> list:
    """单个 item 的校验。返回错误列表。"""
    where = f"items[{index}]"
    errors: list[str] = []
    if not isinstance(item, dict):
        return [f"{where} 必须是对象"]

    i_keys = set(item.keys())
    i_unknown = i_keys - ITEM_FIELDS
    i_missing = ITEM_REQUIRED - i_keys
    if i_unknown:
        errors.append(f"{where} 含未知字段：{'、'.join(sorted(i_unknown))}")
    if i_missing:
        errors.append(f"{where} 缺少字段：{'、'.join(sorted(i_missing))}")

    label = item.get("label")
    if label is not None and label not in ITEM_LABELS:
        errors.append(
            f"{where}.label 只能是 object、character 或 text（当前 {label!r}）")
    if "caption" in item and not _nonempty_text(item["caption"]):
        errors.append(f"{where}.caption 必须是非空文本")
    if "text" in item and not _nonempty_text(item["text"]):
        errors.append(f"{where}.text 必须是非空文本")
    if label == "text" and "text" not in item:
        errors.append(f"{where} 是 text 类型，必须带 text 字段（图内文字逐字内容）")
    for key in ("tags", "palette"):
        if key in item and not _string_list(item[key]):
            errors.append(f"{where}.{key} 必须是非空字符串组成的数组")

    if "bbox" in item:
        errors.extend(_validate_bbox(item["bbox"], where, width, height))
    return errors


def _validate_bbox(bbox, where: str, width, height) -> list:
    """bbox 必须是 [x_min,y_min,x_max,y_max]，四个有限数，在画布内。"""
    errors: list[str] = []
    if not isinstance(bbox, list) or len(bbox) != 4:
        return [f"{where}.bbox 必须是 [x_min,y_min,x_max,y_max] 四个数"]
    if not all(_is_number(v) for v in bbox):
        return [f"{where}.bbox 的四个坐标都必须是有限数字"]
    x1, y1, x2, y2 = bbox
    if not (x1 < x2 and y1 < y2):
        errors.append(f"{where}.bbox 必须有正的宽和高（当前 {bbox}）")
        return errors
    if _is_number(width) and _is_number(height):
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            errors.append(f"{where}.bbox 超出 {width}×{height} 画布范围")
    return errors


# ------------------------------------------------------------------ 坐标编译
#  与官方 scripts/compile_prompt.py 的 centered_box() 逐条对齐：
#    [floor(y1)-h//2, floor(x1)-w//2, ceil(y2)-h//2, ceil(x2)-w//2]
#  （先 round 到 9 位小数消数值噪声，再 floor/ceil 包住像素边）


def centered_box(bbox: list, width: int, height: int) -> list:
    """左上原点 xyxy -> 画布中心原点 yxyx（官方算法）。"""
    x1, y1, x2, y2 = bbox
    return [
        math.floor(round(y1, 9)) - height // 2,
        math.floor(round(x1, 9)) - width // 2,
        math.ceil(round(y2, 9)) - height // 2,
        math.ceil(round(x2, 9)) - width // 2,
    ]


def compile_layout(draft: dict) -> dict:
    """
    校验 + 编译 layout 草稿。

    成功返回**编译后**的 MOSS Layout dict：
      {"summary": ..., "items": [...],
       "bbox_coord_space": "abs_centralized", "bbox_format": "yxyx"}
    编译后不含 width/height（官方 compile_prompt.py 就是这样 —— 画布尺寸
    留在外面，跟出图尺寸对齐）。

    失败抛 LayoutError（带逐条可读原因）。
    """
    result = validate_layout(draft)
    if not result["ok"]:
        raise LayoutError("layout 草稿不符合 schema：" + "；".join(result["errors"]),
                          result["errors"])
    width = draft["width"]
    height = draft["height"]
    compiled_items = []
    for item in draft["items"]:
        compiled = {}
        for key, value in item.items():
            compiled[key] = centered_box(value, width, height) if key == "bbox" else value
        compiled_items.append(compiled)
    return {
        "summary": draft["summary"],
        "items": compiled_items,
        "bbox_coord_space": "abs_centralized",
        "bbox_format": "yxyx",
    }


# ------------------------------------------------------------------ layout -> 文本
#  确定性翻译：同一份 layout JSON 永远得到同一段文本提示词。
#  不调用任何模型 —— 文字全从 layout 的字段里来。

#: 方位/尺度/类型短语的**中英双份**。
#:
#: 为什么要有两份：官方 Skill 写的是「Default to English descriptive prose
#: unless the user requests another language」。但模型实际返回的 caption
#: 语言**跟着用户需求走**（中文需求 -> 中文 caption）。
#: 如果翻译层固定输出英文方位短语，就会得到「中文 caption + 英文方位」的
#: 混合语言提示词 —— 对 CLIPTextEncode 来说是两套词表，效果打折。
#: 所以这里按 caption 的实际语言选同一套短语，保证整段提示词语言一致。
_PHRASES = {
    "en": {
        "region_x": ["on the left side", "in the horizontal center", "on the right side"],
        "region_y": ["in the upper area", "in the vertical center", "in the lower area"],
        "size": ["filling most of the frame", "occupying a large part of the frame",
                 "of moderate size", "small within the frame"],
        "label": {"object": "object", "character": "character", "text": "text element"},
        "lead": "A {kind} {region}, {size}",
        "wording": 'the exact visible wording is "{text}"',
        "keywords": "keywords: {values}",
        "colours": "colours: {values}",
        "palette": "Overall colour palette: {values}.",
        "medium": "Medium and format: {values}.",
        "multi_panel": "The image is composed as multiple panels.",
        "collage": "The image is composed as a collage.",
    },
    "zh": {
        "region_x": ["位于画面左侧", "位于画面水平中央", "位于画面右侧"],
        "region_y": ["处于画面上部", "处于画面垂直中央", "处于画面下部"],
        "size": ["占据画面大部分", "占据画面较大区域", "尺寸适中", "在画面中较小"],
        "label": {"object": "物件", "character": "人物", "text": "文字元素"},
        "lead": "{kind}{region}，{size}",
        "wording": "图内文字逐字为「{text}」",
        "keywords": "关键词：{values}",
        "colours": "配色：{values}",
        "palette": "整体配色：{values}。",
        "medium": "媒介与形式：{values}。",
        "multi_panel": "画面由多个分栏组成。",
        "collage": "画面为拼贴构图。",
    },
}


def _detect_language(text: str) -> str:
    """
    判断 caption 的**主要语言**（zh / en）。

    【为什么不能用「含不含 CJK 字符」】
    英文 caption 里出现图内文字（"月光 MOSS"）就会误判成中文 ——
    实测踩过：英文描述 + 中文标题 -> 整段提示词被切成「英文正文 + 中文方位短语」，
    对 CLIPTextEncode 来说是两套词表，效果打折。

    所以按**字符占比**判定：CJK 字符占比 >= 20% 才算中文。
    20% 这个阈值：短 caption 里嵌一两个中文词（占比会很低）仍判英文；
    真正的中文 caption（CJK 占比 > 50%）判中文。
    """
    text = text or ""
    if not text.strip():
        return "en"
    cjk = 0
    latin = 0
    for ch in text:
        if "一" <= ch <= "鿿":
            cjk += 1
        elif ch.isascii() and ch.isalpha():
            latin += 1
    total = cjk + latin
    if total == 0:
        return "en"
    return "zh" if (cjk / total) >= 0.2 else "en"


def _region_phrase(cx: float, cy: float, width: int, height: int, lang: str) -> str:
    """把框中心点翻译成方位短语。"""
    p = _PHRASES[lang]
    fx = cx / width if width else 0.5
    fy = cy / height if height else 0.5
    if fx < 0.34:
        xi = 0
    elif fx > 0.66:
        xi = 2
    else:
        xi = 1
    if fy < 0.34:
        yi = 0
    elif fy > 0.66:
        yi = 2
    else:
        yi = 1
    if lang == "zh":
        return f"{p['region_x'][xi]}，{p['region_y'][yi]}"
    return f"{p['region_x'][xi]}, {p['region_y'][yi]}"


def _size_phrase(bw: float, bh: float, width: int, height: int, lang: str) -> str:
    """按框占画布的比例给出尺度短语。"""
    p = _PHRASES[lang]
    area = (bw * bh) / float(width * height) if width and height else 0
    if area >= 0.35:
        idx = 0
    elif area >= 0.15:
        idx = 1
    elif area >= 0.04:
        idx = 2
    else:
        idx = 3
    return p["size"][idx]


def layout_to_text(compiled: dict, width: int, height: int) -> str:
    """
    由**编译后**的 layout JSON 确定性翻译出自然语言提示词。

    翻译规则（逐条可复现，不含模型随机性）：
      1. 第一段 = summary.caption（整体构图与风格，来自模型草稿）
      2. 其次逐个 item，按 **编译后的 yxyx 坐标**反算中心点与尺寸，
         落成「方位 + 尺度 + 类型 + 外观 + 文字」的自然语言短语
      3. text 类型额外把图内文字原样带出（一字不差，双引号/书名号包裹）
      4. item 的 tags / palette 以补充描述的形式附在后面
      5. summary.meta / summary.palette 作为整体媒介与配色说明追加在末尾

    为什么用编译后的坐标反算：草稿是左上原点，编译后是中心原点，
    而**真正送进出图的是编译后的那一份** —— 位置描述必须与它一致，
    否则文本提示词说的位置和 layout JSON 说的位置会对不上。

    语言：整段提示词跟随 summary.caption 的语言（中文 caption -> 中文方位短语），
    避免出现「中文 caption + 英文方位」的混合语言提示词。
    图内文字（item.text）**始终**保持原语言原样，不翻译。
    """
    summary = compiled.get("summary") or {}
    items = compiled.get("items") or []
    lines: list[str] = []

    caption = str(summary.get("caption") or "").strip()
    if caption:
        lines.append(caption)

    lang = _detect_language(caption)
    p = _PHRASES[lang]

    # ---- 整体媒介 / 配色
    meta = summary.get("meta") or []
    palette = summary.get("palette") or []
    canvas = summary.get("canvas") or ""
    if canvas == "multi_panel":
        lines.append(p["multi_panel"])
    elif canvas == "collage":
        lines.append(p["collage"])
    if meta:
        lines.append(p["medium"].format(values=", ".join(str(m) for m in meta)))
    if palette:
        lines.append(p["palette"].format(values=", ".join(str(x) for x in palette)))

    # ---- 逐个元素
    for item in items:
        label = str(item.get("label") or "object")
        item_caption = str(item.get("caption") or "").strip()
        bbox = item.get("bbox") or []
        if len(bbox) != 4:
            continue
        y1, x1, y2, x2 = bbox
        bw = float(x2 - x1)
        bh = float(y2 - y1)
        cx = float(x1 + x2) / 2 + width / 2
        cy = float(y1 + y2) / 2 + height / 2
        region = _region_phrase(cx, cy, width, height, lang)
        size = _size_phrase(bw, bh, width, height, lang)
        kind = p["label"].get(label, p["label"]["object"])

        parts = [p["lead"].format(kind=kind, region=region, size=size)]
        if item_caption:
            parts.append(item_caption.rstrip("."))
        if label == "text" and item.get("text"):
            # 图内文字原样带出（官方 Skill 要求「保留逐字可见文字」）
            parts.append(p["wording"].format(text=item["text"]))
        tags = item.get("tags") or []
        if tags:
            parts.append(p["keywords"].format(values=", ".join(str(t) for t in tags)))
        item_palette = item.get("palette") or []
        if item_palette:
            parts.append(p["colours"].format(values=", ".join(str(x) for x in item_palette)))
        # 元素之间用分号连接（中文用「；」）—— 保持整段提示词语言一致
        joiner = "；" if lang == "zh" else "; "
        lines.append(joiner.join(parts) + ("。" if lang == "zh" else "."))

    if not lines:
        raise LayoutError("layout 里没有任何可翻译的内容（summary.caption 为空且没有元素）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 整链路


def build_messages(original: str, width: int, height: int,
                   intent: str = "") -> list:
    """
    构造发给本地模型的消息。

    system 段把「官方 MOSS Skill 的写画规则」与「官方 layout 草稿 schema」
    合到一起 —— 两者都来自 ComfyUI-Moss 的 defaults/，不是自创的规则。
    """
    system = _LAYOUT_SYSTEM_PROMPT.format(width=width, height=height)
    user_parts = []
    if intent:
        user_parts.append(f"画面意图（用户补充说明）：{intent}")
    user_parts.append(f"画布尺寸：{width}×{height} 像素（宽高都必须是 {SIZE_MULTIPLE} 的倍数）。")
    user_parts.append(f"用户需求：{original}")
    user_parts.append(
        "请只输出一个 JSON 对象（不要 Markdown 围栏、不要解释、不要多余文字），"
        "字段严格按下述 schema。"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_parts)},
    ]


_LAYOUT_SYSTEM_PROMPT = """You are the layout drafting stage of an image pipeline that targets MOSS-Image.

Your single job: turn the user's image request into an uncompiled MOSS-Image Layout draft.
The runtime validates the draft, converts the coordinates, and delivers the final prompt.
Do not call tools. Do not claim to run scripts. Do not generate an image.
Reply ONLY with the JSON draft, without Markdown fences or commentary.

## Scope
Use only the current user message as the creative brief. There is no conversation history.
Do not invent earlier references, stored memories or project content.

## Preserve the brief
Preserve subjects, counts, relationships, medium, style, exclusions and the intended visible wording.
Resolve ordinary creative gaps yourself; do not invent factual event logistics or specifications.
Default to English descriptive prose unless the user requests another language.
Preserve visible text verbatim in its original language, including punctuation, capitalization
and line breaks. Keep the style of each item's caption inside that caption, never inside text.

## Develop the image
Describe visible choices: subject and action, relevant appearance, setting, camera and framing,
medium, lighting, materials, palette and mood. Add detail that makes the image more specific.
Avoid redundant quality tags, arbitrary word budgets, unrequested style changes and
invisible process notes. Keep a minimalist brief minimalist.

## Layout draft schema
The root object has exactly these six fields:
- "width": positive integer, divisible by 16. Must be {width}.
- "height": positive integer, divisible by 16. Must be {height}.
- "bbox_coord_space": the literal string "abs".
- "bbox_format": the literal string "xyxy".
- "summary": an object describing the complete composition.
- "items": a nonempty flat array of visible units.

"summary" requires a nonempty "caption". Optional fields are "meta" and "palette"
(arrays of nonempty strings) and "canvas" ("single", "multi_panel" or "collage").

The "summary.caption" is the global description of the whole scene. It MUST cover,
in prose: the subject and action, the setting, the camera and framing (shot type,
viewpoint, depth of field), the medium, the lighting (direction, quality, mood),
the materials, the colour palette and the overall mood. Every element that is not
a boxed item is described here — the camera and the lighting live here, not in
the items.

Each item requires:
- "label": "object", "character" or "text".
- "caption": nonempty text describing how that unit looks, including its
  appearance, material, colour and any text-rendering detail (font style,
  weight, size relative to the canvas).
- "bbox": [x_min, y_min, x_max, y_max] in pixels from the canvas top-left, inside
  0..width and 0..height. Both dimensions must be positive.
Text items also require a nonempty "text" containing the exact visible wording.
Optional item fields are "tags" and "palette" (arrays of nonempty strings).
No other fields, no nested children, no normalized boxes, no guessed font fields.
Express font appearance in "caption" and visual grouping through placement.

Boxes are placement annotations, not instructions to draw a frame, panel or sticker.
Natural overlap and occlusion are allowed. Never normalize, center, reorder or quantize boxes.
Put literal visible strings in text items' "text" fields and keep explanations of JSON,
boxes and conversion steps out of image-description fields.

Write every caption and the summary caption in the same language as the user's
request. Keep the visible wording in its own original language, verbatim."""


def enhance(original: str, raw_model_output: str, width: int, height: int,
            intent: str = "") -> dict:
    """
    完整链路：模型原始输出 -> 校验后的 layout + 最终文本提示词。

    失败一律抛 LayoutError（带可读原因），**绝不返回半成品**。
    成功返回：
      {
        "draft":       草稿（原样，abs/xyxy，左上原点）
        "layout":      编译后的 MOSS Layout（abs_centralized/yxyx）
        "layout_json": 编译后 layout 的紧凑 JSON 文本（可直接粘进支持 layout 的下游）
        "final_prompt":由 layout 确定性翻译出的自然语言提示词（送 CLIPTextEncode）
        "width": 画布宽, "height": 画布高
      }
    """
    if not _nonempty_text(original):
        raise LayoutError("请先输入要增强的提示词")
    if width <= 0 or height <= 0:
        raise LayoutError("画布宽高必须是正整数")
    if width % SIZE_MULTIPLE or height % SIZE_MULTIPLE:
        raise LayoutError(
            f"画布宽高必须是 {SIZE_MULTIPLE} 的倍数（当前 {width}×{height}）")

    draft = parse_draft(raw_model_output)
    result = validate_layout(draft, width=width, height=height)
    if not result["ok"]:
        raise LayoutError("layout 草稿不符合 schema：" + "；".join(result["errors"]),
                          result["errors"])

    compiled = compile_layout(draft)
    final_prompt = layout_to_text(compiled, width, height)
    return {
        "draft": draft,
        "layout": compiled,
        "layout_json": json.dumps(compiled, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False),
        "final_prompt": final_prompt,
        "width": width,
        "height": height,
    }
