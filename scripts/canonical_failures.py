#!/usr/bin/env python3
"""把 pytest -rfE 的输出解析成「规范化的失败/错误名单」，并做两个名单的逐条 diff。

规范形式 = 排序去重（pytest 收集顺序不固定，行序会变）。
输出：
  * <name>_failures.txt   —— 规范名单（每行一条 test id）
  * <name>_md5.txt       —— 名单的 md5
  * diff 报告（只在 --diff 时打）
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

LINE = re.compile(r"^(FAILED|ERROR)\s+(\S+?)(?:\s+-\s+.*)?$")


def parse(path: Path) -> list[str]:
    ids: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE.match(raw.strip())
        if m:
            ids.add(f"{m.group(1)} {m.group(2)}")
    return sorted(ids)


def canon_md5(ids: list[str]) -> str:
    return hashlib.md5("\n".join(ids).encode("utf-8")).hexdigest()


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: canonical_failures.py <out_prefix> <pytest_output>... "
              "[--diff <other_prefix>]")
        return 2
    prefix = sys.argv[1]
    rest = sys.argv[2:]
    other_prefix = None
    if "--diff" in rest:
        i = rest.index("--diff")
        other_prefix = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    files = [a for a in rest if not a.startswith("--")]
    ids: list[str] = []
    for f in files:
        ids += parse(Path(f))
    ids = sorted(set(ids))
    out = Path(f"{prefix}_failures.txt")
    out.write_text("\n".join(ids) + ("\n" if ids else ""), encoding="utf-8")
    md5 = canon_md5(ids)
    Path(f"{prefix}_md5.txt").write_text(
        f"count = {len(ids)}\ncanonical_md5 = {md5}\n", encoding="utf-8")
    print(f"[{prefix}] 规范名单 {len(ids)} 条  md5={md5}")
    print(f"  -> {out}")

    if other_prefix:
        o_ids = parse(Path(f"{other_prefix}_failures.txt"))
        only_base = [x for x in ids if x not in set(o_ids)]
        only_other = [x for x in o_ids if x not in set(ids)]
        print(f"\n=== diff: {prefix} vs {other_prefix} ===")
        print(f"  {prefix} count = {len(ids)}  md5 = {md5}")
        print(f"  {other_prefix} count = {len(o_ids)}  md5 = {canon_md5(o_ids)}")
        print(f"  仅在 {prefix}: {len(only_base)}")
        for x in only_base[:40]:
            print(f"    + {x}")
        print(f"  仅在 {other_prefix}: {len(only_other)}")
        for x in only_other[:40]:
            print(f"    - {x}")
        print(f"  完全一致 = {not only_base and not only_other}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
