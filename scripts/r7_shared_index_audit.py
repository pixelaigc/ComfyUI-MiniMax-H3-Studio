#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：核验共享 worktree index 相对**权威基线**的零污染。

【为什么需要这个】
`r7_advance_maintainer_step2.py` 第 6 步用「上一轮推进前的 index」作为
参照来复原对方的暂存删除意图 —— 但那个 index 是**我自己在第一轮构造出来的**，
不是对方的原始 index。于是「参照集」被污染：
  * 对方真正暂存删除的文件（本轮推进前仍在 index 里）被误判成「已删除」而被移除
  * 共有 157 条被移除，其中 126 条是误判
权威参照必须是**本轮一切操作之前的原始 index**：
  `/tmp/r7_shared_index_before.bak`（1610 条，第一轮推进前的原始快照）

【判定口径】
  对方的暂存删除意图 = 旧主线 tree 中存在、但**原始 index** 里没有的路径
  正确状态 = 这批路径在当前 index 里也不存在
"""
from __future__ import annotations

import json

import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"   # 权威基线（1610 条）
CUR_INDEX = ("/root/projects/comfyui-minimax-h3-studio/.git/worktrees/"
             "h3studio-cont-seam-hires/index")


def git(*args: str, index: str | None = None) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=600, env=env)


def index_paths(index: str) -> set[str]:
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "ls-files", "-s", "-z"],
                         capture_output=True, timeout=300,
                         env={**__import__("os").environ,
                              "GIT_INDEX_FILE": index}).stdout
    out = set()
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        parts = rec.split(b"\t", 1)
        if len(parts) != 2:
            continue
        out.add(parts[1].decode("utf-8", "replace"))
    return out


def tree_paths(rev: str) -> set[str]:
    out: set[str] = set()
    for line in git("ls-tree", "-r", rev).stdout.splitlines():
        out.add(line.split("\t", 1)[1])
    return out


def main() -> int:
    orig = index_paths(ORIGINAL_INDEX)
    cur = index_paths(CUR_INDEX)
    t_old = tree_paths("d0a622f")
    t_new = tree_paths("76d61f8")

    # 对方的暂存删除意图（权威口径）
    staged_del = t_old - orig
    print(f"原始 index = {len(orig)} 条；当前 index = {len(cur)} 条")
    print(f"对方的暂存删除意图（权威口径）= {len(staged_del)} 条")

    wrongly_removed = sorted(p for p in staged_del if p in cur)
    still_staged_del = sorted(p for p in staged_del if p not in cur)
    print(f"  已正确复原（不在 index）= {len(still_staged_del)}")
    print(f"  ❌ 被误移除（仍在 index）= {len(wrongly_removed)}")

    # 主线新增文件必须全在 index
    land_new = t_new - t_old
    missing = sorted(p for p in land_new if p not in cur)
    print(f"主线新增文件 = {len(land_new)}；index 缺失 = {len(missing)}")

    # 对方真实暂存的 blob 必须逐字节不变
    fcb = tree_paths("fcb6057")
    orig_map = {}
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "ls-files", "-s", "-z"],
                         capture_output=True, timeout=300,
                         env={**__import__("os").environ,
                              "GIT_INDEX_FILE": ORIGINAL_INDEX}).stdout
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, blob, stage = meta.split()
        orig_map[path.decode("utf-8", "replace")] = blob.decode()
    cur_map = {}
    raw2 = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                           "ls-files", "-s", "-z"],
                          capture_output=True, timeout=300).stdout
    for rec in raw2.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, blob, stage = meta.split()
        cur_map[path.decode("utf-8", "replace")] = blob.decode()

    genuine = [p for p in orig_map
               if p not in fcb or orig_map[p] != fcb[p]]
    changed = [p for p in genuine if cur_map.get(p) != orig_map[p]]
    print(f"对方真实暂存 = {len(genuine)} 条；被改动 = {len(changed)} {changed[:5]}")

    if wrongly_removed or missing or changed:
        print("\n需要修正。")
        with open("/tmp/r7_index_fix_needed.json", "w", encoding="utf-8") as fh:
            json.dump({"wrongly_removed": wrongly_removed, "missing": missing},
                      fh, ensure_ascii=False)
        return 1
    print("\n✅ 零污染，无需修正")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
