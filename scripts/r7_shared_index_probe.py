#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：核实共享 worktree（/root/projects/h3studio-cont-seam-hires）的 index 状态，
为「推进 local/maintainer」这一步做精确判定。

【背景】local/maintainer 被那个 worktree 检出着；要推进主线就必须处理它的
index（历史教训见 docs/接续段标准采样路径改造-20261118.md §5.5：
`git update-ref` 不刷新 index ⇒ 埋「静默删除他人成果」的雷）。

【本脚本只读】不修改任何文件。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

WT = Path("/root/projects/h3studio-cont-seam-hires")


def git(*args: str) -> str:
    proc = subprocess.run(["git", "-C", str(WT), *args],
                          capture_output=True, text=True, timeout=300)
    return proc.stdout


def blob(rev: str, path: str) -> str:
    proc = subprocess.run(["git", "-C", str(WT), "rev-parse", f"{rev}:{path}"],
                          capture_output=True, text=True, timeout=120)
    return proc.stdout.strip() if proc.returncode == 0 else "-"


def main() -> int:
    raw = subprocess.run(
        ["git", "-C", str(WT), "status", "--porcelain=v1", "-z"],
        capture_output=True, timeout=300).stdout
    # 解析 -z 格式：XY<space>path\0（改名会有两条）
    fields = raw.split(b"\0")
    entries = []
    i = 0
    while i < len(fields):
        item = fields[i].decode("utf-8", "replace")
        if len(item) < 3:
            i += 1
            continue
        xy = item[:2]
        path = item[3:]
        entries.append((xy, path))
        i += 1

    staged = [(xy, p) for xy, p in entries if xy[0] not in " ?"]
    unstaged = [(xy, p) for xy, p in entries if xy[1] not in " ?"]

    print(f"共享 worktree：{WT}")
    print(f"  HEAD = {git('rev-parse', 'HEAD').strip()}")
    print(f"  分支 = {git('branch', '--show-current').strip()}")
    print(f"  staged 条目 = {len(staged)}   unstaged 条目 = {len(unstaged)}")
    print()

    old_head = "fcb6057"
    new_head = "d0a622f"

    print("=== staged 条目逐条判定 ===")
    print("  M 判定：index blob == old_head blob  -> 陈旧快照（主线推进造成）")
    print("  D 判定：文件在 old_head 里存在、但 index 里没有 -> 可能是陈旧删除")
    print()
    stale_m: list[str] = []
    stale_d: list[str] = []
    genuine_m: list[str] = []
    genuine_d: list[str] = []
    for xy, path in staged:
        if xy[0] == "M":
            idx = subprocess.run(
                ["git", "-C", str(WT), "ls-files", "-s", "--", path],
                capture_output=True, text=True, timeout=120).stdout.split()
            idx_blob = idx[1] if len(idx) >= 2 else "-"
            if idx_blob == blob(old_head, path):
                stale_m.append(path)
            else:
                genuine_m.append(path)
        elif xy[0] == "D":
            if blob(old_head, path) != "-" and blob(new_head, path) != "-":
                stale_d.append(path)
            else:
                genuine_d.append(path)

    print(f"  陈旧 M（index 仍是 old_head 的旧版本）= {len(stale_m)}")
    print(f"  真实 M（对方自己的在改内容）        = {len(genuine_m)}")
    print(f"  陈旧 D（新主线里仍存在）            = {len(stale_d)}")
    print(f"  真实 D                            = {len(genuine_d)}")
    print()
    print("=== 陈旧 M 明细 ===")
    for p in stale_m:
        print("   ", p)
    print("=== 陈旧 D 明细（前 40）===")
    for p in stale_d[:40]:
        print("   ", p)
    print()
    print("=== 新主线 d0a622f 里有、但共享 index 里没有的文件（update-ref 会造成的污染）===")
    tracked_new = git("ls-tree", "-r", "--name-only", new_head).splitlines()
    index_paths = set(git("ls-files").splitlines())
    missing = [p for p in tracked_new if p not in index_paths]
    print(f"  共 {len(missing)} 条（前 30）：")
    for p in missing[:30]:
        print("   ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
