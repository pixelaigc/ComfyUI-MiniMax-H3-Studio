# -*- coding: utf-8 -*-
"""YuE2 结构化风格 —— 面板侧的薄封装（**权威实现见 yue2_schema.py**）。

【为什么是薄封装】审查 P1-1：
   本模块曾与 h3studio/yue2_schema.py **各持一份** STATIC_ENUMS /
   _omit() / build_style_prompt() / length_hint 映射。两份副本当前
   行为一致（已实测同进程比对），但那是**巧合性的一致** —— 改一处忘
   另一处就会出现「A 路径能拼、B 路径拼不出」的隐性 bug。

   现在：**权威实现唯一** = h3studio/yue2_schema.py
     - STATIC_ENUMS / get_enums()  -> 运行时从 ComfyUI /object_info 拉，
       失败回退静态副本（永不在前端维护第二份枚举）
     - build_style_prompt()         -> 与官方节点 ComfyUI-YuE2/nodes.py
       逐条对齐
     - LENGTH_HINTS                 -> length_hint -> style 串里的英文时长描述
   本模块只保留**面板需要、但 schema 里没有**的东西：
     - STYLE_DEFAULTS（面板默认值，与 schema 的 DEFAULTS 语义不同：
       schema 的 DEFAULTS 是「模型漏填时的收敛目标」，面板默认是
       「用户没动过时给什么」）
     - style_fields_from_params()   -> 从 HTTP 参数取 12 个字段 + 纯器乐联动
     - style_options()              -> 给前端 /api/music-options 的选项清单
     - resolve_lyrics()             -> 歌词为空时用段落骨架兜底

【兼容】
   旧名保留（re-export），现有调用方（music_yue2.py / server.py / 测试）
   不需要改：
     STATIC_ENUMS / LENGTH_HINT_TEXT / LYRIC_TEMPLATES / STYLE_FIELDS /
     _OMIT_PREFIX / get_enums / _omit
"""

from . import yue2_schema

# ------------------------------------------------------------------
# 权威实现的 re-export（**不要在这里再抄一份**）
# ------------------------------------------------------------------

#: 静态兜底枚举（运行时权威仍是 ComfyUI /object_info）
STATIC_ENUMS = yue2_schema.STATIC_ENUMS

#: length_hint -> 写进 style 串的英文时长描述
#: （旧名 LENGTH_HINT_TEXT 保留；权威名 LENGTH_HINTS）
LENGTH_HINT_TEXT = yue2_schema.LENGTH_HINTS
LENGTH_HINTS = yue2_schema.LENGTH_HINTS

#: 歌词段落骨架模板
LYRIC_TEMPLATES = yue2_schema.LYRIC_TEMPLATES

#: 字段顺序 = ComfyUI YuE2StylePrompt 的 input_order
STYLE_FIELDS = yue2_schema.STYLE_FIELDS

#: 「括号即省略」的标记
_OMIT_PREFIX = yue2_schema.OMIT_PREFIX

#: 运行时枚举（优先 ComfyUI，失败回退静态副本）
get_enums = yue2_schema.get_enums

#: 「括号即省略」判定（官方节点 nodes.py:181-182 语义）
_omit = yue2_schema._omit


def build_style_prompt(language, genre, mood, vocal, vocal_color, instruments,
                       tempo_bpm, energy, length_hint, extra,
                       custom_language="", custom_genre="") -> str:
    """拼 style 串 —— **委托给权威实现** yue2_schema.build_style_prompt()。

    保留位置参数签名（历史调用方按位置传），内部转成 dict 交给权威实现。
    两边的判定逻辑因此**结构上**一致，不再依赖「两份副本碰巧相同」。
    """
    return yue2_schema.build_style_prompt({
        "language": language,
        "genre": genre,
        "mood": mood,
        "vocal": vocal,
        "vocal_color": vocal_color,
        "instruments": instruments,
        "tempo_bpm": tempo_bpm,
        "energy": energy,
        "length_hint": length_hint,
        "extra": extra,
        "custom_language": custom_language,
        "custom_genre": custom_genre,
    })


#: 面板默认值（用户没动过时给什么）。
#: 注意与 yue2_schema.DEFAULTS 的区别：那是「模型漏填时的收敛目标」。
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


def style_fields_from_params(params: dict) -> dict:
    """从面板参数里取结构化风格字段。

    params 里没给就用面板默认值 —— 保证「什么都不填」也能出活，
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
    """给前端的结构化风格选项清单（**同步**版）。

    【length_hint 的英文描述也在这里下发】前端原先硬编码了第三份
    length_hint 映射（web/music.js 的 STYLE_LENGTH_HINT_TEXT）——
    审查 P1-1 指出的第三份副本。现在由服务端下发，前端不再硬编码。

    【注意】这个函数内部会**同步阻塞**地拉 ComfyUI（见 get_enums）。
    async handler 里请用 style_options_async()，别直接调这个。
    """
    return _build_style_options(get_enums())


async def style_options_async() -> dict:
    """异步版 style_options —— 给 aiohttp 的 async handler 用。

    【为什么要异步版】
    get_enums() 内部是**同步阻塞**的 urllib 拉 ComfyUI /object_info
    （timeout 10s）。直接在 aiohttp 事件循环里调，整个 server 会被卡住
    —— 表现为「打开工作台后所有接口都超时」。

    这里用 yue2_schema.get_enums_async()（丢线程池）拿到枚举，
    再用同一份组装逻辑拼出选项清单。

    【为什么要单独一个函数】
    style_options() 里 style_options() 被调了 6 次（每个字段一次），
    缓存未命中时会打 6 次 ComfyUI。所以必须先取一次 enums，再组装。
    """
    enums = await yue2_schema.get_enums_async()
    return _build_style_options(enums)


def _build_style_options(enums: dict) -> dict:
    """按给定的枚举组装前端要的选项清单（纯组装，无 IO）。"""
    return {
        "fields": list(STYLE_FIELDS),
        "defaults": dict(STYLE_DEFAULTS),
        "enums": {k: list(v) for k, v in enums.items()},
        "lyric_templates": list(LYRIC_TEMPLATES.keys()),
        "lyric_template_default": "V-C-V-C-bridge-C",
        # length_hint -> style 串里的英文时长描述（前端拼预览用）
        "length_hint_text": dict(LENGTH_HINTS),
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
