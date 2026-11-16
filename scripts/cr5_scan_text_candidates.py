#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第5轮 A 段 —— 前端用户可见文案污染：词法级全量扫描。

【为什么要自己写分词器】
用正则抽字符串字面量是**不可靠**的：模板串内部也含引号
（`<div class="enhance-warn">`），正则会把模板串静态文本里的 `"`
当成字符串起点，一路吞到下一个 `"`，把中间的注释和代码全算进来
—— 第5轮实测就因此产生假阳性。

所以这里走**统一分词器**：
  * 逐字符走一遍 JS 源码，正确处理
    「行注释 / 块注释 / 字符串 / 模板串 / 正则字面量」；
  * 直接产出「字符串与模板串的静态文本片段 + 精确位置」；
  * 注释与正则字面量从源头剔除（不是事后猜）。

【四道扫描面】
  1. 所有字符串/模板串字面量（宽口径候选池，避开「只扫赋值点同一语句」
     漏掉「对象字面量定义文案 -> 后续渲染进 DOM」的间接路径）
  2. HTML 文本节点 + placeholder/title/aria-label/alt 属性值
  3. innerHTML/outerHTML 模板里嵌的 HTML 注释（不渲染但进 DOM，
     「查看源代码 / 复制页面内容」能带走）
  4. index.html 的静态注释块（单独统计：用户已判定「渲染不可见，
     不算缺陷」，这里只做计数留证）

只读脚本：不写任何被审文件。
"""
import json
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3s-cr5"
WEB = os.path.join(ROOT, "web")

# ------------------------------------------------------------------ 词表
DEV_WORDS = [
    (r"TODO|FIXME|\bXXX\b|\bHACK\b|\bWIP\b", "todo-marker"),
    (r"\bdebug\b|\bDEBUG\b|\bconsole\.log\b|\bconsole\.warn\b|\bconsole\.error\b", "debug-trace"),
    # 【为什么不用裸的「占位符」】
    # 「User 模板必须包含 {prompt} 或 {original} 占位符」是**正常的用户可见
    # 错误提示**（实测存在于 providers.js）—— 「占位符」在这里是官方模板
    # 语法的术语，不是开发者内部说法。
    # 真正要抓的是「占位文字 / 占位文本」这类描述**自己写的临时文案**的说法。
    (r"占位文字|占位文本|此处占位|placeholder text|示意文字|占位用的", "placeholder-text"),
    (r"mock|fake|dummy|stub|假数据|模拟数据", "mock"),
    (r"对齐布局|对齐用|仅用于对齐|供测试|测试用|调试用|开发用|内部用", "internal-only"),
    (r"内部实现|内部逻辑|仅供内部|内部测试|内部状态|内部调试", "internal-only"),
    (r"待定|待联调|待对接|待补充|待完善|未实现|尚未实现|不实现", "unfinished"),
    (r"先别|暂时先|先这么|先这样", "temp-tone"),
    (r"【\d{4}-\d{2}-\d{2}", "internal-date"),
    (r"交付项|第\s*\d+\s*轮|缺陷编号|审查\s*[A-Z]-\d+", "internal-ref"),
    (r"\bcommit\s+[0-9a-f]{7,}|\bgit\s+checkout\b", "git-trace"),
    (r"\bfoo\b|\bbar\b|\bbaz\b|\bqux\b", "placeholder-var"),
    (r"asdf|qwerty|lorem ipsum|测试文本|示例文本", "dummy-text"),
    (r"Traceback \(most recent|stacktrace", "stacktrace"),
    (r"这个函数|该函数|注意这里|看这里|改这里|删掉这行|忘了写|没写完", "code-note"),
]

# 【为什么「临时 / 暂时」不在上面那张表里】
# 「暂时无法确认任务是否仍在运行」这类**正常用户提示语**实测存在，
# 一旦把「暂时」当污染标记，正常文案会被淹没（假阳性）。
# 真正要抓的是「临时文字」「临时占位」这类**开发者内部说法**，
# 所以单独用更精确的短语表：
DEV_WORDS += [
    (r"临时文字|临时占位|临时文案|临时内容|临时字符串|临时方案|临时 hack", "temporary"),
]

# CSS 类名属性值 —— 不是用户可见文案，匹配前先抹掉
_CSS_ATTR_RE = re.compile(r"""\b(class|id)\s*=\s*["'][^"']*["']""")


def _strip_css_attrs(text: str) -> str:
    """抹掉 class/id 属性值（保留标签骨架）。

    【为什么】CSS 类名 `bar` / `bar-label` / `progress-bar` / `up-bar`
    撞上 `\\bbar\\b` -> 被误报成「占位变量 foo/bar/baz」。
    类名既不是用户可见文案，也不该参与污染判定。
    """
    return _CSS_ATTR_RE.sub(lambda m: f'{m.group(1)}=""', text)


def _is_css_class_only(text: str) -> bool:
    """整段文本是否只是一个 CSS 类名 / 选择器 / 类名列表。

    【判据】文本里除了类名本身没有别的可见字符。
    例：`bar` / `bar danger` / `bar-label` -> True
        `.prompt-enhance-bar` -> True（CSS 选择器）
        `<div class="bar-label">x</div>` -> False（含标签与文本）
    """
    t = text.strip()
    if not t:
        return False
    if "<" in t or ">" in t:
        return False
    m = re.match(r"^class\s*=\s*[\"']([^\"']*)[\"']$", t)
    if m:
        t = m.group(1).strip()
    if not t:
        return False
    # CSS 选择器：`.foo-bar` / `#foo` / `div.foo`
    if re.fullmatch(r"[.#]?[A-Za-z_][\w-]*", t):
        return True
    # 类名列表
    return all(re.fullmatch(r"[A-Za-z_][\w-]*", tok) for tok in t.split())


def _match_dev_words(text: str):
    """在文本里找开发者措辞，命中返回 (marker, 命中的片段)，否则 None。

    CSS 类名不参与判定。
    """
    if _is_css_class_only(text):
        return None
    target = _strip_css_attrs(text)
    for pat, name in DEV_WORDS:
        m = re.search(pat, target)
        if m:
            return name, m.group(0)
    return None

# / 何时是正则字面量（而非除号）—— 看前一个有效 token
REGEX_ALLOWED_BEFORE = re.compile(
    r"(?:^|[=(,:\[!&|?{};+\-*%<>~^]|\breturn\b|\bcase\b|\btypeof\b|\binstanceof\b|\bnew\b|\bdo\b|\belse\b)$"
)


class Tokenizer:
    """把 JS 源码切成三类：literal(字符串/模板串) / code / 其他。"""

    def __init__(self, src):
        self.src = src
        self.literals = []   # (start, end, quote, body)
        self.templates = []  # innerHTML=...`...` 模板（起始位置）

    def _skip_interpolation(self, src, i):
        """扫描 `${...}` 插值内部的内容，返回**闭合 `}` 的下标**（不含）。

        插值内部可以再嵌套字符串 / 模板串 / 注释 / 对象字面量 `{...}`，
        这里只保证「反引号与花括号配对正确」，不做字面量收集。

        【为什么要单独返回 `}` 的位置而不是越过它】
        调用方（_scan_template）靠这个 `}` 把 depth 减 1。
        若在这里就把 `}` 吃掉，depth 永远回不到 0 ->
        整个文件剩余部分都被当成模板串内容 -> 扫描结果全错。
        """
        n = len(src)
        while i < n:
            ch = src[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "/" and i + 1 < n and src[i + 1] == "/":
                j = src.find("\n", i)
                i = n if j == -1 else j
                continue
            if ch == "/" and i + 1 < n and src[i + 1] == "*":
                j = src.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
            if ch in "\"'":
                q = ch
                i += 1
                while i < n:
                    if src[i] == "\\":
                        i += 2
                        continue
                    if src[i] == q:
                        i += 1
                        break
                    if src[i] == "\n":
                        break
                    i += 1
                continue
            if ch == "`":
                # 嵌套模板串 —— 递归处理
                sub = Tokenizer.__new__(Tokenizer)
                sub.src = src
                sub.literals = []
                sub.templates = []
                i = sub._scan_template(i)
                continue
            if ch == "{":
                # 嵌套对象字面量 —— 跳过它自己那一层
                nested = self._skip_interpolation(src, i + 1)
                i = nested + 1 if nested < n else n
                continue
            if ch == "}":
                return i
            i += 1
        return n

    def _scan_template(self, i):
        """从反引号位置 i 开始，返回模板串**结束之后**的位置。"""
        src, n = self.src, len(self.src)
        j, depth = i + 1, 0
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "$" and j + 1 < n and src[j + 1] == "{":
                depth += 1
                j += 2
                continue
            if depth > 0:
                close = self._skip_interpolation(src, j)
                if close >= n:
                    return n
                depth -= 1
                j = close + 1
                continue
            if ch == "`":
                return j + 1
            j += 1
        return n

    def run(self):
        src, n = self.src, len(self.src)
        i = 0
        prev = ""          # 上一个有效字符
        prev_word = ""     # 上一个有效单词
        while i < n:
            c = src[i]
            # 行注释
            if c == "/" and i + 1 < n and src[i + 1] == "/":
                j = src.find("\n", i)
                i = n if j == -1 else j
                continue
            # 块注释
            if c == "/" and i + 1 < n and src[i + 1] == "*":
                j = src.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
            # 正则字面量
            if c == "/" and (not prev or REGEX_ALLOWED_BEFORE.search(prev_word or prev)):
                j, in_class, ok = i + 1, False, False
                while j < n:
                    ch = src[j]
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        ok = True
                        break
                    elif ch == "\n":
                        break
                    j += 1
                if ok:
                    j += 1
                    while j < n and src[j].isalpha():
                        j += 1
                    prev, prev_word = "x", "x"
                    i = j
                    continue
            # 字符串
            if c == '"' or c == "'":
                q = c
                j = i + 1
                body_start = j
                while j < n:
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == q:
                        break
                    if src[j] == "\n":     # 未闭合（异常代码）—— 到行尾为止
                        break
                    j += 1
                body = src[body_start:min(j, n)]
                self.literals.append((i, min(j, n) + 1, q, body))
                prev, prev_word = "x", "x"
                i = min(j, n) + 1
                continue
            # 模板串 —— 必须处理 ${} 里的**嵌套模板串**
            # （真实案例：providers.js 里 `${p.has_api_key ? `<span ...>${x}</span>` : ""}`）
            # 若不递归处理，内层反引号会提前终止外层模板串，
            # 后续整段代码被当成字面量 -> 大量假阳性。
            if c == "`":
                end = self._scan_template(i)
                body = src[i + 1:max(i + 1, end - 1)]
                self.literals.append((i, end, "`", body))
                prev, prev_word = "x", "x"
                i = end
                continue
            # 普通字符
            if not c.isspace():
                prev = c
                if c.isalnum() or c == "_":
                    k = i
                    while k < n and (src[k].isalnum() or src[k] == "_"):
                        k += 1
                    prev_word = src[i:k]
                    prev = src[k - 1]
                    i = k
                    continue
                prev_word = ""
            i += 1
        return self.literals


def scan_js(path):
    out = []
    fname = os.path.relpath(path, ROOT)
    raw = open(path, encoding="utf-8", errors="replace").read()
    tk = Tokenizer(raw)
    lits = tk.run()
    for start, end, q, body in lits:
        text = body
        if q == "`":
            text = re.sub(r"\$\{[^}]*\}", "\x00", text)
        if not text.strip() or len(text) > 400:
            continue
        lineno = raw.count("\n", 0, start) + 1
        hit = _match_dev_words(text)
        if hit:
            out.append({"file": fname, "line": lineno, "marker": hit[0],
                        "text": text.strip()[:200]})
    return out


def scan_js_html_comment_in_template(path):
    """innerHTML / outerHTML 赋值模板里嵌的 HTML 注释。"""
    out = []
    fname = os.path.relpath(path, ROOT)
    raw = open(path, encoding="utf-8", errors="replace").read()
    # 只在模板串内部找注释（模板串由分词器给出精确区间）
    tk = Tokenizer(raw)
    lits = tk.run()
    for start, end, q, body in lits:
        if q != "`":
            continue
        # 该模板串前面最近的赋值目标
        head = raw[max(0, start - 200):start]
        if not re.search(r"\.(?:innerHTML|outerHTML)\s*(?:\+)?=\s*$", head):
            continue
        for cm in re.finditer(r"<!--(.*?)-->", body, re.S):
            lineno = raw.count("\n", 0, start + cm.start()) + 1
            out.append({
                "file": fname, "line": lineno, "marker": "html-comment-in-template",
                "text": cm.group(1).strip()[:200],
            })
    return out


HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)


def scan_html(path):
    out = []
    fname = os.path.relpath(path, ROOT)
    raw = open(path, encoding="utf-8", errors="replace").read()
    src = HTML_COMMENT.sub(lambda m: "\x00" * len(m.group(0)), raw)
    for m in re.finditer(r">([^<>]+)<", src):
        text = m.group(1)
        if not text.strip():
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        hit = _match_dev_words(text)
        if hit:
            out.append({"file": fname, "line": lineno, "marker": hit[0],
                        "text": text.strip()[:200]})
    for m in re.finditer(
        r"""\b(placeholder|title|aria-label|alt|data-title)\s*=\s*"([^"]*)"|\b(placeholder|title|aria-label|alt|data-title)\s*=\s*'([^']*)'""",
        src,
    ):
        val = m.group(2) or m.group(4) or ""
        if not val.strip():
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        hit = _match_dev_words(val)
        if hit:
            out.append({"file": fname, "line": lineno, "marker": hit[0],
                        "text": val.strip()[:200]})
    return out


def count_html_comments(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    return len(HTML_COMMENT.findall(raw))


def main():
    findings = []
    html_comments = {}
    for fn in sorted(os.listdir(WEB)):
        p = os.path.join(WEB, fn)
        if not os.path.isfile(p):
            continue
        if fn.endswith(".js"):
            findings += scan_js(p)
            findings += scan_js_html_comment_in_template(p)
        elif fn.endswith(".html"):
            findings += scan_html(p)
            html_comments[fn] = count_html_comments(p)
    uniq = {}
    for f in findings:
        uniq[(f["file"], f["line"], f["marker"], f["text"])] = f
    res = sorted(uniq.values(), key=lambda x: (x["file"], x["line"]))
    print(json.dumps({"root": ROOT, "count": len(res), "findings": res,
                      "static_html_comments": html_comments},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
