#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""第二轮全量回归的 21 项失败 —— 逐条在**基线**上对拍。

【方法论】
全量回归里的失败**不能**直接当成「我引入的」——
必须拿同一套用例在**基线 commit** 上跑一遍，比对差集。
差集里「只在我的分支失败」的才是真回归。

用法: python scripts/compare_failures_vs_baseline.py <failures.txt>
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/root/projects/h3s-favorites")
BASELINE = "/tmp/base-check"
PY = "/home/ubuntu/comfyui/venv/bin/python"


def run(nodeids, cwd):
    if not nodeids:
        return []
    proc = subprocess.run(
        [PY, "-m", "pytest", *nodeids, "-q", "--tb=no", "-p", "no:randomly"],
        cwd=cwd, capture_output=True, text=True, timeout=3000)
    out = proc.stdout or ""
    failed = []
    for line in out.splitlines():
        if line.startswith("FAILED "):
            failed.append(line.split(" - ")[0].replace("FAILED ", "").strip())
    return failed


def main():
    failures_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/regr-v4.txt"
    text = Path(failures_file).read_text(encoding="utf-8", errors="replace")
    # 两种输入都支持：pytest 输出（"FAILED xxx"）或纯 nodeid 一行一个
    mine = [m.strip() for m in re.findall(r"^FAILED\s+(\S+)", text, re.M)]
    if not mine:
        mine = [ln.strip() for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")]
    mine = [m for m in mine if "::" in m]
    # 去重保序
    seen = set()
    mine = [m for m in mine if not (m in seen or seen.add(m))]
    print(f"我的分支失败 {len(mine)} 项\n")

    # 基线：在 BASELINE 目录跑同一组
    if not Path(BASELINE).exists():
        subprocess.run(
            ["git", "-C", str(ROOT), "worktree", "add", "--detach", BASELINE, "dd477a1"],
            check=True, capture_output=True)
    print("=== 在基线 dd477a1 上跑同一组 ===")
    base_failed = run(mine, BASELINE)
    base_set = set(base_failed)
    print(f"基线也失败 {len(base_failed)} 项\n")

    only_mine = [m for m in mine if m not in base_set]
    only_base = [m for m in base_failed if m not in set(mine)]

    print("=== 只在我的分支失败（真回归候选）===")
    if not only_mine:
        print("  （无）")
    for m in only_mine:
        print(f"  {m}")

    print("\n=== 只在基线失败（我的改动修好的）===")
    if not only_base:
        print("  （无）")
    for m in only_base:
        print(f"  {m}")

    print(f"\n=== 汇总 ===")
    print(f"  我的分支失败 {len(mine)}  基线也失败 {len(base_failed)}")
    print(f"  真回归候选 {len(only_mine)}  被我修好 {len(only_base)}")
    return 0 if not only_mine else 1


if __name__ == "__main__":
    raise SystemExit(main())
