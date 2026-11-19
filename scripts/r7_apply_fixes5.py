#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 修复补丁（第五轮）：shift 节点的「不可用」提示里的节点类名。

【缺陷 B5 续】`web/video-panel.js` 的 shift 滑杆提示里写着
「本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点」—— 节点类名对普通用户
没有意义（用户只需要知道「这个滑杆用不了」）。

【修法】类名换成「shift 节点」这种可读说法，信息量不丢。

用法：python3 scripts/r7_apply_fixes5.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECK_ONLY = "--check" in sys.argv
VP = REPO / "web" / "video-panel.js"


def read_text(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def main() -> int:
    text = read_text(VP)
    old = '"本机 ComfyUI 没有 MiniMaxH3SigmaShift 节点"'
    new = '"本机 ComfyUI 没有 shift 节点"'
    count = text.count(old)
    if count != 2:
        raise SystemExit(f"[B5-shift] 期望 2 处，实际 {count} 处")
    text = text.replace(old, new)
    if not CHECK_ONLY:
        with open(VP, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    print(("检查模式（未落盘）: " if CHECK_ONLY else "已应用: ")
          + f"B5-shift shift 滑杆提示去掉节点类名（{count} 处）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
