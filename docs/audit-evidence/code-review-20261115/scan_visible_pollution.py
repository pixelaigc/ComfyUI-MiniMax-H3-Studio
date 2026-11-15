#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
扫描 web/ 前端文件里的「用户可见文案污染」。

判定思路：
  1. 先把「会进入用户界面」的赋值点摘出来（innerHTML / textContent / placeholder /
     title / aria-label / setAttribute / toast / notify / alert / confirm 等）。
  2. 再对这些赋值点的字符串字面量 + 模板字符串静态片段做污染标记匹配。
  3. 模板字符串里如果整段是 HTML 注释块（<!-- ... -->），也算污染（会被当文本渲染或
     至少是开发注释残留）。

污染标记分两类：
  HARD  = 无争议的开发者痕迹（TODO/FIXME/XXX/HACK/debug/内部日期标记/交付项编号等）
  SOFT  = 需要人工判断的调试味文案（测试/临时/占位/假数据/mock 等）

只读脚本：不写任何被审文件。
"""
import json
import os
import re
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else "/root/projects/h3s-cr-20261115"
WEB = os.path.join(ROOT, "web")

# ---------------------------------------------------------------- 污染标记
HARD_PATTERNS = [
    (r"\bTODO\b", "TODO"),
    (r"\bFIXME\b", "FIXME"),
    (r"\bXXX\b", "XXX"),
    (r"\bHACK\b", "HACK"),
    (r"\bWIP\b", "WIP"),
    (r"\bdebug\b", "debug"),
    (r"\bDEBUG\b", "DEBUG"),
    (r"【\d{4}-\d{2}-\d{2}", "内部日期标记 【YYYY-MM-DD】"),
    (r"交付项\s*\d", "交付项编号"),
    (r"【\d{4}-\d{2}-\d{2}[^】]*交付", "交付标记"),
    (r"\bcommit\s+[0-9a-f]{7,}", "commit hash"),
    (r"\bgit\s+checkout\b", "git 命令"),
    (r"\bconsole\.log\b", "console.log"),
    (r"\bprint\s*\(", "print()"),
    (r"内部实现|内部逻辑|仅供内部|内部测试|调试用|测试用|开发用|临时方案|临时 hack",
     "内部/调试措辞"),
    (r"先别|暂时先|先这么|先这样", "临时口吻"),
    (r"接口待定|待定|待联调|待对接|待补充|待完善|未实现|尚未实现", "未完成措辞"),
    (r"暂不支持|不支持该|功能缺失", "未支持措辞"),
    (r"示例代码|伪代码|pseudo", "示例/伪代码"),
    (r"\bfoo\b|\bbar\b|\bbaz\b", "占位变量 foo/bar/baz"),
    (r"asdf|qwerty|lorem ipsum", "占位文本"),
]

SOFT_PATTERNS = [
    (r"测试|test|Test", "含 test/测试"),
    (r"占位|placeholder|示意|样例|example", "占位/示例措辞"),
    (r"mock|fake|dummy|stub", "mock/fake 措辞"),
    (r"暂时|临时", "临时措辞"),
    (r"你可以在|请注意：调试|开发环境", "开发者提示"),
]

# ------------------------------------------------------- 用户可见赋值点
# (正则, 类型标签, 该表达式里哪些子串是「用户可见文案」)
ASSIGN_PATTERNS = [
    (r"\.innerHTML\s*(?:\+)?=\s*", "innerHTML"),
    (r"\.outerHTML\s*(?:\+)?=\s*", "outerHTML"),
    (r"\.textContent\s*(?:\+)?=\s*", "textContent"),
    (r"\.innerText\s*(?:\+)?=\s*", "innerText"),
    (r"\.placeholder\s*=\s*", "placeholder"),
    (r"\.title\s*=\s*", "title"),
    (r"\.setAttribute\(\s*['\"](?:title|aria-label|alt|placeholder)['\"]\s*,", "attr"),
    (r"\.setAttribute\(\s*['\"]aria-[\w-]+['\"]\s*,", "aria"),
    (r"\.ariaLabel\s*=\s*", "ariaLabel"),
    (r"\bdocument\.title\s*=\s*", "document.title"),
    (r"\balert\s*\(", "alert"),
    (r"\bconfirm\s*\(", "confirm"),
    (r"\bprompt\s*\(", "prompt"),
]

# 提示条 / toast 类函数名（把这些函数调用的实参也当用户可见）
TOAST_FN = re.compile(
    r"\b(showToast|toast|notify|notifyUser|showMsg|showMessage|showError|showInfo|"
    r"showWarn|showSuccess|flashMsg|setStatus|setHint|setBanner|showBanner|"
    r"showAlert|showNotice|pushToast|toastMsg|msg|message)\s*\(",
    re.IGNORECASE,
)

STRING_LITERAL = re.compile(r"""(?P<q>['"`])(?P<body>(?:\\.|(?!\1).)*?)(?P=q)""", re.S)


def find_string_literals(expr):
    """从一段 JS 表达式里抽出所有字符串字面量/模板字符串的静态片段。"""
    out = []
    for m in STRING_LITERAL.finditer(expr):
        body = m.group("body")
        if m.group("q") == "`":
            # 模板字符串：去掉 ${...} 插值部分，只留静态文本
            body = re.sub(r"\$\{[^}]*\}", "\x00", body)
        out.append((m.group("q"), body, m.start()))
    return out


def scan_expr(expr, tag, lineno, fname, findings, seen):
    for q, body, _ in find_string_literals(expr):
        if not body.strip():
            continue
        # 整段是 HTML 注释 -> 开发注释被拼进 UI
        if re.match(r"^\s*<!--", body) and "-->" in body:
            findings.append({
                "file": fname, "line": lineno, "type": tag, "severity": "HARD",
                "marker": "HTML 注释块进入 UI",
                "text": body.strip()[:220],
                "why": "整段 HTML 注释被赋给 %s，会作为注释节点进入 DOM（可能被 innerHTML 拼进可见区域）" % tag,
            })
            continue
        for pat, name in HARD_PATTERNS:
            if re.search(pat, body, re.IGNORECASE):
                findings.append({
                    "file": fname, "line": lineno, "type": tag, "severity": "HARD",
                    "marker": name,
                    "text": body.strip()[:220],
                    "why": "字符串字面量含开发者痕迹「%s」，经 %s 进入用户界面" % (name, tag),
                })
                break


def scan_js(path):
    findings = []
    fname = os.path.relpath(path, ROOT)
    src = open(path, encoding="utf-8", errors="replace").read()
    lines = src.splitlines()
    # 逐行 + 跨行拼接（模板字符串常跨行）
    for pat, tag in ASSIGN_PATTERNS:
        for m in re.finditer(pat, src):
            # 往后取到语句结束（分号或换行+缩进结束），最多 3000 字符
            tail = src[m.end(): m.end() + 3000]
            # 截断到语句边界
            cut = re.search(r";(?![^\n]*[)])", tail)
            if cut:
                tail = tail[: cut.start()]
            lineno = src.count("\n", 0, m.start()) + 1
            scan_expr(tail, tag, lineno, fname, findings, None)
    for m in TOAST_FN.finditer(src):
        lineno = src.count("\n", 0, m.start()) + 1
        tail = src[m.end(): m.end() + 600]
        scan_expr(tail, "toast", lineno, fname, findings, None)
    return findings


HTML_TEXT = re.compile(r">([^<>]+)<")
HTML_ATTR = re.compile(r"""\b(placeholder|title|aria-label|alt)\s*=\s*"([^"]*)"|\b(placeholder|title|aria-label|alt)\s*=\s*'([^']*)'""")
HTML_COMMENT = re.compile(r"<!--(.*?)-->", re.S)


def scan_html(path):
    findings = []
    fname = os.path.relpath(path, ROOT)
    src = open(path, encoding="utf-8", errors="replace").read()
    # 文本节点
    for m in HTML_TEXT.finditer(src):
        text = m.group(1)
        if not text.strip():
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        for pat, name in HARD_PATTERNS:
            if re.search(pat, text, re.IGNORECASE):
                findings.append({
                    "file": fname, "line": lineno, "type": "html-text", "severity": "HARD",
                    "marker": name, "text": text.strip()[:220],
                    "why": "HTML 文本节点含开发者痕迹「%s」，直接可见" % name,
                })
                break
    for m in HTML_ATTR.finditer(src):
        val = m.group(2) or m.group(4) or ""
        if not val.strip():
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        for pat, name in HARD_PATTERNS:
            if re.search(pat, val, re.IGNORECASE):
                findings.append({
                    "file": fname, "line": lineno, "type": "html-attr", "severity": "HARD",
                    "marker": name, "text": val.strip()[:220],
                    "why": "HTML 属性含开发者痕迹「%s」，用户可见（hover / 占位符 / 无障碍名）" % name,
                })
                break
    return findings


def main():
    findings = []
    for fn in sorted(os.listdir(WEB)):
        p = os.path.join(WEB, fn)
        if not os.path.isfile(p):
            continue
        if fn.endswith(".js"):
            findings += scan_js(p)
        elif fn.endswith(".html"):
            findings += scan_html(p)
    # 去重
    uniq = {}
    for f in findings:
        k = (f["file"], f["line"], f["marker"], f["text"])
        uniq[k] = f
    res = sorted(uniq.values(), key=lambda x: (x["file"], x["line"]))
    print(json.dumps({"root": ROOT, "count": len(res), "findings": res},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
