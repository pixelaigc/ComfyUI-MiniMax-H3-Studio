#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 补正：把共享 worktree 里**对方自己的 31 条暂存删除**放回新 index。

【为什么需要补正】
推进 `local/maintainer` 时，新 index 的基底是「新主线 tree」。
但对方 worktree 的 index 里，有 31 条路径是对方**主动暂存的删除**
（`docs/第4轮收口报告-20261119.md`、`evidence/20261119-r4-closure/**`、
`scripts/cleanup_assets_jobs.py`、`tests/e2e/gen_flow.py` …）——
这些是对方**清理生成物**的意图，不是第 6 轮遗留的污染。

新 index 用了主线 tree 当基底 ⇒ 那 31 条被「复活」成 index 有、工作区无
⇒ 状态从 `D `（暂存删除）变成 ` D`（未暂存删除）
⇒ 对方下一次 `git commit -a` 会**把本轮主线成果删掉**（新地雷）。

【正确处置】
把这 31 条路径从新 index 里移除，恢复成对方的原始意图（暂存删除）。
只动 index，不动工作区（工作区本来就没有这些文件）。
"""
from __future__ import annotations

import json
import os
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
OLD = "fcb6057"
NEW = "d0a622f"


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=600)
    if check and proc.returncode != 0:
        raise SystemExit(f"git 失败: {' '.join(args)}\n{proc.stderr}")
    return proc


def index_paths() -> set[str]:
    raw = subprocess.run(
        ["git", "-C", WT, "-c", "core.quotePath=false", "ls-files", "-s", "-z"],
        capture_output=True, timeout=300).stdout
    out = set()
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        _meta, path = rec.split(b"\t", 1)
        out.add(path.decode("utf-8", "replace"))
    return out


def tree_paths(rev: str) -> set[str]:
    out = set()
    for line in git("ls-tree", "-r", rev).stdout.splitlines():
        out.add(line.split("\t", 1)[1])
    return out


def main() -> int:
    idx_now = index_paths()
    old_tree = tree_paths(OLD)
    new_tree = tree_paths(NEW)

    # 对方原始 index（推进前备份）里缺失、但旧主线有的路径 = 对方的暂存删除
    backup = "/tmp/r7_shared_index_pre_advance.bak"
    env = {**os.environ, "GIT_INDEX_FILE": backup}
    raw = subprocess.run(
        ["git", "-C", WT, "-c", "core.quotePath=false", "ls-files", "-s", "-z"],
        capture_output=True, timeout=300, env=env).stdout
    idx_backup = set()
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        _meta, path = rec.split(b"\t", 1)
        idx_backup.add(path.decode("utf-8", "replace"))

    staged_del = sorted((old_tree - idx_backup) & new_tree)
    print(f"对方的暂存删除（本应保留）= {len(staged_del)} 条")
    for p in staged_del:
        print("   ", p)

    # 当前新 index 里多出来的这 31 条 = 需要移除，恢复对方的暂存删除意图
    to_remove = [p for p in staged_del if p in idx_now]
    print(f"新 index 中需移除 = {len(to_remove)} 条")
    if not to_remove:
        print("无需处置")
        return 0

    for p in to_remove:
        proc = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                               "update-index", "--force-remove", "--", p],
                              capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            print(f"  移除失败 {p}: {proc.stderr.strip()}")
    print("已从 index 移除（工作区未动）")

    # 核验
    idx_after = index_paths()
    still = [p for p in to_remove if p in idx_after]
    print(f"核验：仍留在 index 的 = {len(still)} {still}")
    land_new = new_tree - old_tree
    missing = [p for p in land_new if p not in idx_after]
    print(f"核验：主线新增文件在 index 缺失 = {len(missing)} {missing[:6]}")
    json.dump({"staged_del_preserved": to_remove},
              open("/tmp/r7_staged_del_preserved.json", "w"), ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
