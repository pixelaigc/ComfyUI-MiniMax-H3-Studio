#!/usr/bin/env python3
"""c6 验收取证：全量 pytest 与未改动基线的 FAILED/ERROR 名单逐条 diff。

【要证明什么】
  1. 我的分支**零新增回归**（名单 diff：「仅在 acc-fix」= 0 条）。
  2. 新增用例**全部通过**（本轮新增/修改的测试文件全绿）。
  3. 两份名单文件 + md5 都落盘，可被第三方独立复算。

【为什么名单要「规范化」后比 md5】
  pytest 的失败摘要行含耗时（如 `0.12s`）、随机端口等易变字段，
  直接对原文取 md5 会因环境抖动而不可复现。
  所以规范化口径是：
    * 只取 `FAILED xxx` / `ERROR xxx` 的**测试 node id**
    * 排序
    * 去重
  规范化后的 md5 才是可复算的一致性证据。

用法：
    python3 scripts/verify_acceptance_c6_regression.py --fix <fix_full.txt> \
        --baseline <baseline_full.txt> --out <证据目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

#: 从 pytest 输出里提取失败/错误名单的行首
FAILED_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)", re.M)


def extract_failures(text: str) -> list[str]:
    """提取规范化后的失败/错误测试 node id 列表（排序 + 去重）。"""
    items = []
    for kind, nodeid in FAILED_RE.findall(text):
        items.append(f"{kind} {nodeid}")
    return sorted(set(items))


def canonical_md5(items: list[str]) -> str:
    payload = "\n".join(items).encode("utf-8")
    return hashlib.md5(payload).hexdigest()


def parse_summary(text: str) -> dict:
    """从 pytest 尾部汇总行解析计数。"""
    out = {}
    for line in reversed(text.splitlines()):
        line = line.strip()
        m = re.match(
            r"^(?:(\d+) failed)?(?:, )?(?:(\d+) passed)?"
            r"(?:, )?(?:(\d+) skipped)?(?:, )?(?:(\d+) error)?"
            r"(?:, )?(?:(\d+) warnings?)?(?: in [\d.]+s)?$",
            line)
        if m and any(m.groups()):
            names = ["failed", "passed", "skipped", "errors", "warnings"]
            for name, val in zip(names, m.groups()):
                out[name] = int(val) if val else 0
            out["summary_line"] = line
            break
    # 兼容 `2 failed, 2107 passed, 4 skipped in 1245.74s (0:20:45)` 这种顺序
    if "summary_line" not in out:
        for line in reversed(text.splitlines()):
            if " passed" in line and ("failed" in line or "error" in line):
                out["summary_line"] = line.strip()
                for name in ("failed", "passed", "skipped", "error"):
                    mm = re.search(rf"(\d+) {name}", line)
                    if mm:
                        out[name if name != "error" else "errors"] = int(mm.group(1))
                break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", required=True)
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--new-tests", nargs="*", default=[],
                    help="本轮新增/修改的测试文件（用来证明「新增用例全通过」）")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    fix_text = Path(args.fix).read_text(encoding="utf-8", errors="replace")
    base_text = Path(args.baseline).read_text(encoding="utf-8", errors="replace")

    fix_items = extract_failures(fix_text)
    base_items = extract_failures(base_text)

    fix_set, base_set = set(fix_items), set(base_items)
    only_in_fix = sorted(fix_set - base_set)     # **新增回归**（必须为空）
    only_in_base = sorted(base_set - fix_set)    # 本轮修掉的
    common = sorted(fix_set & base_set)

    # 落盘两份名单文件（规范化版，可被第三方 diff）
    fix_list_file = out / "c6-fix-failures.txt"
    base_list_file = out / "c6-baseline-failures.txt"
    fix_list_file.write_text("\n".join(fix_items) + "\n", encoding="utf-8")
    base_list_file.write_text("\n".join(base_items) + "\n", encoding="utf-8")

    fix_md5 = canonical_md5(fix_items)
    base_md5 = canonical_md5(base_items)
    md5_file = out / "c6-md5.txt"
    md5_file.write_text(
        f"# c6 名单一致性证据（规范化口径：pytest FAILED/ERROR 的测试 node id，"
        f"排序去重）\n"
        f"# 规范化算法：md5(\"\\n\".join(sorted(set(items))))\n"
        f"count = {len(fix_items)}\n"
        f"canonical_md5 = {fix_md5}\n"
        f"\n"
        f"[baseline]\n"
        f"count = {len(base_items)}\n"
        f"canonical_md5 = {base_md5}\n",
        encoding="utf-8")

    # 新增用例是否全通过
    new_tests_result = None
    if args.new_tests:
        import subprocess
        import sys
        cmd = [sys.executable, "-m", "pytest", *args.new_tests,
               "-p", "no:cacheprovider", "-q", "--tb=short"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        out_file = out / "c6-new-tests.txt"
        out_file.write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
        new_tests_result = {
            "cmd": " ".join(cmd),
            "returncode": proc.returncode,
            "all_passed": proc.returncode == 0,
            "output_file": str(out_file),
            "summary": parse_summary(proc.stdout),
            "tail": (proc.stdout or "")[-1500:],
        }

    diff_file = out / "c6-diff.txt"
    diff_file.write_text(
        f"===== 仅在 acc-fix（新增回归，必须为空）=====\n"
        + ("\n".join(only_in_fix) if only_in_fix else "(空)\n")
        + f"\n===== 仅在 acc-baseline（本轮修掉）=====\n"
        + ("\n".join(only_in_base) if only_in_base else "(空)\n")
        + f"\n===== 两侧共有（既有失败）=====\n"
        + ("\n".join(common) if common else "(空)\n"),
        encoding="utf-8")

    verdict = {
        "fix_summary": parse_summary(fix_text),
        "baseline_summary": parse_summary(base_text),
        "fix_failure_count": len(fix_items),
        "baseline_failure_count": len(base_items),
        "fix_canonical_md5": fix_md5,
        "baseline_canonical_md5": base_md5,
        "only_in_fix_count": len(only_in_fix),
        "only_in_fix": only_in_fix,
        "only_in_baseline_count": len(only_in_base),
        "only_in_baseline": only_in_base,
        "common_count": len(common),
        "common": common,
        "zero_new_regression": len(only_in_fix) == 0,
        "new_tests_all_passed": (
            new_tests_result["all_passed"] if new_tests_result else None),
    }

    evidence = {
        "verdict": verdict,
        "files": {
            "fix_full_output": str(Path(args.fix).resolve()),
            "baseline_full_output": str(Path(args.baseline).resolve()),
            "fix_failure_list": str(fix_list_file),
            "baseline_failure_list": str(base_list_file),
            "md5_file": str(md5_file),
            "diff_file": str(diff_file),
        },
        "new_tests": new_tests_result,
    }
    (out / "acceptance_c6_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    print("===== c6 判定 =====")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    print(f"\n证据目录：{out}")
    ok = verdict["zero_new_regression"]
    if new_tests_result is not None:
        ok = ok and new_tests_result["all_passed"]
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
