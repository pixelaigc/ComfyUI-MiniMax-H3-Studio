#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步 tests/test_frontend_assets_hygiene.py 的版本号表（对齐线上实际值）。

【为什么需要脚本】
这张表是「内容改了必须升 ?v=」的关卡。表里的期望值必须与
web/index.html 的**实际** ?v= 一致，否则要么报假红（表过期），
要么漏掉真的没升版的资源。

本轮 web/app.js 与 web/style.css 又被改了（失败预览区加 is-failed
标记 + 新增 .job-preview.is-failed 规则），?v= 升到 genfix20261119b，
所以表里这两条也要跟着走。

【同时】把 style.css 补进 STALE_BUMPED —— 上一轮它还在 NOT_STALE_OK，
但内容已被本轮改动过（两表不能同时含同一个 key，否则断言必红）。
"""
from pathlib import Path

TARGET = Path("/root/projects/h3s-merge-20261119/tests/test_frontend_assets_hygiene.py")

REPLACEMENTS = [
    # app.js: 期望值 -> 本轮实际值
    ('    "app.js":                  ("cr20261116c", "genfix20261119a"),',
     '    "app.js":                  ("cr20261116c", "genfix20261119b"),'),
    # style.css: 补进 STALE_BUMPED（内容已被本轮改动过）
    ('    "agent-manage.js":         ("a4gen1",       "cr20261118a"),\n}',
     '    "agent-manage.js":         ("a4gen1",       "cr20261118a"),\n'
     '    # 【2026-11-19 生成状态缺陷修复】style.css 的内容被改了\n'
     '    #   （新增 .job-preview.is-failed 失败占位底色 —— 失败卡片不再\n'
     '    #    是一片黑）⇒ ?v= 必须升到 genfix20261119b。\n'
     '    "style.css":               ("cr20261115a",  "genfix20261119b"),\n'
     '}'),
]


def main() -> int:
    text = TARGET.read_text(encoding="utf-8")
    for old, new in REPLACEMENTS:
        if old not in text:
            print(f"!! 没找到: {old[:70]!r}")
            return 1
        text = text.replace(old, new, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("已同步版本号表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
