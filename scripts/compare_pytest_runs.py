#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比两次全量 pytest 运行的**逐用例结果序列**。

【为什么不用「passed/failed 总数」对比】
两次全量跑都在浏览器用例阶段被 3000s timeout 掐断（没跑到汇总行），
所以拿不到总数。但 pytest 的 `-q` 输出是**每个用例一个字符**：
    .  通过
    F  失败
    E  出错（fixture 之类）
    s  跳过
    x  xfail
把两边的字符序列逐位对齐，就能直接回答
「我这版有没有引入**基线里没有**的失败」——
这正是验收要的「新增失败 0 条」。

【怎么用】
    python3 scripts/compare_pytest_runs.py \
        --baseline /tmp/pytest_base4.txt --candidate /tmp/pytest_mine4.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 汇总行/进度行的标记，用来把结果序列从噪声里切出来
_NOISE = re.compile(r"^=+ .* =+$|^\s*$|warnings summary|short test summary|"
                     r"^\d+ (passed|failed)|passed,|failed,|error|skipped")


def result_sequence(path: Path) -> str:
    """从 pytest -q 输出里抽出逐用例结果字符序列。"""
    seq = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _NOISE.match(line):
            continue
        # 进度行形如 "....F.. [ 42%]"，去掉 [ xx%] 部分
        line = re.sub(r"\[\s*\d+%\]\s*$", "", line)
        line = line.strip()
        if not line:
            continue
        if not re.fullmatch(r"[.FEsxXu]*", line):
            continue
        seq.append(line)
    return "".join(seq)


def summarize(seq: str) -> dict:
    return {
        "total": len(seq),
        "passed": seq.count("."),
        "failed": seq.count("F"),
        "error": seq.count("E"),
        "skipped": seq.count("s"),
        "xfail": seq.count("x"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    b = result_sequence(Path(args.baseline))
    c = result_sequence(Path(args.candidate))

    # 【为什么要对齐而不是直接逐位比】
    # 两次全量跑都在浏览器用例阶段被 timeout 掐断，
    # **跑到的用例数不一定一样**（候选版多跑/少跑几个用例，
    # 或者前一个用例耗时不同导致进度点错位）。
    # 直接逐位比会把这「错位」误报成「一堆新增失败」。
    #
    # 最稳的判据：**找出候选相对基线插入/删除的那一段**，
    # 看它是「新增且全过」还是「新增且有失败」。
    #   * 插入段全为 '.'  -> 纯新增用例且全部通过 -> PASS
    #   * 插入段含 F/E    -> 新增失败 -> FAIL
    # 去掉插入段后，两边必须**逐位完全一致**（= 没有任何既有用例被改坏）。

    b_fail = {i for i, ch in enumerate(b) if ch in "FE"}
    c_fail = {i for i, ch in enumerate(c) if ch in "FE"}

    # 找最长公共前缀与后缀，夹出中间的差异段
    pre = 0
    while pre < min(len(b), len(c)) and b[pre] == c[pre]:
        pre += 1
    suf = 0
    while (suf < min(len(b), len(c)) - pre
           and b[len(b) - 1 - suf] == c[len(c) - 1 - suf]):
        suf += 1

    b_mid = b[pre:len(b) - suf] if suf else b[pre:]
    c_mid = c[pre:len(c) - suf] if suf else c[pre:]

    # 候选插入段里的失败
    inserted_failures = [i for i, ch in enumerate(c_mid) if ch in "FE"]
    # 基线有失败、候选没有的（被删掉的失败用例 —— 也算差异）
    removed_failures = [i for i, ch in enumerate(b_mid) if ch in "FE"]

    b_sum, c_sum = summarize(b), summarize(c)
    count_new_fail = max(0, (c_sum["failed"] + c_sum["error"])
                          - (b_sum["failed"] + b_sum["error"]))

    # 去掉插入段后是否逐位一致
    c_wo = c[:pre] + c[len(c) - suf:] if suf else c[:pre]
    identical_after = c_wo == b

    result = {
        "baseline_file": args.baseline,
        "candidate_file": args.candidate,
        "note": ("两次全量跑都在浏览器用例阶段被 timeout 掐断（没跑到汇总行），"
                 "所以用 pytest -q 的逐用例结果字符序列做精确对比："
                 "找出候选相对基线插入的那一段，看它是否全过，"
                 "并验证去掉该段后两边逐位完全一致。"),
        "baseline_summary": b_sum,
        "candidate_summary": c_sum,
        "baseline_len": len(b),
        "candidate_len": len(c),
        "delta_len": len(c) - len(b),
        "common_prefix_len": pre,
        "common_suffix_len": suf,
        "baseline_middle": b_mid,
        "candidate_middle": c_mid,
        "inserted_len": len(c_mid),
        "inserted_all_passed": bool(c_mid) and set(c_mid) == {"."},
        "inserted_failures_count": len(inserted_failures),
        "inserted_failures_positions": inserted_failures,
        "removed_failures_count": len(removed_failures),
        "identical_after_removing_inserted": identical_after,
        "new_failures_count": len(inserted_failures),
        "count_delta_failed_plus_error": count_new_fail,
        "verdict": (
            ("PASS: 候选相对基线只新增了 %d 个用例（全部通过），"
             "去掉后逐位完全一致 —— 新增失败 0 条"
             % len(c_mid)) if (identical_after and not inserted_failures
                              and count_new_fail == 0) else
            ("FAIL: 插入段含 %d 个失败 / 计数差 %d / 去掉插入段后不一致=%s"
             % (len(inserted_failures), count_new_fail,
                not identical_after))),
    }

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"证据落盘：{out}")

    print(f"基线: len={len(b)} {b_sum}")
    print(f"候选: len={len(c)} {c_sum}")
    print(f"公共前缀 {pre} / 公共后缀 {suf}")
    print(f"插入段（{len(c_mid)} 个）: {c_mid[:60]!r}"
          f"{'...' if len(c_mid) > 60 else ''}")
    print(f"插入段全过: {result['inserted_all_passed']}")
    print(f"插入段里的失败数: {len(inserted_failures)}")
    print(f"去掉插入段后两边逐位一致: {identical_after}")
    print(f"计数差（failed+error）: {count_new_fail}")
    print(f"结论: {result['verdict']}")
    return 0 if (identical_after and not inserted_failures
                 and count_new_fail == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
