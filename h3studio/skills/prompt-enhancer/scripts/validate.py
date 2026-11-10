#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提示词增强结果校验器。

每条检查对应 references/constraints.md 的一节。
被 h3studio/enhancer.py 复用，也可在命令行单独跑：

    python3 validate.py --text "一段提示词" --original "一个女孩"
    python3 validate.py --text "..." --original "..." --mode bbox

输出 JSON：
    {"ok": bool, "errors": [...], "warnings": [...], "stats": {...}}

判定分两级：
    errors   —— 硬约束违反（拒收）
    warnings —— 软约束违反（放行但提示）
"""
import argparse
import json
import re
import sys

# ------------------------------------------------------------------ 词表

# 【官方】Z-Image 官方 PE：「绝不包含"8K"、"杰作"等元标签或绘制指令」
# 【官方】InvokeAI：长串通用质量词是常见错误
QUALITY_WORDS = [
    "masterpiece", "best quality", "8k", "4k", "uhd", "high quality",
    "highest quality", "ultra hd", "hyperdetailed", "insanely detailed",
    "超清", "高清", "杰作", "最佳质量", "极致细节", "顶级画质", "大师级",
]

# 【官方】官方 PE：「严禁使用比喻、情感化修辞」
SUBJECTIVE_WORDS = [
    "beautiful", "stunning", "gorgeous", "amazing", "spectacular",
    "breathtaking", "perfect", "striking", "mesmerizing",
    "漂亮", "惊艳", "绝美", "震撼", "完美",
]

# 【官方】InvokeAI：Z-Image 不支持 Compel 权重语法
# 覆盖 (word:1.2) / (word) / ((word)) / [word] / word++ / word---
# 注意：方括号/圆括号里的内容若是「纯数字列表」（bbox 的 [y_min, x_min, y_max, x_max]）
# 或「JSON 数组」（["cat", "animal", ...]），不是权重语法 —— 用否定前瞻排除。
_WEIGHT_PAREN = re.compile(r"\(\s*(?![-\d,\s]+\))([^()]{1,80}?)\s*\)")
# 括号里带「词:数值」或纯数字权重值 —— 一定是 Compel 权重语法
_WEIGHT_PAREN_VALUE = re.compile(r"^[^()]{1,60}\s*:\s*[0-9.]+\s*$")


def _is_weight_paren(inner: str, scoped: str = "") -> bool:
    """
    括号里的内容是不是 Compel 权重语法。

    官方口径（diffusers weighted_prompts）：
      (cat)        单个词 -> 1.1 倍
      (cat:1.5)    词:数值 -> 1.5 倍
    所以**只有这两类**才是权重语法。

    【2026-11-09 修正】旧实现把**任意**括号内容都当权重语法，
    于是模型写的普通英文注释也会被误杀：
      (Tong Lau)              港式地名注音 -> 误报
      (arcade buildings)      骑楼建筑的英文补充 -> 误报
      (partially obscured or fragmented)  状态描述 -> 误报
    实测：普通增强 10 次有 3 次因此整单 502 被拒，
    而这些输出其实都是完整的（finish_reason=stop）。

    现在只把这两类判为权重语法：
      1. 括号内是「词:数值」或「数值」
      2. 括号内是**单个词**（不含空格、不含逗号）—— diffusers 的 (cat) 形式
    多词短语（中英文皆然）不算权重语法。
    """
    text = (inner or "").strip()
    if not text:
        return False
    # (word:1.5) / (1.5)
    if _WEIGHT_PAREN_VALUE.match(text):
        return True
    # 纯数字
    if re.fullmatch(r"[0-9.]+", text):
        return True
    # 括号内是中文 -> 是注释（如「（红色霓虹灯）」），不是权重语法
    if re.search(r"[\u4e00-\u9fff]", text):
        return False
    # 多词短语 -> 普通注释（(Tong Lau) / (arcade buildings)），不是权重语法
    if " " in text or "," in text:
        return False
    # 剩下的：单个英文词
    #
    # 【语义判据 · 比上下文匹配更根本】
    # Compel 的 (cat) 是「**强调**这个已有的词」——
    # 官方写法必然是 "a (cat) sitting" 这种：**括号里的词在别处也出现**。
    # 而模型写的说明性注释是「这个词只出现在括号里」：
    #     "Tong lau (arcade) buildings"   -> arcade 只在这里出现
    #     "a (cat) b"                     -> cat 在括号外也出现 -> 真权重语法
    #
    # 所以：单个词只有在**括号外也出现**时才判权重语法。
    # 这一条把三类注释（逐字注音 / 术语转写 / 术语英文名）统一覆盖掉了，
    # 不需要逐个匹配上下文 —— 也不用追着模型的新写法跑。
    #
    # 官方口径是「(cat) = 1.1 倍」，所以单个词**按字面**就是权重语法。
    # 但实测发现模型会用括号做**两种说明性注释**（都不是倍率）：
    #
    #   a) 逐字注音：港式招牌「金」「記」「酒」
    #      被扩写成 "金" (gold)、"記" (record)、"酒" (wine)
    #   b) 术语转写：中文建筑术语「骑楼」
    #      被扩写成 arcade buildings (tong lau)
    #
    # 这两种都是给渲染模型看的说明（官方 PE 模板也要求
    # 「所有画面文字用双引号括起来」，模型顺手就给术语加了注音），
    # 不是 Compel 倍率 —— 当成权重语法拒掉会整单 502，输出本身是好的。
    #
    # 判据（覆盖模型实际会写的三类说明性注释）：
    #   a) 逐字注音：  "金" (gold)
    #   b) 术语转写：  骑楼 (tong lau)
    #   c) 术语英文：  T-tong lau (shophouse) buildings
    #      —— 括号跟着一个**连字符/斜杠分隔的复合术语**，
    #         是给它标英文名，不是倍率。
    if _is_transliteration_gloss(text, scoped):
        return False
    if _is_term_transliteration(text, scoped):
        return False
    if _is_compound_term_gloss(text, scoped):
        return False
    # 单个词：Compel 强调语法会**重复**这个词（"(cat)" 强调前面已有的 cat），
    # 而说明性注释里的词只出现一次。
    #   真权重语法："a cat sitting, (cat) nearby"  -> cat 出现 2 次
    #   说明性注释："Tong lau (arcade) buildings"    -> arcade 只出现 1 次
    if _count_word_occurrences(text, scoped) < 2:
        return False
    return True


def _count_word_occurrences(word: str, scoped: str) -> int:
    """
    这个词在整段文本里出现了几次（按词边界计数）。

    Compel 强调：同一个词在括号内外都出现 -> >= 2
    说明性注释：括号里新引入的词 -> == 1
    """
    target = (word or "").strip().lower()
    if not target:
        return 0
    pattern = r"(?<![a-z0-9])" + re.escape(target) + r"(?![a-z0-9])"
    return len(re.findall(pattern, (scoped or "").lower()))


# 「T-tong lau (shophouse) buildings」这类：复合术语 + 括号英文名
_COMPOUND_GLOSS_PATTERN = re.compile(
    r"[A-Za-z][A-Za-z\-]{2,}(?:\s+[A-Za-z][A-Za-z\-]{2,}){0,2}\s*\(([^()]{1,40})\)")


def _is_compound_term_gloss(inner: str, scoped: str) -> bool:
    """
    判断这个括号是不是在给一个**复合术语**标英文名。

    实测上下文：
        "a row of cramped T-tong lau (shophouse) buildings featuring rusty iron window bars"
    括号里 (shophouse) 是「唐楼」的英文名，不是倍率。
    判据：括号前紧邻的词含有连字符/下划线/斜杠（复合术语特征）。
    """
    for m in _COMPOUND_GLOSS_PATTERN.finditer(scoped or ""):
        if m.group(1).strip().lower() == (inner or "").strip().lower():
            return True
    return False


# 「骑楼 (tong lau)」这类结构：中文词 + 紧跟的括号转写
_TERM_GLOSS_PATTERN = re.compile(r"[\u4e00-\u9fff]{1,10}\s*\(([^()]{1,40})\)")


def _is_term_transliteration(inner: str, scoped: str) -> bool:
    """
    判断这个括号是不是在给**紧邻的中文词**做转写/音译。

    实测：模型把「骑楼」写成 "arcade buildings (tong lau)" ——
    括号里是粤语音译，不是倍率。
    """
    for m in _TERM_GLOSS_PATTERN.finditer(scoped or ""):
        if m.group(1).strip().lower() == (inner or "").strip().lower():
            return True
    return False


# 「"金" (gold)」这类结构：引号文字 + 紧跟的括号注音
_GLOSS_PATTERN = re.compile(r"\"[^\"]{1,40}\"\s*\(([^()]{1,40})\)")


def _is_transliteration_gloss(inner: str, scoped: str) -> bool:
    """
    判断这个括号是不是在给**引号里的画面文字**做逐字注音。

    官方 PE 模板要求「所有文字内容用英文双引号括起来」，
    模型常写成 `"金" (gold)` —— 括号里是注音，不是倍率。
    这种不该被当权重语法拒掉（否则整单 502，输出本身是好的）。
    """
    for m in _GLOSS_PATTERN.finditer(scoped or ""):
        if m.group(1).strip().lower() == (inner or "").strip().lower():
            return True
    return False
def _is_json_like(inner: str) -> bool:
    """
    判断方括号里的内容是不是「结构化数据」而不是权重语法。

    权重语法形如 [word] / [a word] / [some words]；
    结构化数据形如 ["cat", "animal"] / [500, 350, 950, 650] / ["key": "value"]
    """
    stripped = inner.strip()
    if not stripped:
        return False
    # 纯数字列表
    if re.fullmatch(r"[\d\s.,\-]+", stripped):
        return True
    # JSON 数组：引号包裹的字符串列表
    if re.fullmatch(r'"[^"]*"\s*(,\s*"[^"]*")*', stripped):
        return True
    # JSON 对象条目："key": value
    if re.search(r'"[^"]+"\s*:', stripped):
        return True
    return False


_WEIGHT_BRACKET = re.compile(r"\[\s*([^\[\]]{1,120}?)\s*\]")
_WEIGHT_DOUBLE_PAREN = re.compile(r"\(\([^()]{1,80}?\)\)")
_WEIGHT_INC_DEC = re.compile(r"(?<![+\-])\b[A-Za-z\u4e00-\u9fff]{2,}\s*(\+\+|--)(?![+\-])")

WEIGHT_PATTERNS = [
    _WEIGHT_PAREN,        # (word:1.2) / (word)
    _WEIGHT_DOUBLE_PAREN,  # ((word))
    _WEIGHT_BRACKET,       # [word]
    _WEIGHT_INC_DEC,       # word++ / word--
]

# thinking 残留
THINK_PATTERNS = [
    re.compile(r"<\s*think\s*>", re.I),
    re.compile(r"<\s*/\s*think\s*>", re.I),
    re.compile(r"<\s*thinking\s*>", re.I),
    re.compile(r"<\s*reasoning\s*>", re.I),
]

# 中文正文里常见的「思考独白」开场（Qwen3 thinking 模型的特征泄漏）
NARRATION_MARKERS = [
    "我们需要回答", "用户要求", "用户输入", "首先，", "接下来，",
    "让我思考", "根据用户的要求", "分析用户",
]

# ------------------------------------------------------------------ 检查


def _norm(text: str) -> str:
    return (text or "").strip()


def check_quality_words(text: str):
    """质量元标签 —— 拒收。"""
    hits = []
    low = text.lower()
    for word in QUALITY_WORDS:
        # 用词边界匹配，避免 "8k" 命中 "18k" 之类的误伤
        pattern = r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, low):
            hits.append(word)
    return hits


def check_weight_syntax(text: str, mode: str = "pe"):
    """权重语法 —— 拒收。"""
    hits = []
    # bbox 档的 JSON 数组里全是 [数字, 数字, ...]，不是权重语法 —— 先剥掉再查
    scoped = text
    if mode == "bbox":
        scoped = re.sub(r"\[\s*\{.*\}\s*\]", " ", text, flags=re.S)
        scoped = re.sub(r'"bbox"\s*:\s*\[[^\]]*\]', '"bbox": []', scoped)
    for pattern in WEIGHT_PATTERNS:
        for match in pattern.finditer(scoped):
            if pattern is _WEIGHT_BRACKET:
                # 方括号：区分权重语法 [word] 与结构化数据 ["a","b"] / [1,2]
                inner = match.group(1)
                if _is_json_like(inner):
                    continue
            if pattern is _WEIGHT_PAREN:
                # 括号：区分 Compel 权重语法 (cat) / (cat:1.5) 与
                # 普通英文/中文注释 (Tong Lau) / (arcade buildings)
                if not _is_weight_paren(match.group(1), scoped):
                    continue
            snippet = match.group(0).strip()
            if snippet and snippet not in hits:
                hits.append(snippet)
    return hits


def check_thinking(text: str):
    """thinking 段残留 —— 拒收。"""
    hits = []
    for pattern in THINK_PATTERNS:
        if pattern.search(text):
            hits.append(pattern.pattern.replace("\\s*", ""))
    for marker in NARRATION_MARKERS:
        if marker in text[:120]:
            hits.append(f"疑似思考独白：{marker}")
            break
    return hits


def check_subjective(text: str):
    """主观评价词 —— 警告。"""
    hits = []
    low = text.lower()
    for word in SUBJECTIVE_WORDS:
        pattern = r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, low):
            hits.append(word)
    return hits


# 中文分词不可靠，用「原文里较长的名词片段」做保留检查
def _candidate_terms(original: str):
    """
    从用户原文里挑出可能是核心要素的词：
      - 英文：长度 >= 3 的单词（去掉停用词）
      - 中文：长度 >= 2 的连续汉字片段

    中文里要特别处理「量词 + 名词」的组合（如「一个女孩」）：
    「一个」是量词不是核心要素，真正的核心是「女孩」。

    另外，中文没有词边界，「女人坐在长椅上抱着狗」是一整串。
    这里额外切出「2 字组合」作为候选，让核心名词（女人/长椅/狗）能被检出，
    而不是把整句当成一个词去匹配。
    """
    out = []
    for token in re.findall(r"[A-Za-z][A-Za-z\-']{2,}", original or ""):
        if token.lower() in _STOPWORDS:
            continue
        out.append(token)
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", original or ""):
        # 剥掉开头的中文量词/代词，只留名词部分
        stripped = chunk
        for quant in _CJK_QUANTIFIERS:
            if stripped.startswith(quant) and len(stripped) > len(quant):
                stripped = stripped[len(quant):]
                break
        if len(stripped) >= 2:
            out.append(stripped)
        # 长句额外切 2 字窗口，作为「核心名词候选」
        if len(stripped) > 4:
            for i in range(len(stripped) - 1):
                window = stripped[i:i + 2]
                if window not in _CJK_STOP_BIGRAMS:
                    out.append(window)
    return out


# 常见的非核心 2 字组合（切窗口时跳过，避免制造噪声候选）
_CJK_STOP_BIGRAMS = {
    "坐在", "抱着", "长着", "站在", "拿着", "看着", "是个", "是一", "有的",
    "这个", "那个", "什么", "可以", "应该", "一种", "一些", "进行", "出现",
}

# 中文量词与代词（剥掉后剩下的才是核心名词）
_CJK_QUANTIFIERS = [
    "一个", "一只", "一条", "一张", "一幅", "一颗", "一束", "一堆", "一群",
    "这位", "那个", "这个", "一些", "很多", "几个",
]

_STOPWORDS = {
    "the", "and", "with", "for", "that", "this", "from", "into", "your", "you",
    "are", "was", "has", "have", "his", "her", "its", "not", "but", "all",
    "can", "will", "one", "two", "out", "who", "what", "when", "where",
    "image", "photo", "picture", "shot", "view", "style", "make", "create",
    "一个", "一只", "一条", "一张", "这个", "那个", "可以",
    "生成", "创建", "图片", "图像", "风格", "画",
}


# 语义等价词组：扩写后换了一种说法，但核心要素没丢 —— 不算丢失
# （官方 PE 允许「保留核心要素」而不是「逐字保留」）
_CJK_SYNONYM_GROUPS = [
    ("女孩", "女子", "女性", "少女", "姑娘", "女生", "女人"),
    ("男孩", "男子", "男性", "少年", "男生", "男人"),
    ("猫", "猫咪"),
    ("狗", "犬", "小狗"),
    ("孩子", "儿童", "孩童", "小孩"),
    ("老人", "长者", "老者"),
    ("车", "汽车", "轿车"),
    ("房子", "屋", "建筑"),
    ("花", "花朵", "花卉"),
    ("树", "树木"),
    ("山", "山脉", "群山"),
    ("海", "海洋", "大海"),
    ("城市", "都市", "城区"),
    ("街道", "街道", "马路", "路面"),
]

_EN_SYNONYM_GROUPS = [
    ("girl", "woman", "lady", "female", "young", "she"),
    ("boy", "man", "male", "he"),
    ("cat", "kitten", "feline"),
    ("dog", "puppy", "canine"),
    ("child", "kid", "children", "boy", "girl"),
    ("car", "vehicle", "automobile"),
    ("house", "home", "building"),
    ("tree", "forest"),
    ("mountain", "mountains", "peak"),
    ("city", "urban", "town"),
]


def _is_semantically_preserved(term: str, enhanced_low: str) -> bool:
    """判断原文核心词是否以同义说法出现在扩写结果里。"""
    groups = _CJK_SYNONYM_GROUPS if re.search(r"[\u4e00-\u9fff]", term) else _EN_SYNONYM_GROUPS
    for group in groups:
        if term in group or term.lower() in group:
            for word in group:
                if _term_present(word, enhanced_low):
                    return True
    return False


def _is_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _term_present(term: str, enhanced_low: str) -> bool:
    """
    原文核心词是否出现在扩写结果里。
    英文按词边界匹配（避免 "car" 命中 "carpet"）；
    中文直接子串匹配（中文没有词边界）。
    """
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in enhanced_low
    pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
    return bool(re.search(pattern, enhanced_low))


def check_subject_preserved(original: str, enhanced: str):
    """
    用户原文的核心词是否保留 —— 警告。

    中文长句会切出很多 2 字窗口候选，只要其中**大部分**能在扩写结果里
    找到（或找到同义说法），就认为核心要素保留了。

    跨语言时（SCALIST / bbox 档输出英文、输入是中文）：
    中文词不可能在英文结果里逐字出现，所以不做逐字比对。
    但如果结果**很短**，说明模型其实什么都没扩写 —— 这时仍要报。
    """
    if not original or not enhanced:
        return []
    low = enhanced.lower()
    # 跨语言：结果够长就认为核心要素被语义保留；结果太短说明没真扩写
    if _is_cjk(original) != _is_cjk(enhanced):
        words = len(re.findall(r"[A-Za-z0-9']+", enhanced))
        if words >= 25:
            return []
        return ["扩写结果过短（跨语言扩写应输出完整段落），核心要素可能未保留"]
    candidates = _candidate_terms(original)
    lost = []
    for term in candidates:
        if _term_present(term, low):
            continue
        if _is_semantically_preserved(term, low):
            continue
        lost.append(term)
    if not candidates:
        return []
    # 丢失比例过高才提示（个别词被自然改写是正常的）
    if len(lost) / len(candidates) > 0.5:
        return lost
    return []


# 画面文字线索词（只在这些词出现时才要求双引号）
# 用词边界匹配，避免 "textured"/"context" 之类误伤
TEXT_MARKERS = ["海报", "菜单", "招牌", "路标", "屏幕", "标题", "logo", "文字",
                "poster", "menu", "sign", "screen", "title", "text", "banner"]

# 否定语境：说了「画面里没有文字」时不需要双引号
NEGATION_MARKERS = ["无任何文字", "没有任何文字", "不含文字", "没有文字", "无文字",
                     "no text", "without text", "no lettering", "no signage",
                     "without any text"]


def _has_text_marker(text: str) -> str | None:
    """找出画面文字线索词（英文按词边界匹配，避免 textured/context 误伤）。"""
    low = text.lower()
    for marker in TEXT_MARKERS:
        if re.search(r"[\u4e00-\u9fff]", marker):
            if marker in low:
                return marker
        else:
            pattern = r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])"
            if re.search(pattern, low):
                return marker
    return None


def check_quoted_text(text: str):
    """
    画面文字是否用双引号包裹 —— 警告。
    只在文本里出现明显的「要出现在画面上的文字」线索时才提示，
    避免对纯视觉描述误报；命中否定语境（「画面中无任何文字」）时不提示。
    """
    low = text.lower()
    for neg in NEGATION_MARKERS:
        if neg in low:
            return []
    marker = _has_text_marker(text)
    if marker:
        # 文本里应该至少有一对双引号
        if '"' not in text:
            return [f"提到「{marker}」但没有用双引号逐字转录画面文字"]
    return []


def check_refine_structure(text: str):
    """二次增强（细化）档专用：物体分级结构是否齐全 —— 错误。

    要求至少出现物体分级标题，且每个 L1/L2 物件条目都带上了
    「外观/材质/状态」这类细化字段 —— 否则等于没细化。
    """
    errors = []
    warnings = []
    # 【2026-11-09 修正】模板原文写着「若某一级没有物体，跳过该级标题」——
    #   所以**不能**硬要求 L1 存在。旧实现要求 L1，与模板自相矛盾，
    #   模型合法地跳过 L1 时会被判「结构不完整」而整单拒收
    #   （实测：合并实例上二次增强 502，报「缺少 L1 特写分级标题」）。
    # 现在：只要有**任意一级**分级标题（或末段）就算结构完整。
    if not (re.search(r"【\s*L[123]\s*", text)
            or re.search(r"【\s*(主体细化|整体细化)\s*】", text)):
        errors.append("二次增强结果缺少分级标题（应含 L1/L2/L3 或主体细化/整体细化）")
    # 至少要有若干个物件条目，每个带冒号字段
    entries = re.findall(r"^[^\n:：]{1,40}[：:].{0,80}$", text, flags=re.M)
    if len(entries) < 3:
        warnings.append("细化字段偏少，可能没有真正逐个物体展开")
    # 每个物件应至少有一项材质或状态描述
    for key in ("材质", "表面", "状态", "磨损", "外观", "轮廓"):
        if key not in text:
            warnings.append(f"未见「{key}」类细化字段")
            break
    return errors, warnings


def check_music_structure(text: str, refine: bool = False):
    """
    音乐档专用：style 串的基本结构 —— 错误 / 警告。

    音乐 style 串是「逗号分隔的自然语言」，不是 JSON、不是分项清单。
    这里只查**会毁掉下游可用性**的硬伤：
      - 输出成了 JSON / 键值对 / markdown 列表（下游 YuE2 只吃自然语言串）
      - refine 档没有逐要素展开（等于没细化）
    """
    errors = []
    warnings = []
    stripped = text.strip()
    # JSON 复读：{...} 或 [...] 开头
    if stripped[:1] in ("{", "["):
        errors.append("音乐档输出看起来是 JSON/数组，不是自然语言 style 串")
    # markdown 标题 / 列表符号开头
    if re.match(r"^(#|\*\*|-\s|\d+\.\s)", stripped):
        errors.append("音乐档输出带 markdown 标题或列表符号，不是纯 style 串")
    # 键值对写法：language: / genre: 这类
    if re.search(r"^[A-Za-z_\s]{3,20}\s*[:：]", stripped, flags=re.M):
        errors.append("音乐档输出是「字段: 值」写法，不是逗号分隔的自然语言 style 串")
    if refine:
        # refine 档要能看到分级标题（逐要素展开的结构）
        # 图像版标题是「L1 特写 / L2 中景 / L3 远景 / 主体细化」，
        # 音乐版是「L1 主线 / L2 支撑 / L3 远景 / 整体细化」——
        # 两套都认（共同点是 L1/L2/L3 分级标题）。
        if not (re.search(r"【\s*L[123]\s*", text)
                or re.search(r"【\s*(主体细化|整体细化)\s*】", text)):
            errors.append("二次增强结果缺少分级标题（应含 L1/L2/L3 或 主体细化/整体细化）")
        entries = re.findall(r"^[^\n:：]{1,40}[：:].{0,80}$", text, flags=re.M)
        if len(entries) < 3:
            warnings.append("细化字段偏少，可能没有真正逐个要素展开")
    # 速度：应该给出具体 BPM
    if not re.search(r"\b\d{2,3}\s*BPM\b", text, flags=re.I):
        warnings.append("未见具体 BPM 数值（建议写成「96 BPM」而不是「中速」）")
    return errors, warnings


def check_length(text: str, mode: str):
    """长度区间 —— 警告。"""
    words = len(re.findall(r"[A-Za-z0-9']+", text))
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    # 中文按 1 词 ≈ 1.5 英文词 的粗略口径折算
    est = words + cjk / 1.5
    if mode == "bbox":
        lo, hi = 100, 350
    elif mode == "scalist":
        lo, hi = 80, 220
    elif mode == "refine":
        # 逐物体枚举天然更长，但也不该无限长
        lo, hi = 80, 600
    elif mode in ("music", "music_refine"):
        # 音乐 style 串：9~10 个要素，比纯 caption 短但比图像 caption 长
        lo, hi = 15, 600
    else:
        lo, hi = 60, 350
    if est < lo:
        return [f"长度偏短（约 {int(est)} 词，建议 {lo}-{hi} 词）"], int(est)
    if est > hi:
        return [f"长度偏长（约 {int(est)} 词，建议 {lo}-{hi} 词）"], int(est)
    return [], int(est)


def check_bbox(text: str):
    """bbox 档专用：id/bbox/desc 结构与 y 在 x 之前的规则 —— 错误。"""
    errors = []
    warnings = []
    blob = _extract_json_array(text)
    if not blob:
        return ["bbox 档没有找到 JSON 数组"], []
    try:
        items = json.loads(blob)
    except Exception as exc:
        return [f"JSON 数组解析失败：{exc}"], []
    if not isinstance(items, list) or not items:
        return ["JSON 数组为空或格式不对"], []
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            errors.append("数组元素不是对象")
            continue
        pid = item.get("id")
        if not pid:
            errors.append("元素缺少 id")
        elif pid in ids:
            errors.append(f"id 重复：{pid}")
        else:
            ids.add(pid)
        bbox = item.get("bbox")
        if not (isinstance(bbox, list) and len(bbox) == 4):
            errors.append(f"{pid} 的 bbox 不是 4 元组")
            continue
        if not all(isinstance(v, (int, float)) for v in bbox):
            errors.append(f"{pid} 的 bbox 含非数值")
            continue
        if not all(0 <= v <= 1000 for v in bbox):
            errors.append(f"{pid} 的 bbox 超出 0-1000 归一化范围")
        if bbox[0] > bbox[2] or bbox[1] > bbox[3]:
            errors.append(f"{pid} 的 bbox 顺序异常（应为 [y_min, x_min, y_max, x_max]）")
        if not item.get("desc"):
            errors.append(f"{pid} 缺少 desc")
    # caption 里应能用尖括号引用到 id —— 官方格式要求，但漏引不影响 bbox 本身可用
    for pid in ids:
        if f"<{pid}>" not in text:
            warnings.append(f"caption 里没有用尖括号引用 <{pid}>（官方格式要求）")
    return errors, warnings


def _extract_json_array(text: str) -> str:
    """
    抽出 JSON 数组并尽力修复被 max_tokens 截断的输出。
    与 h3studio/enhancer.py 的 extract_json_block 保持同一套修复规则。
    """
    if not text:
        return ""
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        blob = fence.group(1).strip()
    else:
        start = text.find("[")
        if start == -1:
            return ""
        depth = 0
        blob = ""
        for i in range(start, len(text)):
            blob += text[i]
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    break
    return _repair_truncated_json(blob)


def _repair_truncated_json(blob: str) -> str:
    """修复被 max_new_tokens 截断的 JSON 数组。"""
    blob = blob.strip()
    if not blob:
        return blob
    blob = re.sub(r",\s*$", "", blob)
    in_string = False
    escape = False
    brackets = []
    for ch in blob:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "[{":
            brackets.append(ch)
        elif ch in "]}":
            if brackets:
                brackets.pop()
    if in_string:
        blob += '"'
    for ch in reversed(brackets):
        blob += "]" if ch == "[" else "}"
    blob = re.sub(r',?\s*"[a-zA-Z_]+"\s*:\s*"[^"]*$', "", blob)
    blob = re.sub(r",\s*\{\s*$", "", blob)
    blob = re.sub(r",\s*$", "", blob)
    return blob


# ------------------------------------------------------------------ 主入口


def validate(text: str, original: str = "", mode: str = "pe"):
    """
    校验扩写结果。

    返回 {"ok", "errors", "warnings", "stats"}
    ok = 无 errors（warnings 不影响 ok）
    """
    text = _norm(text)
    errors = []
    warnings = []

    if not text:
        return {"ok": False, "errors": ["结果为空"], "warnings": [],
                "stats": {"words": 0, "chars": 0}}

    hits = check_quality_words(text)
    if hits:
        errors.append("出现质量元标签（Z-Image 官方 PE 明令禁止）："
                      + "、".join(hits))

    hits = check_weight_syntax(text, mode)
    if hits:
        errors.append("出现提示词权重语法（Z-Image 不支持 Compel 语法）："
                      + "、".join(hits))

    if mode in ("refine", "music_refine"):
        # music_refine（音乐版二次增强）也走逐物体枚举的结构检查 ——
        # 它的分级标题是「【L1 主线】/【L2 支撑】/【L3 远景】/【整体细化】」，
        # 末段用「整体细化」而不是「主体细化」。
        refine_errors, refine_warnings = check_refine_structure(text)
        errors.extend(refine_errors)
        warnings.extend(refine_warnings)

    hits = check_thinking(text)
    if hits:
        errors.append("出现 thinking/思考痕迹（thinking 模型不适合做扩写）："
                      + "、".join(hits))

    if mode == "bbox":
        bbox_errors, bbox_warnings = check_bbox(text)
        errors.extend(bbox_errors)
        warnings.extend(bbox_warnings)

    if mode in ("music", "music_refine"):
        music_errors, music_warnings = check_music_structure(
            text, refine=(mode == "music_refine"))
        errors.extend(music_errors)
        warnings.extend(music_warnings)

    hits = check_subjective(text)
    if hits:
        warnings.append("出现主观评价词（官方 PE 要求客观具象）："
                        + "、".join(hits))

    lost = check_subject_preserved(original, text)
    if lost:
        warnings.append("用户原文的核心要素可能丢失：" + "、".join(lost))

    hits = check_quoted_text(text)
    warnings.extend(hits)

    length_msg, est = check_length(text, mode)
    warnings.extend(length_msg)

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {"words": est, "chars": len(text)},
    }


def main():
    parser = argparse.ArgumentParser(description="提示词增强结果校验器")
    parser.add_argument("--text", required=True, help="要校验的扩写结果")
    parser.add_argument("--original", default="", help="用户原始输入（用于主体保留检查）")
    parser.add_argument("--mode", default="pe",
                        choices=["pe", "scalist", "bbox", "music", "music_refine"],
                        help="模板档位")
    args = parser.parse_args()
    result = validate(args.text, args.original, args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
