# -*- coding: utf-8 -*-
"""第 6 轮审查：把 DOM 里出现的 markdown 片段映射回源码位置。

用途：区分「**合法**的官方提示词模板原文（本来就是 markdown）」
与「**污染**（本项目自己的注释记法漏进了界面）」。

用法：python3 scripts/cr6_map_dom_markdown.py [证据目录]
"""
from __future__ import annotations

import pathlib
import re
import sys

MD = re.compile(r"\*\*[^*\n]{1,60}\*\*")
REPO = pathlib.Path(__file__).resolve().parent.parent


def main(argv):
    ev = pathlib.Path(argv[1] if len(argv) > 1 else
                      REPO / ".review-evidence" / "evidence-cr6")
    dom = []
    for name in ("dom_texts.txt", "dom_attrs.txt"):
        path = ev / name
        if path.exists():
            dom.extend(path.read_text(encoding="utf-8").split("\n"))

    frags = set()
    for line in dom:
        for m in MD.findall(line):
            frags.add(m)

    # 官方提示词模板原文（本来就是 markdown，不是污染）
    asset_frags = set()
    for p in (REPO / "h3studio" / "skills").rglob("*.txt"):
        for m in MD.findall(p.read_text(encoding="utf-8", errors="replace")):
            asset_frags.add(m)

    # 源码位置索引
    srcidx = {}
    files = list((REPO / "web").glob("*.js")) + list((REPO / "h3studio").glob("*.py"))
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(txt.split("\n"), 1):
            for m in MD.findall(ln):
                srcidx.setdefault(m, []).append(f"{p.relative_to(REPO)}:{i}")

    print(f"DOM 里出现的 markdown 片段（去重）: {len(frags)}")
    print(f"其中提示词模板原文（合法 markdown）: {len(frags & asset_frags)}")
    print(f"需处理: {len(frags - asset_frags)}")
    print()
    for f in sorted(frags):
        if f in asset_frags:
            tag, loc = "模板", "(官方提示词原文，不是污染)"
        else:
            locs = srcidx.get(f)
            tag = "需修" if locs else "未定位"
            loc = locs[0] if locs else "(源码里没找到 —— 可能是运行时拼出来的)"
        print(f"  [{tag}] {f}")
        print(f"        @ {loc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
