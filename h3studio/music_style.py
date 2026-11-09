# -*- coding: utf-8 -*-
"""
YuE2 结构化风格描述 —— 对齐第三方 `YuE2StylePrompt` 节点的语义。

【为什么要它】
第三方链路（nonebot_plugin_novelai/h3_yue2.py）不是让用户直接写一整段英文
style，而是用 `YuE2StylePrompt` 节点把 **12 个结构化字段** 拼成 style 串
（custom_nodes/ComfyUI-YuE2/nodes.py:184-212 的 build_style_prompt）。
那套字段是实跑验证过出活效果的，我们之前只支持「一整段自由文本」，
表达力弱于第三方。

【关键语义（全部来自第三方节点源码，不是我们发明的）】
1. **「括号即省略」**：`_omit(value)`（nodes.py:181-182）—— 值以 `(` 开头
   就把该段整段丢弃。所以枚举里的 `(none)` / `(custom / none)` /
   `(instrumental / no lead vocal)` 不是字面量，是「不要这一段」。
2. **拼接顺序固定**（nodes.py:184-212）：
   language → genre → mood → (vocal_color + vocal) → instruments →
   "{BPM} BPM" → energy → length_hint 的英文描述 → extra
3. **length_hint 是文本映射不是参数**（nodes.py:173）：
   model decides        -> ""
   short clip (~45-75s) -> "short song, about one minute"
   single edit (~2-3 min) -> "radio-length song, about two to three minutes"
   full song (~3.5-5 min) -> "full-length song, about four minutes"
   very long (may truncate) -> "long-form song with extended sections"
4. **纯器乐必须 vocal + vocal_color 成对置 `(none)`**（yue2_schema.py:397-402），
   否则会拼出 "warm instrumental"，模型可能自己加唱。
5. **tempo_bpm=0 不写进 style 串**（nodes.py:196-197）。
6. **枚举值以运行时 ComfyUI /object_info 为准**（yue2_schema.py:106-134，
   TTL 15 分钟），失败回退静态副本 —— 绝不二次手抄。

【我们做什么、保留什么】
* 新增：结构化风格字段（12 个），可单独使用，也可与自由文本 style 合并。
* 保留：面板的自由文本风格描述框（那是我们的交互，第三方是聊天里选枚举）。
* 拼接：**沿用第三方节点的 build_style_prompt 逐字逻辑**，不自己发明。
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

# ------------------------------------------------------------------
# 枚举（静态兜底副本；权威来源是运行中的 ComfyUI /object_info）
# 抄自 ComfyUI-YuE2/nodes.py:167-180（第三方 yue2_schema.py:59-68 的 STATIC_ENUMS 同源）
# ------------------------------------------------------------------
STATIC_ENUMS = {
    "language": [
        "English", "Mandarin", "Cantonese", "Japanese", "Korean", "Spanish",
        "French", "Portuguese", "German", "Italian", "Hindi",
        "(custom / none)",
    ],
    "genre": [
        "pop", "piano pop", "indie pop", "synth pop", "dance pop", "r&b", "soul",
        "funk", "nu-disco", "disco", "house", "edm", "hip hop", "trap", "rap",
        "rock", "indie rock", "alternative rock", "punk", "metal", "jazz",
        "jazz-funk", "bossa nova", "latin", "reggaeton", "country", "folk",
        "indie folk", "singer-songwriter", "acoustic", "gospel", "blues",
        "lo-fi", "dream pop", "shoegaze", "ambient", "cinematic", "film score",
        "classical crossover", "k-pop", "j-pop", "city pop", "c-pop",
        "afrobeats", "reggae", "ska", "(custom / none)",
    ],
    "mood": [
        "(none)", "uplifting", "euphoric", "romantic", "intimate", "melancholic",
        "bittersweet", "dark", "aggressive", "playful", "nostalgic", "dreamy",
        "cinematic", "chill", "anthemic", "mysterious",
    ],
    "vocal": [
        "(instrumental / no lead vocal)", "female vocal", "male vocal",
        "androgynous vocal", "duet male and female", "choir / stacked vocals",
        "rap lead", "spoken word",
    ],
    "vocal_color": [
        "(none)", "warm", "bright", "breathy", "powerful", "raspy",
        "soft intimate", "belting", "tenor", "baritone", "soprano", "alto",
    ],
    "energy": [
        "(none)", "low energy", "mid energy", "high energy",
        "builds from quiet to loud",
    ],
    "length_hint": [
        "model decides", "short clip (~45-75s)", "single edit (~2-3 min)",
        "full song (~3.5-5 min)", "very long (may truncate)",
    ],
}

#: length_hint -> 写进 style 串的英文描述（第三方 nodes.py:173）
LENGTH_HINT_TEXT = {
    "model decides": "",
    "short clip (~45-75s)": "short song, about one minute",
    "single edit (~2-3 min)": "radio-length song, about two to three minutes",
    "full song (~3.5-5 min)": "full-length song, about four minutes",
    "very long (may truncate)": "long-form song with extended sections",
}

#: 歌词段落骨架模板 —— 抄自第三方 ComfyUI-YuE2/nodes.py:174-179
#:
#: 【第三方语义】`YuE2LyricsTemplate.build()`（nodes.py:281-283）逻辑是
#: **lyrics_override 非空就完全忽略 template**；为空才用 template 的骨架。
#: 我们的面板有自由歌词框，所以模板只作为「歌词为空时的兜底骨架」。
LYRIC_TEMPLATES = {
    "blank": "",
    "verse + chorus": "[Verse]\nLine one of the verse\nLine two of the verse\n\n[Chorus]\nSing the hook here\nRepeat the title line",
    "V-C-V-C": "[Verse]\nFirst verse line\n\n[Chorus]\nMain hook\n\n[Verse]\nSecond verse line\n\n[Chorus]\nMain hook",
    "V-C-V-C-bridge-C": "[Verse]\nFirst verse\n\n[Chorus]\nHook\n\n[Verse]\nSecond verse\n\n[Chorus]\nHook\n\n[Bridge]\nContrast the chorus here\n\n[Chorus]\nFinal hook",
}

#: 字段顺序 = ComfyUI YuE2StylePrompt 的 input_order（第三方 yue2_schema.py:35-39）
STYLE_FIELDS = [
    "language", "genre", "mood", "vocal", "vocal_color", "instruments",
    "tempo_bpm", "energy", "length_hint", "extra",
    "custom_language", "custom_genre",
]

#: 第三方默认字段值（ComfyUI-YuE2/nodes.py:253-265）
STYLE_DEFAULTS = {
    "language": "English",
    "genre": "piano pop",
    "mood": "intimate",
    "vocal": "female vocal",
    "vocal_color": "warm",
    "instruments": "acoustic piano, rounded bass and light drums",
    "tempo_bpm": 88,
    "energy": "mid energy",
    "length_hint": "single edit (~2-3 min)",
    "extra": "lyrical memorable melody, unhurried phrasing",
    "custom_language": "",
    "custom_genre": "",
}

#: 「括号即省略」的标记 —— 值以 ( 开头就整段丢弃（第三方 nodes.py:181-182）
_OMIT_PREFIX = "("

#: 运行时枚举缓存（第三方 yue2_schema.py:28-29 同款：15 分钟 TTL）
_OBJECT_INFO_URL = "http://127.0.0.1:8188/object_info"
_ENUM_TTL = 900.0
_enum_cache: dict = {"at": 0.0, "enums": {}}


def _fetch_enums() -> dict:
    """从运行中的 ComfyUI 拉 YuE2StylePrompt 的真实枚举。

    第三方 yue2_schema.py:106-134 就是这么做的 —— 枚举永不二次手抄，
    因为手抄过一次就出过 ComfyUI 400 value_not_in_list（h3_yue2.py:36-39 注释）。
    """
    try:
        with urllib.request.urlopen(_OBJECT_INFO_URL, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}
    node = data.get("YuE2StylePrompt") or {}
    required = node.get("input", {}).get("required", {})
    out = {}
    for field, spec in required.items():
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            out[field] = list(spec[0])
    return out


def get_enums() -> dict:
    """取枚举（优先运行时，失败回退静态副本）。"""
    now = time.time()
    if now - _enum_cache["at"] < _ENUM_TTL and _enum_cache["enums"]:
        return _enum_cache["enums"]
    runtime = _fetch_enums()
    merged = {k: list(v) for k, v in STATIC_ENUMS.items()}
    for k, v in runtime.items():
        if v:
            merged[k] = v
    _enum_cache["at"] = now
    _enum_cache["enums"] = merged
    return merged


def _omit(value) -> bool:
    """第三方 nodes.py:181-182 的 _omit：值为空或以 ( 开头 -> 该段丢弃。

    【重要，只对 mood / vocal / vocal_color / energy 生效】
    language / genre 走的是另一条路（见 build_style_prompt）——
    它们的 "(custom / none)" 是**要换成 custom_* 的值**，不是「省略」。
    """
    s = "" if value is None else str(value).strip()
    return not s or s.startswith(_OMIT_PREFIX)


def build_style_prompt(language, genre, mood, vocal, vocal_color, instruments,
                       tempo_bpm, energy, length_hint, extra,
                       custom_language="", custom_genre="") -> str:
    """拼 style 串 —— **逐行对齐**第三方 ComfyUI-YuE2/nodes.py:184-212。

    与第三方唯一的差别：第三方是 COMBO 值，这里允许任意字符串；
    但每个分支的判定逻辑完全一致（已用 9 组参数做同进程输出比对验证）。

    第三方原始逻辑（nodes.py:186-212）：
        lang = custom_language if language.startswith("(custom") else language
        gen  = custom_genre   if genre.startswith("(custom")    else genre
        if lang: parts.append(lang)
        if gen:  parts.append(gen)
        if not _omit(mood):     parts.append(mood)
        if not _omit(vocal):
            color = "" if _omit(vocal_color) else vocal_color + " "
            parts.append(color + vocal)
        elif not _omit(vocal_color):
            parts.append(vocal_color + " instrumental")
        inst = (instruments or "").strip()
        if inst: parts.append(inst)
        if int(tempo_bpm) > 0: parts.append(f"{int(tempo_bpm)} BPM")
        if not _omit(energy):   parts.append(energy)
        hint = LENGTH_HINTS.get(length_hint, "")
        if hint: parts.append(hint)
        extra_txt = (extra or "").strip()
        if extra_txt: parts.append(extra_txt)
        return ", ".join(parts)
    """
    parts = []
    lang = str(custom_language or "").strip() if str(language).startswith("(custom") else language
    if lang:
        parts.append(lang)
    gen = str(custom_genre or "").strip() if str(genre).startswith("(custom") else genre
    if gen:
        parts.append(gen)
    if not _omit(mood):
        parts.append(mood)
    if not _omit(vocal):
        color = "" if _omit(vocal_color) else str(vocal_color) + " "
        parts.append(color + str(vocal))
    elif not _omit(vocal_color):
        parts.append(str(vocal_color) + " instrumental")
    inst = (instruments or "").strip()
    if inst:
        parts.append(inst)
    try:
        bpm = int(tempo_bpm)
    except (TypeError, ValueError):
        bpm = 0
    if bpm > 0:
        parts.append(f"{bpm} BPM")
    if not _omit(energy):
        parts.append(energy)
    hint = LENGTH_HINT_TEXT.get(length_hint, "")
    if hint:
        parts.append(hint)
    extra_txt = (extra or "").strip()
    if extra_txt:
        parts.append(extra_txt)
    return ", ".join(parts)


def style_fields_from_params(params: dict) -> dict:
    """从面板参数里取结构化风格字段。

    params 里没给就用第三方默认值 —— 保证「什么都不填」也能出活，
    与第三方节点默认行为一致。
    """
    out = {}
    for field in STYLE_FIELDS:
        value = params.get(f"style_{field}")
        if value is None:
            value = STYLE_DEFAULTS.get(field, "")
        out[field] = value
    # 纯器乐联动（第三方 yue2_schema.py:397-402）
    if str(out.get("vocal", "")).strip() == "(instrumental / no lead vocal)":
        if not _omit(out.get("vocal_color")):
            out["vocal_color"] = "(none)"
    # custom_* 只在对应 COMBO 选中时才生效（第三方 yue2_schema.py:389-394）
    if str(out.get("language", "")).strip() != "(custom / none)":
        out["custom_language"] = ""
    if str(out.get("genre", "")).strip() != "(custom / none)":
        out["custom_genre"] = ""
    return out


def style_options() -> dict:
    """给前端的结构化风格选项清单。"""
    enums = get_enums()
    return {
        "fields": list(STYLE_FIELDS),
        "defaults": dict(STYLE_DEFAULTS),
        "enums": {k: list(v) for k, v in enums.items()},
        "lyric_templates": list(LYRIC_TEMPLATES.keys()),
        "lyric_template_default": "V-C-V-C-bridge-C",
    }


def resolve_lyrics(lyrics: str, template: str = "V-C-V-C-bridge-C") -> str:
    """歌词为空时用段落骨架兜底（第三方 YuE2LyricsTemplate 语义）。

    第三方是「lyrics_override 非空 -> 完全忽略 template」
    （ComfyUI-YuE2/nodes.py:281-283）。我们的面板同理：
    用户填了歌词就用用户的，没填才用骨架。
    """
    text = (lyrics or "").strip()
    if text:
        return lyrics
    return LYRIC_TEMPLATES.get(template, LYRIC_TEMPLATES["V-C-V-C-bridge-C"])
