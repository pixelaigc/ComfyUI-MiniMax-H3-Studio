#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 web/style.css 插入 `.job-preview.is-failed` 规则（保持 CRLF 行尾）。

【为什么需要脚本而不是 patch 工具】
web/style.css 是 **CRLF** 行尾文件。patch 工具写入时用 LF，会把整份文件的
行尾搞乱（diff 全文件变红）。这个脚本按 bytes 级别的 CRLF 写入。

【规则本身】
`.job-preview` 默认背景 #0d0f12（近黑）+ 16:9 的 padding-bottom 盒子。
失败任务即使渲染了错误文案，用户看到的仍是「一大片近黑背景 + 几行红字」
—— 视觉上就是「一张全黑图」（实测截图里 vision 判定预览区「为全黑」）。

失败 / 取消 / 无产物三种状态给预览区加 is-failed，换成明确的错误底色
（暗红 + 斜纹），不再像一张「图」。
"""
from pathlib import Path

TARGET = Path("/root/projects/h3s-merge-20261119/web/style.css")
ANCHOR = ".job-preview video {\r\n"

BLOCK = (
    "/* 【2026-11-19 缺陷 2 修 —— 失败占位的底色不能是黑的】\r\n"
    "   原因：`.job-preview` 默认背景 #0d0f12（近黑），失败任务即使渲染了\r\n"
    "   错误文案，用户看到的仍是一大片近黑背景 + 中间几行红字 —— 视觉上就是\r\n"
    "   「一张全黑图」。实测截图（evidence/gen-status-20261119/\r\n"
    "   oom-failed-card-e2e.png）里 vision 判定预览区「为全黑」。\r\n"
    "   现在失败 / 取消 / 无产物三种状态给预览区加 is-failed，换成明确的\r\n"
    "   错误底色 + 斜纹，不再像一张「图」。 */\r\n"
    ".job-preview.is-failed {\r\n"
    "  background: repeating-linear-gradient(135deg,\r\n"
    "      #2a1418 0 12px, #34191e 12px 24px);\r\n"
    "  color: #ff8a8e;\r\n"
    "}\r\n"
    ".job-preview.is-failed .err-text { color: #ff9ca0; }\r\n"
)


def main() -> int:
    raw = TARGET.read_bytes()
    if ".job-preview.is-failed" in raw.decode("utf-8"):
        print("已存在，跳过")
        return 0
    anchor = ANCHOR.encode("utf-8")
    if anchor not in raw:
        print("!! 锚点没找到")
        return 1
    raw = raw.replace(anchor, BLOCK.encode("utf-8") + anchor, 1)
    TARGET.write_bytes(raw)
    data = TARGET.read_bytes()
    crlf = data.count(b"\r\n")
    lf_only = data.count(b"\n") - crlf
    print(f"已插入。CRLF={crlf} LF-only={lf_only}")
    return 0 if lf_only == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
