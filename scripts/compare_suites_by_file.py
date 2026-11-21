#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐文件对拍：「我的分支」vs「baseline」的全量测试结果对齐。

【为什么逐文件而不是比总数】
全量跑一次要 45~50 分钟，两边各跑一次太慢；
而且总数字会被**并发争用**污染 ——
首轮实测我的分支 49 failed / 45 errors，
但把同样的用例单独跑全过（是并发跑套件导致的资源争用，
另一个并发任务当时也在跑自己的全量套件）。

逐文件跑：每个测试文件**独立**起、跑、收集结果，
不受其它文件的资源占用影响；两边同口径，最后对齐比较。

【判据】某个文件的 failed+error 变多 = 真回归。

用法：python3 scripts/compare_suites_by_file.py <worktreeA> <worktreeB> <out.json>
"""
import json
import re
import subprocess
import sys
from pathlib import Path

TIMEOUT = 400


def run_file(wt, rel):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", rel, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(wt), capture_output=True, text=True, timeout=TIMEOUT)
    out = proc.stdout
    nums = {k: int(v) for v, k in
            re.findall(r"(\d+) (failed|passed|error)", out)}
    # pytest 的 error 关键字也可能出现在 "X errors" 形式
    m = re.search(r"(\d+) errors?", out)
    if m:
        nums["error"] = int(m.group(1))
    return nums


def collect(wt, files):
    res = {}
    for rel in files:
        try:
            res[rel] = run_file(wt, rel)
        except subprocess.TimeoutExpired:
            res[rel] = {"timeout": 1}
        print("  %-52s %s" % (rel, res[rel]), flush=True)
    return res


def main():
    wa, wb, out = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    files_a = sorted(p.relative_to(wa).as_posix() for p in (wa / "tests").glob("test_*.py"))
    files_b = sorted(p.relative_to(wb).as_posix() for p in (wb / "tests").glob("test_*.py"))

    common = sorted(set(files_a) & set(files_b))
    only_a = sorted(set(files_a) - set(files_b))
    only_b = sorted(set(files_b) - set(files_a))
    print("共同文件 %d 个（A 独有 %d，B 独有 %d）" % (len(common), len(only_a), len(only_b)))

    print("=== A: %s ===" % wa)
    ra = collect(wa, common)
    print("=== B: %s ===" % wb)
    rb = collect(wb, common)

    def bad(n):
        return n.get("failed", 0) + n.get("error", 0) + n.get("timeout", 0) * 99

    regress = []
    for f in common:
        if bad(ra[f]) > bad(rb[f]):
            regress.append({"file": f, "B_baseline": rb[f], "A_branch": ra[f]})

    report = {
        "files_compared": len(common),
        "only_in_A": only_a, "only_in_B": only_b,
        "A_failures_total": sum(bad(v) for v in ra.values()),
        "B_failures_total": sum(bad(v) for v in rb.values()),
        "regressions_A_worse_than_B": regress,
        "A": ra, "B": rb,
    }
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print()
    print("对比文件数:", len(common))
    print("A(我的分支) 失败+错误总数:", report["A_failures_total"])
    print("B(baseline) 失败+错误总数:", report["B_failures_total"])
    print("回归（A 比 baseline 差）:", len(regress))
    for r in regress:
        print("  ", r["file"], "baseline:", r["B_baseline"], "-> branch:", r["A_branch"])
    if only_a:
        print("只在 A 有的文件:", only_a)
    if only_b:
        print("只在 B 有的文件:", only_b)
    print("OUT:", out)


if __name__ == "__main__":
    main()
