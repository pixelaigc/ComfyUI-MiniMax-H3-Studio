#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：推进 local/maintainer 前的**共享 worktree index 精确判定**。

【为什么要这么较真】
local/maintainer 被共享 worktree `/root/projects/h3studio-cont-seam-hires` 检出着。
历史教训（docs/接续段标准采样路径改造-20261118.md §5.5）：
`git update-ref` 直接移动分支指针**不刷新**那个 worktree 的 index ⇒
在对方 index 里埋下「静默删除他人成果」的雷。

本脚本算出三张清单，供处置时逐条使用：
  A. 对方**真实在改**的文件（staged M / unstaged M）—— 一个字节都不能动
  B. 主线推进会变成「暂存删除 / 陈旧回退」的路径 —— 这些是**地雷**
  C. 两者的**交集** —— 处置时必须保留对方的版本

只读脚本：不修改任何文件。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WT = Path("/root/projects/h3studio-cont-seam-hires")
OLD = "fcb6057"
NEW = "d0a622f"


def run(*args: str) -> str:
    proc = subprocess.run(["git", "-C", str(WT), *args],
                          capture_output=True, text=True, timeout=300)
    return proc.stdout


def ls_tree(rev: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in run("ls-tree", "-r", rev).splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def main() -> int:
    status = subprocess.run(
        ["git", "-C", str(WT), "status", "--porcelain=v1", "-z"],
        capture_output=True, timeout=300).stdout
    fields = status.split(b"\0")
    staged: list[tuple[str, str]] = []
    unstaged: list[tuple[str, str]] = []
    for item in fields:
        if len(item) < 4:
            continue
        text = item.decode("utf-8", "replace")
        xy, path = text[:2], text[3:]
        if xy[0] not in " ?":
            staged.append((xy[0], path))
        if xy[1] not in " ?":
            unstaged.append((xy[1], path))

    old_tree = ls_tree(OLD)
    new_tree = ls_tree(NEW)

    # 对方「真实在改」的文件（staged 或 unstaged 里的 M / A）
    theirs = {p for xy, p in staged + unstaged if xy in ("M", "A")}

    # 主线推进涉及的路径（old -> new 的差异）
    changed = set()
    for path in set(old_tree) | set(new_tree):
        if old_tree.get(path) != new_tree.get(path):
            changed.add(path)

    # 潜在地雷：主线新增的文件在对方 index 里没有（=> update-ref 后成「暂存删除」）
    landmine_new = sorted(p for p in changed if p not in old_tree)
    # 潜在地雷：主线改过的文件，对方 index 仍是旧版（=> 暂存回退）
    landmine_revert = sorted(p for p in changed if p in old_tree)

    print(f"共享 worktree HEAD = {run('rev-parse', 'HEAD').strip()}")
    print(f"  staged = {len(staged)}   unstaged = {len(unstaged)}")
    print(f"  对方真实在改的文件 = {len(theirs)}")
    print(f"  主线 fcb6057 -> d0a622f 涉及 = {len(changed)} 个路径")
    print()

    overlap_new = [p for p in landmine_new if p in theirs]
    overlap_rev = [p for p in landmine_revert if p in theirs]
    print(f"A. 地雷·新文件（主线新增、对方 index 没有）= {len(landmine_new)}")
    print(f"   其中与对方在改文件重叠 = {len(overlap_new)}")
    for p in overlap_new:
        print("     [重叠]", p)
    print(f"B. 地雷·陈旧回退（主线改过、对方 index 仍是旧版）= {len(landmine_revert)}")
    print(f"   其中与对方在改文件重叠 = {len(overlap_rev)}")
    for p in overlap_rev:
        print("     [重叠]", p)
    print()

    # 只处理「对方没在改」的地雷路径 —— 处置时用 git restore --staged <这些>
    safe = [p for p in (landmine_new + landmine_revert) if p not in theirs]
    print(f"C. 可安全处置的地雷路径（对方**没**在改）= {len(safe)}")
    print("   （处置动作：git restore --staged -- <这些路径>，只动 index，不动工作区）")
    print()
    print("D. 对方的在改文件清单（一个字节都不能动）：")
    for p in sorted(theirs):
        print("   ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
