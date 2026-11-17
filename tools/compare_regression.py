#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对拍两份 pytest 输出的**失败集合**，输出差集。

【为什么需要】
「回归没新增失败」这个结论不能靠肉眼看两行摘要 ——
必须**集合对比**：分支失败集合 ⊆ 基线失败集合，且差集为空。

【口径】
  * 只认 pytest 摘要行里的 `FAILED <nodeid>` —— 那是**断言失败**；
    收集期 import 崩掉（ERROR）单独统计，不混进「断言失败」集合
    （但也要报出来，不能藏）。
  * 输出：基线失败数 / 分支失败数 / **新增失败（必须为 0）** /
    已消失的失败 / ERROR 差异。

用法：python3 tools/compare_regression.py <基线输出.txt> <分支输出.txt>
"""

import re
import sys
from pathlib import Path

#: pytest 摘要行：`FAILED tests/xxx.py::test_y - AssertionError: ...`
FAILED_RE = re.compile(r"^FAILED\s+(\S+)", re.M)
#: 收集期错误：`ERROR tests/xxx.py::test_y`
ERROR_RE = re.compile(r"^ERROR\s+(\S+)", re.M)
#: 摘要行：`12 failed, 3400 passed, 3 skipped in 500.12s`
SUMMARY_RE = re.compile(
    r"(\d+)\s+failed.*?(\d+)\s+passed.*?(\d+)\s+skipped", re.S)


def parse(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    failed = set(FAILED_RE.findall(text))
    errored = set(ERROR_RE.findall(text))
    summary = {}
    m = SUMMARY_RE.search(text)
    if m:
        summary = {"failed": int(m.group(1)),
                   "passed": int(m.group(2)),
                   "skipped": int(m.group(3))}
    return {"failed": failed, "errored": errored, "summary": summary,
            "text": text}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    base_path = Path(sys.argv[1])
    branch_path = Path(sys.argv[2])
    if not base_path.is_file() or not branch_path.is_file():
        print(f"❌ 找不到输出文件：{base_path} / {branch_path}")
        return 2

    base = parse(base_path)
    branch = parse(branch_path)

    print("=" * 72)
    print("回归对拍：基线 vs 分支")
    print("=" * 72)
    print(f"基线：{base_path}")
    print(f"分支：{branch_path}")
    print()
    if base["summary"]:
        print(f"  基线摘要：{base['summary']['failed']} failed, "
              f"{base['summary']['passed']} passed, "
              f"{base['summary']['skipped']} skipped")
    if branch["summary"]:
        print(f"  分支摘要：{branch['summary']['failed']} failed, "
              f"{branch['summary']['passed']} passed, "
              f"{branch['summary']['skipped']} skipped")
    print()

    new_failed = branch["failed"] - base["failed"]
    fixed = base["failed"] - branch["failed"]
    new_error = branch["errored"] - base["errored"]

    print(f"基线失败条目：{len(base['failed'])}")
    print(f"分支失败条目：{len(branch['failed'])}")
    print()
    print(f"【新增失败】（必须为 0）：{len(new_failed)}")
    for node in sorted(new_failed):
        print(f"   ❌ {node}")
    print()
    print(f"【已消失的失败】（基线有、分支没有）：{len(fixed)}")
    for node in sorted(fixed):
        print(f"   ✅ {node}")
    print()
    print(f"【新增 ERROR】（收集期错误，必须为 0）：{len(new_error)}")
    for node in sorted(new_error):
        print(f"   ❌ {node}")
    print()

    ok = not new_failed and not new_error
    print("=" * 72)
    if ok:
        print("✅ 结论：**新增失败 0 条**，新增 ERROR 0 条 —— "
              "基线失败集合与分支失败集合一致（不新增回归）")
    else:
        print(f"❌ 结论：新增失败 {len(new_failed)} 条 / "
              f"新增 ERROR {len(new_error)} 条 —— 存在回归")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
