#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升 app.js / video-panel.js 的 ?v= 缓存版本号（保留 CRLF 行尾）。

为什么必须做：`/static/*` 没有 Cache-Control，浏览器按启发式新鲜度缓存。
app.js 内容变了而 ?v= 不变 ⇒ 老用户浏览器拿到的仍是**旧的坏文件**
（本缺陷就是「漏传 onPick」那版）—— 修复等于没上线。

index.html 也是 CRLF，patch 工具的空白匹配会失配 ⇒ 用行级精确替换。
"""
import sys
from pathlib import Path

TARGET = Path(sys.argv[1])

# (文件名, 新版本号)
BUMPS = [
    ("app.js", "cr20261116c"),
    ("video-panel.js", "vparams4"),
]


def main():
    raw = TARGET.read_bytes().decode("utf-8")
    lines = raw.split("\r\n")
    changed = []
    for fname, new_v in BUMPS:
        hit = None
        for i, ln in enumerate(lines):
            if f"/static/{fname}?v=" in ln:
                hit = i
                break
        if hit is None:
            print(f"!! index.html 里找不到 {fname} 的 ?v= 引用", file=sys.stderr)
            return 1
        old = lines[hit]
        import re
        m = re.search(r'\?v=([^"\'\s>]+)', old)
        old_v = m.group(1) if m else "?"
        lines[hit] = old.replace(f"?v={old_v}", f"?v={new_v}")
        changed.append((fname, old_v, new_v))

    TARGET.write_bytes("\r\n".join(lines).encode("utf-8"))
    for fname, old_v, new_v in changed:
        print(f"OK {fname}: ?v= {old_v} -> {new_v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
