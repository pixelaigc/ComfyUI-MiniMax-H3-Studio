# -*- coding: utf-8 -*-
"""H3 效果 LoRA 的**元数据目录**（作者写的说明 -> 界面展示用）。

【数据来源 —— 全是本机真实文件，不编造】
本机 loras 目录里每枚 H3 效果 LoRA 旁边都有一份同名 `.md` 说明文档
（作者/调研者写的），格式统一：

    # 标题
    ## 1. 功能与作用
    ## 4. 触发词
    ```
    3dsrx
    ```
    ## 5. 强度建议（作者给了分场景建议）
    | 场景 | 建议强度 | 原因 |
    | **T2V 文生视频** | **0.8** | ... |

本模块从这些 `.md` 里**解析**出：
  * `trigger`        —— 触发词
  * `recommended_strength` —— 推荐强度
  * `scene`          —— 功能说明
  * `author` / `source` —— 上游信息

**解析不出来就是空字符串** —— 绝不臆造触发词或推荐强度。

【三种触发词写法都要认】
本机这批文档的触发词有三种写法，必须都覆盖：

  1. 代码块：
     ```
     3dsrx
     ```
  2. 表格行：  | **触发词** | **`m1sa_b0dy`** |
  3. 明确说没有： "作者**未提供触发词**" / "⚠️ **无触发词。**"

第 3 种是**负向结论** -> 触发词留空（界面显示「上游未标注触发词」），
**不能**把「作者未提供触发词」这句话本身当成触发词塞进提示词。

【为什么不把这些写进 Python 常量】
因为本机这批 LoRA 的说明文档是**外部资产**（可以随权重更新）。
写死在代码里就会和文件漂移。直接读文件 = 永远一致。
"""

from __future__ import annotations

import re
from pathlib import Path

#: 「触发词」小节标题。
_TRIGGER_SECTION = re.compile(r"^#{2,4}\s*.*触发词.*$", re.MULTILINE)
#: 「强度建议」小节标题。
_STRENGTH_SECTION = re.compile(r"^#{2,4}\s*.*强度.*$", re.MULTILINE)
#: 「功能与作用」小节标题。
_SCENE_SECTION = re.compile(r"^#{2,4}\s*.*功能与作用.*$", re.MULTILINE)
#: 「来源与校验」小节标题。
_SOURCE_SECTION = re.compile(r"^#{2,4}\s*.*来源与校验.*$", re.MULTILINE)

#: 数值（含小数）。
#:
#: ⚠️ 排除「数字后面紧跟字母」的情况 —— `2D 动漫` 里的 2 不是强度，
#: `4K` / `H3` 同理。用 `(?![A-Za-z])` 挡掉。
_NUM = re.compile(r"(?<![\d.])(\d(?:\.\d+)?)(?![A-Za-z\d.])")

#: 带符号的数值（滑杆型 LoRA 的 `+3` / `-3`）。
_SIGNED_NUM = re.compile(r"(?<![\w.])([+-]\d(?:\.\d+)?)(?![A-Za-z\d.])")

#: 明确表示「没有触发词」的措辞 -> 命中即视为**负向结论**（不填触发词）。
_NO_TRIGGER_MARKERS = (
    "无触发词",
    "没有触发词",
    "未提供触发词",
    "未标注触发词",
    "不需要触发词",
    "无需触发词",
    "no trigger",
)

#: 「必须以 `xxx:` 开头」这种**前缀型**触发词 —— 硬性要求，不是普通词。
#: 命中时直接取前缀（`style_transfer:` / `vfx_edit:` / `head_swap:` …）。
_PREFIX_TRIGGER = re.compile(r"`([a-z_]+):`")

#: 已知前缀型触发词的家族（`minimax_h3_*` 系列）。
#: 这批 LoRA 的触发词不是独立词，而是「必须以某前缀开头」的硬性要求，
#: 在文档里以**对比表格**的形式列出（见 minimax_h3_style_transfer 的
#: 「作者自述的局限」表）。表格里找不到时按本清单兜底。
_KNOWN_PREFIX_TRIGGERS = {
    "minimax_h3_style_transfer_v1.0_r64.safetensors": "style_transfer:",
    "minimax_h3_vfx_edit_v1.0_r128.safetensors": "vfx_edit:",
    "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors": "vfx_edit:",
    "minimax_h3_head_swap_v1.0_r32.safetensors": "head_swap:",
    "minimax_h3_live_wallpaper_v1.0_ref2va_r32.safetensors": "live_wallpaper:",
    "minimax_h3_live_wallpaper_v1.0_ref2va_r64.safetensors": "live_wallpaper:",
    "minimax_h3_lms_v1.0_r64.safetensors": "lms:",
    "minimax_h3_character_swap_v1.0_ref2va_r64_gl.safetensors": "character_swap:",
}

#: 强度区间（`0.6~0.8` / `0.6-0.8` / `0.6–0.8`）。
_RANGE = re.compile(
    r"(?<![\d.])(\d(?:\.\d+)?)\s*[~\-–—]\s*(\d(?:\.\d+)?)(?![\d.])")


def _section(text: str, pattern: re.Pattern) -> str:
    """取某个小节的正文（到下一个同级标题为止）。"""
    match = pattern.search(text)
    if not match:
        return ""
    rest = text[match.end():]
    nxt = re.search(r"^#{1,4}\s", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _clean(text: str) -> str:
    """去掉 markdown 强调 / 反引号 / 粗体标记。"""
    return (text.replace("**", "").replace("`", "")
            .replace("__", "").strip())


def _looks_like_prose(text: str) -> bool:
    """这段文字像**说明句**而不是触发词？

    判据：中文标点 / 长句 / 含「作者」「必须」「开头」「组」等叙述词。
    触发词通常是短词或短语（`3dsrx` / `nsgirl` / `style_transfer:`）。
    """
    if not text:
        return True
    if len(text) > 60:
        return True
    if any(mark in text for mark in ("。", "，", "；", "：", "、", "（", "）")):
        return True
    if any(word in text for word in ("作者", "必须", "开头", "组完整",
                                     "写进", "这是", "这是唯一")):
        return True
    return False


def _parse_trigger(section: str) -> str:
    """从「触发词」小节里取触发词。取不到就返回空串。

    三种来源依次尝试：
      1. ``` 代码块 ```（作者原话，最可靠）
      2. 表格里「触发词」那一行的值
      3. 第一行非表格、非引用正文
    负向结论（"无触发词" / "未提供触发词"）一律返回空串。
    """
    if not section:
        return ""
    # --- 负向结论先判 ---
    for line in section.splitlines():
        stripped = line.strip()
        if any(marker in stripped for marker in _NO_TRIGGER_MARKERS):
            return ""

    # --- 1) 代码块 ---
    blocks = re.findall(r"```[^\n]*\n(.*?)```", section, re.DOTALL)
    # 只取**短**的代码块 —— 长段落是「完整指令」（作者写进 trainedWords 的
    # 长指令，不是短触发词）。多个短块 -> 用 " / " 连接。
    short_blocks = []
    for block in blocks:
        lines = [line.strip() for line in block.strip().splitlines()
                 if line.strip()]
        if not lines:
            continue
        cleaned_lines = [_clean(line) for line in lines
                         if _clean(line)
                         and not any(m in _clean(line)
                                     for m in _NO_TRIGGER_MARKERS)]
        if not cleaned_lines:
            continue
        joined = " ".join(cleaned_lines)
        # 太长的当���是「完整指令」而不是触发词 —— 跳过
        if len(joined) > 120:
            continue
        # 说明句（不是触发词）—— 跳过
        if _looks_like_prose(joined):
            continue
        short_blocks.append(joined)
    if short_blocks:
        return " / ".join(short_blocks[:4])

    # --- 2) 表格行 ---
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        key = _clean(cells[0])
        if key in ("触发词", "Trigger", "trigger", "trigger word"):
            value = _clean(cells[1])
            if value and not any(m in value for m in _NO_TRIGGER_MARKERS) \
                    and not _looks_like_prose(value):
                return value[:120]

    # --- 3) 第一行正文 ---
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", ">", "#", "---", "===")):
            continue
        cleaned = _clean(stripped)
        if not cleaned or any(m in cleaned for m in _NO_TRIGGER_MARKERS):
            continue
        if _looks_like_prose(cleaned):
            continue
        return cleaned
    return ""


def _parse_strength(section: str) -> tuple[float, str]:
    """从「强度建议」小节里取推荐强度 + 依据说明。

    返回 (strength, source_text)；strength<=0 表示没解析到。

    取值规则（保守，优先级从上到下）：
      1. 表格里**明确标「作者推荐」**的那一行
      2. 表格里的**区间**（`0.6~0.8` -> 取中值 0.7）
      3. 表格里第一个落在 (0, 2] 的数值
      4. 正文里第一个落在 (0, 2] 的数值
    """
    if not section:
        return 0.0, ""

    def _in_range(value: float) -> bool:
        return 0.0 < value <= 2.0

    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        rows.append(cells)

    # 1) 明确标「推荐」的那一行 —— 优先级最高
    for cells in rows:
        joined = _clean(" ".join(cells))
        if "推荐" not in joined:
            continue
        for cell in cells[1:]:
            rng = _RANGE.search(_clean(cell))
            if rng:
                lo, hi = float(rng.group(1)), float(rng.group(2))
                if _in_range(lo) and _in_range(hi):
                    return round((lo + hi) / 2, 3), joined[:160]
        for num in _SIGNED_NUM.findall(_clean(" ".join(cells))):
            value = float(num)
            if _in_range(abs(value)):
                return value, joined[:160]
        for num in _NUM.findall(_clean(" ".join(cells))):
            value = float(num)
            if _in_range(value):
                return value, joined[:160]

    # 2) 表格里的区间
    for cells in rows:
        for cell in cells[1:]:
            rng = _RANGE.search(_clean(cell))
            if rng:
                lo, hi = float(rng.group(1)), float(rng.group(2))
                if _in_range(lo) and _in_range(hi) and lo <= hi:
                    return round((lo + hi) / 2, 3), _clean(" ".join(cells))[:160]

    # 3) 表格里第一个数值
    for cells in rows:
        for cell in cells[1:]:
            for num in _NUM.findall(_clean(cell)):
                value = float(num)
                if _in_range(value):
                    return value, _clean(" ".join(cells))[:160]

    # 4) 正文第一个数值
    #    ⚠️ 正文里的百分比（`最大 1.540%`）不是强度 —— 排除带 % 的匹配。
    body = re.sub(r"```.*?```", "", section, flags=re.DOTALL)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", "#", "---", "===")):
            continue
        for match in _NUM.finditer(stripped):
            # 紧跟 % 的数值是扰动百分比，不是强度
            tail = stripped[match.end():match.end() + 1]
            if tail == "%":
                continue
            value = float(match.group(1))
            if _in_range(value):
                return value, stripped[:160]
    return 0.0, ""


def _parse_scene(section: str) -> str:
    """从「功能与作用」小节里取一句功能说明。"""
    if not section:
        return ""
    body = re.sub(r"```.*?```", "", section, flags=re.DOTALL)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("|", "#", "---", "===", ">")):
            continue
        cleaned = _clean(stripped)
        if len(cleaned) < 6:
            continue
        return cleaned[:180]
    return ""


def _parse_table_row(section: str, label: str) -> str:
    """从表格里取某一行（如「作者」）。"""
    if not section:
        return ""
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if _clean(cells[0]) == label:
            return _clean(cells[1])[:200]
    return ""


def _parse_source(section: str) -> str:
    """取上游链接。"""
    if not section:
        return ""
    urls = re.findall(r"https?://[^\s|)]+", section)
    for url in urls:
        if "civitai.com" in url or "huggingface.co" in url:
            return url
    return urls[0] if urls else ""


def read_lora_doc(path: Path) -> dict:
    """读一份 LoRA 的同名 `.md` 说明文档，解析出展示用元数据。

    返回固定形状（读不到就是空值）：
      {
        "display": "",            # 文档标题
        "scene": "",              # 功能说明
        "trigger": "",            # 触发词（无则空串）
        "recommended_strength": 0.0,
        "strength_source": "",    # 强度依据（原文）
        "author": "",
        "source": "",             # 上游链接
        "has_doc": bool,
      }
    """
    out = {
        "display": "",
        "scene": "",
        "trigger": "",
        "recommended_strength": 0.0,
        "strength_source": "",
        "author": "",
        "source": "",
        "has_doc": False,
    }
    doc = path.with_suffix(".md")
    if not doc.is_file():
        return out
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    out["has_doc"] = True

    title = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    if title:
        out["display"] = _clean(title.group(1))

    out["scene"] = _parse_scene(_section(text, _SCENE_SECTION))
    trigger = _parse_trigger(_section(text, _TRIGGER_SECTION))
    if not trigger:
        # 前缀型触发词（`style_transfer:` 等）—— 文档里是「必须以 X 开头」
        # 的措辞，或只出现在对比表里。
        trigger = _parse_prefix_trigger(text)
    if not trigger:
        trigger = _KNOWN_PREFIX_TRIGGERS.get(path.name, "")
    out["trigger"] = trigger
    strength, basis = _parse_strength(_section(text, _STRENGTH_SECTION))
    out["recommended_strength"] = strength
    out["strength_source"] = basis
    source_section = _section(text, _SOURCE_SECTION)
    out["author"] = _parse_table_row(source_section, "作者")
    out["source"] = _parse_source(source_section)
    return out


def _parse_prefix_trigger(text: str) -> str:
    """找「必须以 `xxx:` 开头」这种前缀型触发词。

    只认明确带「必须 / 硬性 / 要求 / 开头」语境的匹配 ——
    避免把文档里随便提到的 `vfx_edit:` 也当成触发词。
    """
    for line in text.splitlines():
        if not any(kw in line for kw in ("必须", "硬性", "开头", "要求")):
            continue
        match = _PREFIX_TRIGGER.search(line)
        if match:
            return match.group(1) + ":"
    return ""
