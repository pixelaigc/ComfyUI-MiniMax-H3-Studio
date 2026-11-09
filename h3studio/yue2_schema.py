# -*- coding: utf-8 -*-
"""
YuE2 音乐配置的权威 schema / 校验器 / 归一化器 / 系统提示词渲染器。

【来源与对应关系】
本模块移植自 nonebot 插件 `nonebot_plugin_novelai` 的 `yue2_schema.py`
（咏唱「文生音乐 / 参考生音乐」的实现），对应关系：
  * 权威枚举源      -> 同名模块 `yue2_schema.py`（STATIC_ENUMS / get_enums）
  * 归一化 + 校验   -> 同名模块 `normalize_and_validate()`
  * 系统提示词渲染  -> 同名模块 `build_system_prompt()`
  * 节点字段顺序    -> 咏唱 `workflows/yue2_emergent_15node.json` 的
                       YuE2StylePrompt / YuE2LyricsTemplate 节点 widgets 顺序

【为什么必须有这一层】
YuE2 的风格节点（YuE2StylePrompt）有 8 个 COMBO 字段，每个字段的合法值
都是**带括号、带波浪号、带斜杠**的固定字符串（比如
`(instrumental / no lead vocal)`、`full song (~3.5-5 min)`）。
LLM 极容易意译 / 去括号 / 改大小写，而 ComfyUI 侧是逐字校验的 ——
值不在枚举里就是 HTTP 400 value_not_in_list。
旧版咏唱实现把枚举手抄在系统提示词里（只列了 18/47 个 genre），
结果就是「模型产出正常、ComfyUI 直接拒绝」。
所以这里的规则是：**枚举只从 ComfyUI /object_info 运行时拉，
拉不到才用静态兜底；系统提示词从同一份 schema 渲染，枚举永不二次手抄。**

【与本项目既有链路的衔接】
本项目走 **ComfyUI 原生** YuE2 节点（YuE2GenerateABC / YuE2GenerateMusic），
不是第三方 custom_nodes/ComfyUI-YuE2 的 YuE2StylePrompt 节点 ——
后者内部会调 torch.cuda.set_per_process_memory_fraction()，
是进程级设置，跑一次音乐任务后整个 ComfyUI 进程显存上限被永久压低。
但**字段语义完全一致**：原生节点吃的 `style` / `lyrics` 两个字符串，
正是 YuE2StylePrompt.build_style_prompt() 与 YuE2LyricsTemplate.build()
拼出来的东西。所以本模块照样产出结构化字段，再由
h3studio/music_yue2.py 用**与官方节点逐条对齐的拼接规则**合成最终字符串。

红线：纯 helper，不 import 任何 Web 框架，可被脚本直接 import 复测。
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import threading
import time
import urllib.request

logger = logging.getLogger("h3studio.yue2_schema")

# ComfyUI 的 object_info 端点（本机 ComfyUI 固定在 8188）
COMFY_OBJECT_INFO_URL = "http://127.0.0.1:8188/object_info"
OBJECT_INFO_TTL = 900.0  # 15 分钟缓存，避免每次生成都打 ComfyUI

STYLE_NODE = "YuE2StylePrompt"
LYRICS_NODE = "YuE2LyricsTemplate"

# 节点 2 / 节点 3 的字段顺序（与 ComfyUI input_order 一致）
STYLE_FIELDS = [
    "language", "genre", "mood", "vocal", "vocal_color", "instruments",
    "tempo_bpm", "energy", "length_hint", "extra",
    "custom_language", "custom_genre",
]
LYRICS_FIELDS = ["template", "lyrics_override"]
COMBO_FIELDS = [
    "language", "genre", "mood", "vocal", "vocal_color",
    "energy", "length_hint", "template",
]

# 静态兜底枚举：ComfyUI 拉不到时用。
# 与 /home/ubuntu/comfyui/custom_nodes/ComfyUI-YuE2/nodes.py 的
# LANGUAGES / GENRES / MOODS / VOCALS / VOCAL_COLORS / ENERGIES /
# LENGTH_HINTS / LYRIC_TEMPLATES 逐条一致（2026-11-09 核对）。
STATIC_ENUMS = {
    "language": ["English", "Mandarin", "Cantonese", "Japanese", "Korean",
                 "Spanish", "French", "Portuguese", "German", "Italian",
                 "Hindi", "(custom / none)"],
    "genre": ["pop", "piano pop", "indie pop", "synth pop", "dance pop",
              "r&b", "soul", "funk", "nu-disco", "disco", "house", "edm",
              "hip hop", "trap", "rap", "rock", "indie rock",
              "alternative rock", "punk", "metal", "jazz", "jazz-funk",
              "bossa nova", "latin", "reggaeton", "country", "folk",
              "indie folk", "singer-songwriter", "acoustic", "gospel", "blues",
              "lo-fi", "dream pop", "shoegaze", "ambient", "cinematic",
              "film score", "classical crossover", "k-pop", "j-pop",
              "city pop", "c-pop", "afrobeats", "reggae", "ska",
              "(custom / none)"],
    "mood": ["(none)", "uplifting", "euphoric", "romantic", "intimate",
             "melancholic", "bittersweet", "dark", "aggressive", "playful",
             "nostalgic", "dreamy", "cinematic", "chill", "anthemic",
             "mysterious"],
    "vocal": ["(instrumental / no lead vocal)", "female vocal", "male vocal",
              "androgynous vocal", "duet male and female",
              "choir / stacked vocals", "rap lead", "spoken word"],
    "vocal_color": ["(none)", "warm", "bright", "breathy", "powerful",
                    "raspy", "soft intimate", "belting", "tenor", "baritone",
                    "soprano", "alto"],
    "energy": ["(none)", "low energy", "mid energy", "high energy",
               "builds from quiet to loud"],
    "length_hint": ["model decides", "short clip (~45-75s)",
                    "single edit (~2-3 min)", "full song (~3.5-5 min)",
                    "very long (may truncate)"],
    "template": ["blank", "verse + chorus", "V-C-V-C", "V-C-V-C-bridge-C"],
}
TEMPO_MIN, TEMPO_MAX = 0, 220

# 默认值兜底：LLM 漏填 / 填了垃圾值时的收敛目标
DEFAULTS = {
    "language": "English",
    "genre": "piano pop",
    "mood": "(none)",
    "vocal": "(instrumental / no lead vocal)",
    "vocal_color": "(none)",
    "instruments": "acoustic piano, rounded bass and light drums",
    "tempo_bpm": 88,
    "energy": "mid energy",
    "length_hint": "single edit (~2-3 min)",
    "extra": "lyrical memorable melody, unhurried phrasing",
    "custom_language": "",
    "custom_genre": "",
    "template": "V-C-V-C-bridge-C",
}

# 括号即省略：节点 build_style_prompt 里的 _omit() 会把以 "(" 开头的值整段丢掉
OMIT_PREFIX = "("
INSTRUMENTAL_VOCAL = "(instrumental / no lead vocal)"

MIN_LYRICS_CHARS = 200
#: 普通段（Verse / Intro / Bridge / Outro …）的最小正文行数。
#: 【审查 P3-2】原先对所有段一律要求 4 行，把正常的段落结构误判成问题。
#: 现在行数门槛**只对副歌段生效**，普通段不再按行数判。
MIN_LINES_PER_SECTION = 0
#: 主副歌段（[Chorus] / [Final Chorus] …）的最小正文行数。
#: 副歌是全曲记忆点，行数不足确实值得提醒，所以要求更严。
MIN_LINES_PER_CHORUS = 4
#: 判定「这是不是副歌段」的段标签特征。
#:   [Chorus] / [Chorus 2] / [Final Chorus] / [Big Chorus] …
#:   注意：**排除** [Pre-Chorus] —— 它是前副歌，不是主副歌，
#:   行数少是正常写法（审查 P3-2 的误报案例里就有它）。
CHORUS_TAG_RE = re.compile(r"(?<!pre-)chorus", re.I)
SECTION_TAG_RE = re.compile(r"^\[[^\]\n]{1,30}\]\s*$", re.M)
# [Instrumental] / [Instrumental Break] 这类不是曲式段，是段内的占位内容。
# 纯器乐歌词全是它，不能当段标签切，否则每段都变成 0 行正文，误报「段不足 4 行」。
INSTRUMENTAL_TAG_RE = re.compile(r"^\[instrumental[^\]]*\]$", re.I)
LYRICS_FENCE_RE = re.compile(r"^\s*`{3,}\s*$", re.M)

_cache = {"at": 0.0, "data": dict(STATIC_ENUMS)}
_cache_lock = threading.Lock()


# ==================== schema 拉取 ====================

def _fetch_enums(timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(COMFY_OBJECT_INFO_URL, timeout=timeout) as r:
        oi = json.loads(r.read())
    out = {}
    sp = oi[STYLE_NODE]["input"]["required"]
    for k in ("language", "genre", "mood", "vocal", "vocal_color",
              "energy", "length_hint"):
        out[k] = list(sp[k][0])
    out["template"] = list(oi[LYRICS_NODE]["input"]["required"]["template"][0])
    return out


def get_enums(force: bool = False) -> dict:
    """返回权威枚举。优先运行时拉 ComfyUI，失败/超时用静态兜底（永不抛异常）。"""
    with _cache_lock:
        fresh = (time.monotonic() - _cache["at"]) < OBJECT_INFO_TTL
        if fresh and not force:
            return _cache["data"]
    try:
        live = _fetch_enums()
        with _cache_lock:
            _cache["at"] = time.monotonic()
            _cache["data"] = live
        return live
    except Exception as e:  # noqa: BLE001
        logger.warning("[yue2_schema] 拉 %s 失败，用静态兜底枚举: %s",
                       COMFY_OBJECT_INFO_URL, e)
        with _cache_lock:
            if not _cache["at"]:
                _cache["data"] = dict(STATIC_ENUMS)
            return _cache["data"]


# ==================== 归一化 ====================

def _norm_combo(value, allowed, field, fixes):
    """把一个 COMBO 值收敛到合法枚举，记录修复动作。"""
    if not allowed:
        return DEFAULTS.get(field, "")
    if value is None:
        fixes.append(f"{field}: 缺失 -> 默认 {DEFAULTS.get(field)!r}")
        return DEFAULTS.get(field, allowed[0])
    if not isinstance(value, str):
        fixes.append(f"{field}: 类型 {type(value).__name__} -> 转 str")
        value = str(value)
    v = value.strip()
    if not v:
        fixes.append(f"{field}: 空串 -> 默认 {DEFAULTS.get(field)!r}")
        return DEFAULTS.get(field, allowed[0])
    if v in allowed:
        return v

    # 逐层放宽：精确 -> 归一化空白/大小写 -> 去括号 -> 去副词 -> 子串 -> 模糊
    candidates = [
        (lambda s: re.sub(r"\s+", " ", s.strip()), "空白归一化"),
        (lambda s: s.lower(), "小写化"),
        (lambda s: s.strip("()").strip(), "去外层括号"),
        (lambda s: s.strip("()[]").strip().lower(), "去括号+小写"),
    ]
    for fn, how in candidates:
        try:
            cand = fn(v)
        except Exception:  # noqa: BLE001
            continue
        for a in allowed:
            if fn(a) == cand:
                fixes.append(f"{field}: {v!r} -> {a!r} ({how})")
                return a

    low = v.lower()
    # 「(instrumental / no lead vocal)」类：去掉全部非字母数字后比对
    def squash(s):
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    sq = squash(v)
    for a in allowed:
        if squash(a) == sq:
            fixes.append(f"{field}: {v!r} -> {a!r} (去符号)")
            return a
    # 意译近似：full song (about four minutes) -> full song (~3.5-5 min)
    for a in allowed:
        al = a.lower()
        if low == al:
            fixes.append(f"{field}: {v!r} -> {a!r} (小写匹配)")
            return a
        core = re.split(r"[ (~]", al)[0].strip()
        if core and low.startswith(core) and core in ("full", "short", "single",
                                                      "very", "model"):
            fixes.append(f"{field}: {v!r} -> {a!r} (前缀匹配)")
            return a
    # 子串：LLM 写 "rock music" 而枚举是 "rock"
    hits = [a for a in allowed if len(a) > 2 and (a.lower() in low or low in a.lower())]
    hits = [h for h in hits if not h.startswith(OMIT_PREFIX)] or hits
    if len(hits) == 1:
        fixes.append(f"{field}: {v!r} -> {hits[0]!r} (子串唯一匹配)")
        return hits[0]
    # difflib 兜底
    close = difflib.get_close_matches(v, allowed, n=1, cutoff=0.7)
    if close:
        fixes.append(f"{field}: {v!r} -> {close[0]!r} (模糊匹配)")
        return close[0]

    fixes.append(f"{field}: {v!r} 不在任何枚举里 -> 默认 {DEFAULTS.get(field)!r}")
    return DEFAULTS.get(field, allowed[0])


def _norm_tempo(value, fixes):
    if value is None:
        fixes.append(f"tempo_bpm: 缺失 -> 默认 {DEFAULTS['tempo_bpm']}")
        return DEFAULTS["tempo_bpm"]
    if isinstance(value, bool):
        fixes.append("tempo_bpm: bool -> 默认")
        return DEFAULTS["tempo_bpm"]
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            n = int(m.group(0))
            fixes.append(f"tempo_bpm: {value!r} -> {n} (从字符串抽数字)")
            return max(TEMPO_MIN, min(TEMPO_MAX, n))
        fixes.append(f"tempo_bpm: {value!r} 无数字 -> 默认 {DEFAULTS['tempo_bpm']}")
        return DEFAULTS["tempo_bpm"]
    if isinstance(value, float):
        n = int(round(value))
        fixes.append(f"tempo_bpm: {value} -> {n} (取整)")
        return max(TEMPO_MIN, min(TEMPO_MAX, n))
    if isinstance(value, int):
        if TEMPO_MIN <= value <= TEMPO_MAX:
            return value
        n = max(TEMPO_MIN, min(TEMPO_MAX, value))
        fixes.append(f"tempo_bpm: {value} 越界 -> {n}")
        return n
    fixes.append(f"tempo_bpm: 类型 {type(value).__name__} -> 默认 {DEFAULTS['tempo_bpm']}")
    return DEFAULTS["tempo_bpm"]


def _norm_text(value, field, fixes, min_len=1):
    if value is None:
        fixes.append(f"{field}: 缺失 -> 默认")
        return DEFAULTS.get(field, "")
    if not isinstance(value, str):
        value = str(value)
        fixes.append(f"{field}: 类型 {type(value).__name__} -> 转 str")
    v = value.strip()
    if len(v) < min_len:
        # 关键：字段有非空兜底值时不要清空。清空会让风格串丢掉这一项，
        # 比原样保留用户意图更糟。
        fallback = DEFAULTS.get(field, "")
        if fallback:
            fixes.append(f"{field}: 只有 {len(v)} 字符 (<{min_len}) -> 默认")
            return fallback
        # 兜底也是空（只有 lyrics_override 会这样）-> 保留原文，让上层能看见并报错
        fixes.append(f"{field}: 只有 {len(v)} 字符 (<{min_len}) -> 保留原文 (无兜底值)")
        return v
    return v


def _fallback_lyrics() -> str:
    """lyrics_override 的兜底：段标签齐全的 [Instrumental] 骨架。

    比空字符串安全 —— 原生 YuE2 节点在 lyrics 为空时会自行产出占位内容，
    那会让模型唱出跟用户主题无关的词。这里给的是结构合法、无实词的纯器乐
    骨架，至少曲式正确。
    """
    tags = ["[Intro]", "[Verse 1]", "[Chorus]", "[Verse 2]", "[Chorus]",
            "[Bridge]", "[Final Chorus]", "[Outro]"]
    return "\n\n".join(f"{t}\n[Instrumental]" for t in tags)


def strip_code_fence(text: str) -> str:
    """剥掉 LLM 偶尔加的 markdown 围栏（```json ... ```）。"""
    t = (text or "").strip()
    if not t.startswith("```"):
        return t
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.DOTALL)
    return m.group(1).strip() if m else t


def extract_sections(lyrics: str):
    """把 lyrics 拆成 [(tag, body_lines), ...]。[Instrumental] 不算段标签。"""
    if not lyrics:
        return []
    lines = lyrics.split("\n")
    sections, cur_tag, cur_body = [], None, []
    for ln in lines:
        s = ln.strip()
        if SECTION_TAG_RE.match(s) and not INSTRUMENTAL_TAG_RE.match(s):
            if cur_tag is not None:
                sections.append((cur_tag, cur_body))
            cur_tag, cur_body = s, []
        elif cur_tag is not None:
            cur_body.append(s)
    if cur_tag is not None:
        sections.append((cur_tag, cur_body))
    return sections


def _is_chorus_tag(tag: str) -> bool:
    """这个段是不是主副歌段（[Chorus] / [Final Chorus] / [Chorus 2] …）。

    【审查 P3-2】副歌的行数要求比其它段严 —— 副歌是全曲记忆点，
    写成 1~2 行确实值得提醒；而 Verse 3 行 / Intro 2 行是常规写法。
    """
    return bool(CHORUS_TAG_RE.search(tag or ""))


def _lyrics_problems(lyrics: str):
    """返回歌词质量问题列表（非空 = 有问题）。"""
    problems = []
    clean = LYRICS_FENCE_RE.sub("", lyrics)
    sections = extract_sections(clean)
    if not sections:
        problems.append("lyrics: 没有识别到任何 [段标签]")
        return problems
    tags = [t for t, _ in sections]
    for required in ("[Chorus]", "[Bridge]"):
        if not any(t == required for t in tags):
            problems.append(f"lyrics: 缺 {required} 段")
    # 纯器乐歌词每段只有一行 [Instrumental]，是正确写法，不按行数要求判
    instrumental_only = all(
        all(not b or b.lower() == "[instrumental]"
            or b.lower().startswith("[instrumental") for b in body)
        for _t, body in sections)
    if not instrumental_only:
        # 【审查 P3-2】原来对**所有**段一律要求 >= 4 行，把
        #   [Intro] / [Verse 1] / [Pre-Chorus] / [Bridge] / [Outro]
        # 这些**正常段落结构**全判成问题（实测每次必现的固定噪音，
        # 降低提示的可信度）。
        #
        # 审查报告的建议原文：「把『不足 4 行』降级为**仅对
        # [Chorus]/[Final Chorus] 这类主歌段生效**」。照此实现：
        #   - 主副歌段（[Chorus] / [Final Chorus] / [Chorus 2] …）
        #     -> 仍要求 >= 4 行（副歌是全曲记忆点，行数不足值得提醒）
        #   - 其它段（Verse / Intro / Pre-Chorus / Bridge / Outro）
        #     -> **不再按行数判**（Verse 3 行、Intro 1 行都是常规写法）
        thin = []
        for tag, body in sections:
            if not _is_chorus_tag(tag):
                continue
            if len([b for b in body if b]) < MIN_LINES_PER_CHORUS:
                thin.append(tag)
        if thin:
            problems.append(
                f"lyrics: 这些副歌段不足 {MIN_LINES_PER_CHORUS} 行: {thin}")
    if len(clean.strip()) < MIN_LYRICS_CHARS:
        problems.append(f"lyrics: 只有 {len(clean.strip())} 字符 (<{MIN_LYRICS_CHARS})")
    return problems


def lyrics_is_degraded(lyrics: str) -> bool:
    """纯器乐占位：每个段下面都是 [Instrumental] 或空 —— 模型拿不到可唱内容。"""
    sections = extract_sections(LYRICS_FENCE_RE.sub("", lyrics))
    if not sections:
        return False
    real = 0
    for _tag, body in sections:
        for ln in body:
            s = ln.strip()
            if s and not s.lower().startswith("[instrumental"):
                real += 1
    return real == 0


# ==================== 主入口 ====================

STYLE_KEY = "style"
LYRICS_KEY = "lyrics"


def normalize_and_validate(config, enums=None, log_prefix="[yue2_schema]"):
    """
    校验 + 归一化模型产出的 config。

    返回 (ok, normalized_config, fixes, problems)：
      ok=True       -> normalized 一定能进 ComfyUI，fixes 记录了所有自动修复
      ok=False      -> 顶层结构坏（没两个节点），无法修复
      problems      -> 已修复但仍有质量问题（仅日志/提示用，不阻断）
    """
    enums = enums or get_enums()
    fixes, problems = [], []

    if not isinstance(config, dict):
        return False, None, [], [f"顶层不是 dict (是 {type(config).__name__})"]

    # LLM 有时会多包一层 {"config": {...}} / {"yue2_config": {...}}
    for wrapper in ("config", "yue2_config", "result", "yue2"):
        if STYLE_KEY not in config and isinstance(config.get(wrapper), dict):
            inner = config[wrapper]
            if STYLE_KEY in inner or LYRICS_KEY in inner:
                fixes.append(f"拆掉外层包装 key={wrapper!r}")
                config = inner
                break

    n2 = config.get(STYLE_KEY)
    n3 = config.get(LYRICS_KEY)
    # 单个节点块缺失/类型坏 -> 用 {} 继续跑，所有字段走默认值兜底。
    # 只有当两块都无法用（全空/全非 dict）才判失败，因为那时候产出毫无用户意图。
    if not isinstance(n2, dict):
        n2 = {}
        fixes.append(f"{STYLE_KEY}: 缺失或不是 dict -> 整块用默认值重建")
    if not isinstance(n3, dict):
        n3 = {}
        fixes.append(f"{LYRICS_KEY}: 缺失或不是 dict -> 整块用默认值重建")
    if not n2 and not n3:
        return False, None, fixes, ["两个节点块都没有可用内容, 无法安全兜底"]

    out2 = {}
    for f in STYLE_FIELDS:
        if f == "tempo_bpm":
            out2[f] = _norm_tempo(n2.get(f), fixes)
            continue
        if f in COMBO_FIELDS:
            out2[f] = _norm_combo(n2.get(f), enums.get(f, []), f, fixes)
            continue
        if f in ("custom_language", "custom_genre"):
            v = n2.get(f)
            out2[f] = v.strip() if isinstance(v, str) else ""
            if v not in (None, "", "null"):
                fixes.append(f"{f}: {v!r} -> \"\" (可选字段常规留空)")
            continue
        out2[f] = _norm_text(n2.get(f), f, fixes)

    # 纯器乐时 vocal_color 必须 (none)，否则拼接出 "warm instrumental"
    if out2["vocal"] == INSTRUMENTAL_VOCAL and out2["vocal_color"] != "(none)":
        fixes.append(
            f"vocal_color: {out2['vocal_color']!r} -> '(none)' "
            f"(纯器乐, 否则 style 串会拼出 'xxx instrumental')")
        out2["vocal_color"] = "(none)"

    lyrics_raw = n3.get("lyrics_override")
    out3 = {
        "template": _norm_combo(n3.get("template"), enums.get("template", []),
                                 "template", fixes),
        "lyrics_override": _norm_text(lyrics_raw, "lyrics_override", fixes,
                                      min_len=MIN_LYRICS_CHARS),
    }
    # lyrics_override 兜底也不能是空串：空串会让模型唱出跟用户主题无关的词
    if not out3["lyrics_override"].strip():
        out3["lyrics_override"] = _fallback_lyrics()
        fixes.append("lyrics_override: 空 -> 段标签齐全的 [Instrumental] 骨架")

    # template 只在 lyrics_override 为空时兜底，强制固定值避免无意义漂移
    if out3["template"] != "V-C-V-C-bridge-C":
        fixes.append(f"template: {out3['template']!r} -> 'V-C-V-C-bridge-C' (固定值)")
        out3["template"] = "V-C-V-C-bridge-C"

    for p in _lyrics_problems(out3["lyrics_override"]):
        problems.append(p)
    if lyrics_is_degraded(out3["lyrics_override"]):
        problems.append("lyrics: 全是 [Instrumental] 占位, 无可唱内容 (若是纯器乐则正常)")

    if fixes:
        logger.info("%s 自动修复 %d 处: %s", log_prefix, len(fixes), "; ".join(fixes[:12]))
    return True, {STYLE_KEY: out2, LYRICS_KEY: out3}, fixes, problems


# ==================== 系统提示词渲染 ====================

_FIELD_DOC = [
    ("language", "COMBO", "演唱/歌词语言。决定 YuE2 用哪种语言的语义模型写歌。"
     "中文描述不等于中文歌：用户说「中文歌」填 Mandarin，说「日语角色曲」填 Japanese。",
     "用户没指定语言时：歌词用中文填 Mandarin，纯器乐填 English"),
    ("genre", "COMBO", "音乐风格主分类。选最贴近用户描述的一个，不要组合多个。",
     "不确定时选 (custom / none) 并同时填 custom_genre"),
    ("mood", "COMBO", "情绪色彩。影响 style 串里的情绪词，直接决定听感明暗。",
     "用户完全没提情绪时填 (none)"),
    ("vocal", "COMBO", "主唱类型。纯器乐必须填 (instrumental / no lead vocal) —— "
     "注意值两端各有一对圆括号，一个都不能少。",
     "用户说「无人声/纯音乐/背景音乐/instrumental/BGM」-> (instrumental / no lead vocal)"),
    ("vocal_color", "COMBO", "音色质地。只在 vocal 是人声时生效；vocal 是纯器乐时本字段"
     "必须填 (none)，否则程序会拼出「warm instrumental」这种怪描述，模型可能真的加进人声。",
     "配对建议：female vocal -> warm/bright/breathy；male vocal -> tenor/baritone/raspy；"
     "rap lead -> raspy/powerful；choir -> powerful"),
    ("instruments", "STRING", "乐器描述，多行英文。至少 3 个具体乐器名，并显式排除不要的"
     "（写 no synth, no drums 这种）。禁止只写 piano / electronic 这种单词，"
     "禁止写 atmospheric reverb 之类空话。",
     "用英文写乐器名（模型吃英文 style prompt）。中文意象放 extra 字段"),
    ("tempo_bpm", "INT", "速度 BPM。整数 0-220。填 0 表示不告诉模型速度"
     "（style 串里不出现 BPM）。",
     "慢歌 60-80，中速 88-110，快歌 120-160，舞曲 128-132。"
     "中文描述里的「慢/快/激烈」要能反映到数字上"),
    ("energy", "COMBO", "能量走向。描述整首歌的动态曲线。",
     "安静/舒缓 -> low energy；普通 -> mid energy；燃/炸裂 -> high energy；"
     "有 buildup 结构 -> builds from quiet to loud"),
    ("length_hint", "COMBO", "目标时长。会被翻译成 style 串里的英文时长描述。",
     "值里带 ~ 和括号的必须逐字复制，禁止意译。例：想要四分钟的歌填 "
     "full song (~3.5-5 min)，不是 full song (about four minutes)"),
    ("extra", "STRING", "主题意象补充，多行。放歌词没法表达的东西："
     "场景/画面/叙事/情绪走向/参考作品气质。可以写中文。",
     "把用户原话里的画面感（e.g. 雨夜霓虹 / 月下独舞 / 赛博朋克）完整搬进这里，"
     "这是影响成片听感最直接的字段"),
    ("custom_language", "STRING (optional)",
     "仅当 language = (custom / none) 时生效。其他情况填空字符串 \"\"。",
     "留空时必须是空字符串 \"\"，不是 null，不是省略"),
    ("custom_genre", "STRING (optional)",
     "仅当 genre = (custom / none) 时生效。其他情况填空字符串 \"\"。",
     "留空时必须是空字符串 \"\"，不是 null，不是省略"),
]

_LYRICS_DOC = [
    ("template", "COMBO", "歌词骨架模板。只在 lyrics_override 为空时兜底，"
     "程序会强制改成 V-C-V-C-bridge-C，你按这个填即可。",
     "固定填 V-C-V-C-bridge-C"),
    ("lyrics_override", "STRING", "完整歌词，多行。必须带段标签，副歌段至少 4 行，"
     "全文字符数 >= 200。段标签可用：[Intro] [Verse 1] [Pre-Chorus] "
     "[Chorus] [Verse 2] [Bridge] [Final Chorus] [Outro]。",
     "人声歌：段标签下面写真实歌词，必须跟用户主题一致。纯器乐：每个段标签下面"
     "单独一行写 [Instrumental]，但段标签本身必须齐全（这是 YuE2 识别曲式的依据）"),
]


def _enum_rows(items, width=100):
    lines, cur = [], ""
    for v in items:
        piece = v + " | "
        if len(cur) + len(piece) > width:
            lines.append(cur)
            cur = ""
        cur += piece
    if cur:
        lines.append(cur.rstrip())
    return "\n".join(lines)


def _example_a():
    lyrics = ("[Intro]\n雨还没有停\n窗外的灯一盏一盏熄了\n\n"
              "[Verse 1]\n我把伞留在门口\n像很多年前那样\n你的拖鞋还在鞋柜最下层\n\n"
              "[Pre-Chorus]\n话到嘴边又咽回去\n只剩雨声替我说了\n\n"
              "[Chorus]\n再见 请不必回头\n让这场雨送我一程\n我会记得这个温度\n"
              "在某个没有你的早晨\n\n"
              "[Verse 2]\n末班车开过桥的那一秒\n我数着车窗上的水痕\n"
              "数到第七道的时候\n你就已经不在了\n\n"
              "[Bridge]\n也许某天雨会停\n我也会学会不带伞\n\n"
              "[Final Chorus]\n再见 请不必回头\n让这场雨送我一程\n我会记得这个温度\n"
              "在某个没有你的早晨\n\n"
              "[Outro]\n雨小了一些\n天快亮了")
    obj = {
        "style": {
            "language": "Japanese", "genre": "piano pop", "mood": "bittersweet",
            "vocal": "female vocal", "vocal_color": "breathy",
            "instruments": "acoustic piano, sustained string ensemble, "
                           "felt piano chords, soft brushed drums, no synth pads, "
                           "no electric guitar",
            "tempo_bpm": 76, "energy": "builds from quiet to loud",
            "length_hint": "full song (~3.5-5 min)",
            "extra": "雨夜告别题材. 前段克制雨滴般的钢琴独奏, 副歌弦乐全开释放, "
                     "整体是告别前最后一夜的温度, 结尾回到安静的余韵. "
                     "旋律要走能直接哼唱的叙事型动机, 不要炫技.",
            "custom_language": "", "custom_genre": "",
        },
        "lyrics": {
            "template": "V-C-V-C-bridge-C", "lyrics_override": lyrics,
        },
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _example_b():
    tags = ["[Intro]", "[Verse 1]", "[Chorus]", "[Verse 2]", "[Chorus]",
            "[Bridge]", "[Final Chorus]", "[Outro]"]
    lyrics = "\n\n".join(f"{t}\n[Instrumental]" for t in tags)
    obj = {
        "style": {
            "language": "English", "genre": "cinematic", "mood": "dreamy",
            "vocal": "(instrumental / no lead vocal)", "vocal_color": "(none)",
            "instruments": "lead violin with classical solo phrasing, full string "
                           "ensemble violin II viola cello double bass, concert harp "
                           "arpeggios, solo flute countermelody, woodwind choir "
                           "clarinet oboe, no synth, no drums, no electric guitar",
            "tempo_bpm": 86, "energy": "builds from quiet to loud",
            "length_hint": "single edit (~2-3 min)",
            "extra": "优雅古典芭蕾配乐. 竖琴琶音铺底如流水, 弦乐群在远方轻应和, "
                     "长笛与双簧管在副旋律上追逐, 整体克制不喧宾夺主, "
                     "适合作为角色舞蹈过场. 旋律带一点浪漫主义芭蕾感, "
                     "又有东方幻想的调式色彩.",
            "custom_language": "", "custom_genre": "",
        },
        "lyrics": {
            "template": "V-C-V-C-bridge-C", "lyrics_override": lyrics,
        },
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)


def build_system_prompt(enums=None) -> str:
    """从同一份 schema 渲染系统提示词 —— 枚举永不二次手抄。"""
    enums = enums or get_enums()
    p = []
    A = p.append

    A("你是 YuE2 音乐生成节点配置器。用户会给你一句话中文描述，")
    A("你的任务是产出 EmeraldApple-AI/ComfyUI-YuE2 工作流中 **节点 2（YuE2StylePrompt）** 与")
    A("**节点 3（YuE2LyricsTemplate）** 的完整输入配置 JSON。")
    A("")
    A("工作流里其余节点（模型加载 / 生成参数 / 保存）由程序用固定默认值注入，**不需要你输出**。")
    A("")

    A("## 一、输出契约（违反任何一条，程序会直接判定为失败）")
    A("")
    A("1. **只输出一个 JSON 对象**，且必须是合法 JSON：双引号、无注释、无尾逗号。")
    A("2. **不要 Markdown 代码围栏**。第一字符必须是 `{`，最后一个字符必须是 `}`。")
    A("3. **JSON 之后不得有任何字符**——不加解释、不加「已生成」、不加字段校验说明、不加致谢。")
    A("4. **顶层必须且只有两个 key**，顺序固定为：")
    A("   `style` → `lyrics`")
    A("5. **每个 key 必须全部显式写出，一个都不能省**。空值写成 `\"\"`（空字符串），不要省略 key，不要写 null。")
    A("6. **所有 COMBO 字段的值必须从本提示词给出的枚举表里逐字节复制**，包括圆括号、斜杠、波浪号、连字符。")
    A("   禁止改写、意译、补全、去括号、换大小写。")
    A("7. `tempo_bpm` 必须是 JSON 数字（不加引号）。")
    A("8. **不要输出任何 JSON 以外的包装层**（不要 `{\"config\": {...}}` / `{\"yue2_config\": {...}}`）。")
    A("")

    A(f"## 二、节点 2 `style`（{len(STYLE_FIELDS)} 个字段）")
    A("")
    A("该节点把下列字段拼成一条英文 style prompt 交给模型。字段顺序即拼接顺序。")
    A("")
    for i, (key, typ, desc, rule) in enumerate(_FIELD_DOC, 1):
        tag = "可选" if key in ("custom_language", "custom_genre") else "必填"
        A(f"### 2.{i} `{key}`")
        A(f"- **类型**: {typ}　**{tag}**")
        A(f"- **说明**: {desc}")
        A(f"- **填写规则**: {rule}")
        if key in enums:
            A(f"- **合法值（共 {len(enums[key])} 个，逐字复制，用 ` | ` 分隔）**:")
            for row in _enum_rows(enums[key]).split("\n"):
                A(f"  {row}")
        elif key == "tempo_bpm":
            A(f"- **合法值**: 整数 `{TEMPO_MIN}` ~ `{TEMPO_MAX}`")
        else:
            A("- **合法值**: 任意字符串（通常是空字符串 `\"\"`）")
        A("")

    A("### 2.13 节点 2 的隐式规则（不遵守会生成怪异的 style 串）")
    A("")
    A("- 节点内部有一条「括号即省略」规则：**任何以 `(` 开头的值会被从 style 串里整段丢掉**。")
    A("  所以 `(none)` 表示「这一项不参与拼接」，这是正常用法，不是错误。")
    A("- 因此：想要纯器乐，`vocal` 填 `(instrumental / no lead vocal)`、`vocal_color` 填 `(none)`，")
    A("  程序会把它们都省略，最终 style 串里不会出现任何关于人声的描述。")
    A("- 反之，如果纯器乐时 `vocal_color` 填了 `warm`，style 串会变成 `warm instrumental`，")
    A("  模型可能真的加进 vocal，是错误配置。")
    A("- `language` 和 `genre` 填 `(custom / none)` 时，节点改用 `custom_language` / `custom_genre` 的值。")
    A("  这两个自定义字段平时一律填空字符串。")
    A("")

    A(f"## 三、节点 3 `lyrics`（{len(LYRICS_FIELDS)} 个字段）")
    A("")
    for i, (key, typ, desc, rule) in enumerate(_LYRICS_DOC, 1):
        A(f"### 3.{i} `{key}`")
        A(f"- **类型**: {typ}　**必填**")
        A(f"- **说明**: {desc}")
        A(f"- **填写规则**: {rule}")
        if key in enums:
            A(f"- **合法值（共 {len(enums[key])} 个，逐字复制）**:")
            for row in _enum_rows(enums[key]).split("\n"):
                A(f"  {row}")
        else:
            A("- **合法值**: 任意多行字符串（用 `\\n` 转义换行）")
        A("")
    A("### 3.3 歌词内容规则")
    A("")
    A("- 歌词语言必须与 `language` 字段一致（Mandarin 写中文歌词，English 写英文歌词）。")
    A("- 歌词主题必须与用户描述、以及 `extra` 字段的意象保持一致。")
    A("- **纯器乐（`vocal` = `(instrumental / no lead vocal)`）的特殊处理**：")
    A("  每个段标签下面单独一行写 `[Instrumental]`，例如：")
    A("  ```")
    for tag in ("[Intro]", "[Verse 1]", "[Chorus]", "[Bridge]", "[Final Chorus]"):
        A(tag)
        A("[Instrumental]")
        A("")
    A("  [Outro]")
    A("  [Instrumental]")
    A("  ```")
    A("  段标签本身必须齐全（不能只有 `[Verse 1]` 没有 `[Chorus]`），YuE2 靠段标签识别曲式。")
    A("")

    A("## 四、输出示例（两种典型场景，结构完全一致）")
    A("")
    A("### 示例 A：人声歌（用户说「一首关于雨夜告别的日语慢歌，钢琴加弦乐，副歌要爆发」）")
    A("")
    A(_example_a())
    A("")
    A("### 示例 B：纯器乐（用户说「原神风格 芭蕾舞背景音乐 小提琴主乐器 无人声」）")
    A("")
    A(_example_b())
    A("")

    A("## 五、自检（输出前在内部过一遍，不要把自检过程写进答案）")
    A("")
    A("1. 输出是纯 JSON，第一个字符 `{`，最后一个字符 `}`。")
    A("2. 顶层两个 key 都在，每个 key 的所有子字段都在，没有外层包装。")
    A("3. 8 个 COMBO 字段（language / genre / mood / vocal / vocal_color / energy / length_hint / template）的值，")
    A("   都能在本提示词的枚举表里逐字找到。")
    A("4. `vocal` 是纯器乐时，写的是 `(instrumental / no lead vocal)`（带两侧圆括号）。")
    A("5. `length_hint` 写的是 `full song (~3.5-5 min)` 这样的原始枚举，不是自己改写的说法。")
    A("6. `tempo_bpm` 是数字，在 0-220 之间。")
    A("7. `lyrics_override` 有段标签、副歌段 >= 4 行、总字符 >= 200。")
    A("8. `custom_language` 和 `custom_genre` 都是 `\"\"`。")
    A("")
    A("以上任一条不满足，先修正再输出。最终答案只有那个 JSON 对象。")
    return "\n".join(p)


# ==================== 风格串 / 歌词串拼接 ====================

#: length_hint -> 拼进 style 串的英文时长描述
#: 与官方节点 nodes.py 的 LENGTH_HINTS 逐条一致。
LENGTH_HINTS = {
    "model decides": "",
    "short clip (~45-75s)": "short song, about one minute",
    "single edit (~2-3 min)": "radio-length song, about two to three minutes",
    "full song (~3.5-5 min)": "full-length song, about four minutes",
    "very long (may truncate)": "long-form song with extended sections",
}

#: 歌词骨架模板 -> 节点内置占位歌词
LYRIC_TEMPLATES = {
    "blank": "",
    "verse + chorus": "[Verse]\nLine one of the verse\nLine two of the verse\n\n"
                      "[Chorus]\nSing the hook here\nRepeat the title line",
    "V-C-V-C": "[Verse]\nFirst verse line\n\n[Chorus]\nMain hook\n\n"
               "[Verse]\nSecond verse line\n\n[Chorus]\nMain hook",
    "V-C-V-C-bridge-C": "[Verse]\nFirst verse\n\n[Chorus]\nHook\n\n"
                        "[Verse]\nSecond verse\n\n[Chorus]\nHook\n\n"
                        "[Bridge]\nContrast the chorus here\n\n[Chorus]\nFinal hook",
}


def _omit(value) -> bool:
    """官方节点的 _omit() 语义：空值或以 "(" 开头的值整段省略。"""
    return not value or str(value).startswith(OMIT_PREFIX)


def build_style_prompt(style: dict) -> str:
    """
    把结构化风格字段拼成一条英文 style prompt。

    【与官方节点逐条对齐】ComfyUI-YuE2/nodes.py 的 build_style_prompt()：
      顺序 = language, genre, mood, [vocal_color + vocal | vocal_color + " instrumental"],
      instruments, "{tempo} BPM", energy, length_hint, extra
      用 ", " 连接。
    """
    style = style or {}
    parts = []
    language = str(style.get("language") or "")
    lang = (style.get("custom_language") or "").strip() if language.startswith("(custom") else language
    if lang:
        parts.append(lang)
    genre = str(style.get("genre") or "")
    gen = (style.get("custom_genre") or "").strip() if genre.startswith("(custom") else genre
    if gen:
        parts.append(gen)
    mood = str(style.get("mood") or "")
    if not _omit(mood):
        parts.append(mood)
    vocal = str(style.get("vocal") or "")
    vocal_color = str(style.get("vocal_color") or "")
    if not _omit(vocal):
        color = "" if _omit(vocal_color) else vocal_color + " "
        parts.append(color + vocal)
    elif not _omit(vocal_color):
        parts.append(vocal_color + " instrumental")
    inst = (style.get("instruments") or "").strip()
    if inst:
        parts.append(inst)
    try:
        tempo = int(style.get("tempo_bpm") or 0)
    except (TypeError, ValueError):
        tempo = 0
    if tempo > 0:
        parts.append(f"{tempo} BPM")
    energy = str(style.get("energy") or "")
    if not _omit(energy):
        parts.append(energy)
    hint = LENGTH_HINTS.get(str(style.get("length_hint") or ""), "")
    if hint:
        parts.append(hint)
    extra = (style.get("extra") or "").strip()
    if extra:
        parts.append(extra)
    return ", ".join(parts)


def build_lyrics_prompt(lyrics: dict) -> str:
    """
    产出最终歌词文本。

    【与官方节点逐条对齐】ComfyUI-YuE2/nodes.py 的 YuE2LyricsTemplate.build()：
      lyrics_override 非空 -> 直接用它；为空 -> 回落到 template 内置占位。
    """
    lyrics = lyrics or {}
    text = (lyrics.get("lyrics_override") or "").strip()
    if text:
        return lyrics["lyrics_override"]
    return LYRIC_TEMPLATES.get(str(lyrics.get("template") or ""), "")
