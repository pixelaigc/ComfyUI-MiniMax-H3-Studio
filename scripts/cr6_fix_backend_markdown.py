# -*- coding: utf-8 -*-
"""第 6 轮审查：把**用户可见**的 markdown 强调残留 `**xxx**` 换成中文引号。

【为什么用「按行文本替换」而不是 AST 改写】
Python 的相邻字符串字面量会被隐式拼接：
    note=("第一段 "
          "第二段 **强调** "
          "第三段")
AST 改写要按 (lineno, col_offset) 定位并**重排**字节偏移 ——
一旦某个字面量在同一行里、或跨行拼接，就会把相邻片段错位
（本轮实测：AST 版把 model_recommendations.py 的 note 块改坏了，
字符串被复制成两份）。

所以这里改用**纯文本行替换**：
  * 只在**字符串字面量内部**做 `**xxx**` -> `「xxx」` 的替换；
  * 用「该行的引号配对」判断哪些部分属于字符串；
  * 注释与代码不动。

【口径】
  * 只改 UI 模块（文案会经 API 进浏览器）；
  * 只改**字符串字面量**里的部分（注释里的 `**xxx**` 不动 —— 那是
    本项目的注释记法，不是污染）；
  * docstring 不动。

用法：
    python3 scripts/cr6_fix_backend_markdown.py --dry-run
    python3 scripts/cr6_fix_backend_markdown.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MD = re.compile(r"\*\*(?P<body>[^*\n]{1,60}?)\*\*")

UI_MODULES = (
    "model_recommendations.py", "lora_catalog_mihoyo.py",
    "lora_catalog_ea.py", "lora_catalog_nsfw.py", "lora_catalog_effects.py",
    "job_meta.py", "h3image.py", "continuation.py", "continuation_service.py",
    "h3_audio_continuation.py", "h3_video_size.py", "reso_label.py",
    "h3_video_modes.py", "h3_audio_track.py", "seed_guard.py", "video_models.py",
)

#: 官方提示词模板原文（本来就是 markdown，不是污染）—— 一个字都不动。
def _asset_fragments() -> set[str]:
    frags: set[str] = set()
    for p in (REPO / "h3studio" / "skills").rglob("*.txt"):
        for m in MD.finditer(p.read_text(encoding="utf-8", errors="replace")):
            frags.add(m.group(0))
    return frags


def _string_spans(line: str) -> list[tuple[int, int]]:
    """返回一行里**字符串字面量**的 (start, end) 区间（不含引号）。

    口径：跳过 `#` 之后的注释；跳过单引号/双引号里的转义。
    """
    spans = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "#":
            break                      # 行注释 —— 后面的不是字符串
        if ch in "\"'":
            quote = ch
            j = i + 1
            while j < n:
                if line[j] == "\\":
                    j += 2
                    continue
                if line[j] == quote:
                    break
                j += 1
            if j < n:
                spans.append((i + 1, j))
            i = j + 1
            continue
        i += 1
    return spans


def _docstring_lines(tree: ast.AST) -> set[int]:
    """返回所有 docstring 覆盖的行号（1-based）。

    【为什么必须排除】docstring 在源码里是**多行字符串字面量**，
    逐行看每一行都像「字符串」-> 不排除的话会把 600+ 处 docstring
    里的 `**xxx**` 也换掉（那是注释记法，不是污染）。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            start = node.value.lineno
            end = getattr(node.value, "end_lineno", start) or start
            for ln in range(start, end + 1):
                out.add(ln)
    return out


def fix_file(path: Path, skip: set[str], dry: bool) -> int:
    src = path.read_text(encoding="utf-8")
    lines = src.split("\n")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    doc_lines = _docstring_lines(tree)
    total = 0
    for idx, line in enumerate(lines):
        lineno = idx + 1
        if lineno in doc_lines:
            continue
        spans = _string_spans(line)
        if not spans:
            continue
        # 从右往左替换，避免位移
        for start, end in reversed(spans):
            chunk = line[start:end]
            new_chunk = chunk
            for m in MD.finditer(chunk):
                frag = m.group(0)
                if frag in skip:
                    continue
                new_chunk = new_chunk.replace(
                    frag, f"「{m.group('body')}」")
            if new_chunk != chunk:
                total += len(MD.findall(chunk)) - len(MD.findall(new_chunk))
                line = line[:start] + new_chunk + line[end:]
        lines[idx] = line
    if total and not dry:
        path.write_text("\n".join(lines), encoding="utf-8")
    return total


def main(argv) -> int:
    dry = "--dry-run" in argv
    skip = _asset_fragments()
    grand = 0
    for name in UI_MODULES:
        path = REPO / "h3studio" / name
        if not path.exists():
            continue
        n = fix_file(path, skip, dry)
        if n:
            print(f"{name}: {n} 处")
            grand += n
    print(f"\n合计：{grand} 处" + ("（dry-run，未落盘）" if dry else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
