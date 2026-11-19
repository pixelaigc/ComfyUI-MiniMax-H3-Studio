#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升 web/index.html 里 app.js / style.css 的 ?v= 版本号（保持 CRLF 行尾）。

【为什么用脚本而不是 patch 工具】
web/index.html 是 **CRLF** 行尾文件。shell heredoc / patch 工具写入时会把
`\\r\\n` 写成 `\\n`，把整份文件的行尾搞乱（diff 全文件变红）。
这个脚本按 **bytes** 级别替换，行尾一个字节都不动。

【为什么要再升一次】
本轮又改了 web/app.js（失败预览区加 is-failed 标记）和 web/style.css
（新增 .job-preview.is-failed 规则）。/static/* 没有 Cache-Control，
浏览器按启发式新鲜度缓存 —— 内容变了而 ?v= 不变，老用户拿到的仍是
旧文件（失败卡片还是一片黑）。
"""
from pathlib import Path

TARGET = Path("/root/projects/h3s-merge-20261119/web/index.html")

REPLACEMENTS = [
    (b"style.css?v=genfix20261119a", b"style.css?v=genfix20261119b"),
    (b"app.js?v=genfix20261119a", b"app.js?v=genfix20261119b"),
]


def main() -> int:
    raw = TARGET.read_bytes()
    crlf_before = raw.count(b"\r\n")
    lf_only_before = raw.count(b"\n") - crlf_before
    for old, new in REPLACEMENTS:
        if old not in raw:
            print(f"!! 没找到 {old!r}")
            return 1
        raw = raw.replace(old, new, 1)
    TARGET.write_bytes(raw)
    data = TARGET.read_bytes()
    crlf = data.count(b"\r\n")
    lf_only = data.count(b"\n") - crlf
    print(f"CRLF {crlf_before} -> {crlf}；LF-only {lf_only_before} -> {lf_only}")
    return 0 if lf_only == lf_only_before else 1


if __name__ == "__main__":
    raise SystemExit(main())
