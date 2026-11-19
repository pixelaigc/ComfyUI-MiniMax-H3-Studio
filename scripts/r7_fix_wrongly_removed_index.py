#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：修复「第二步推进误移除共享 worktree index 条目」。

【事故】
`r7_advance_maintainer_step2.py` 第 6 步用「上一轮推进前的 index」当参照来
复原对方的暂存删除意图 —— 但那个 index 是**我自己第一轮构造出来的**，
不是对方的原始 index。于是「对方暂存删除」的参照集被污染：
  * 对方**没有**暂存删除、但因为主线推进而进入新 index 的文件，
    被误判成「已删除」而 `update-index --force-remove` 掉了
  * 后果：这些路径在对方 worktree 里变成「未暂存删除」
    ⇒ 对方下一次 `git add -A` / `git commit -a` 会把它们**从仓库里删掉**

【修复口径（权威）】
参照必须是**本轮一切操作之前的原始 index** `/tmp/r7_shared_index_before.bak`。
  * 对方的暂存删除意图 = fcb6057 tree 里有、原始 index 里没有的路径（31 条）
  * 其余「原始 index 里有、但当前 index 里没有」的路径 = **被误移除**，
    必须用**原始 index 里的 blob** 原样加回
"""
from __future__ import annotations

import json
import os
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"
BASE = "fcb6057"


def git(*args: str, index: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=600, env=env)


def index_map(index: str | None = None) -> dict[str, tuple[str, str]]:
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "ls-files", "-s", "-z"],
                         capture_output=True, timeout=300,
                         env={**os.environ, "GIT_INDEX_FILE": index}
                         if index else None).stdout
    out: dict[str, tuple[str, str]] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        parts = rec.split(b"\t", 1)
        if len(parts) != 2:
            continue
        meta = parts[0].split()
        out[parts[1].decode("utf-8", "replace")] = (meta[0].decode(), meta[1].decode())
    return out


def tree_map(rev: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in git("ls-tree", "-r", rev).stdout.splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def main() -> int:
    orig = index_map(ORIGINAL_INDEX)
    cur = index_map()
    base = tree_map(BASE)

    # 对方的暂存删除意图（权威口径）
    staged_del = sorted(p for p in base if p not in orig)
    # 被误移除：原始 index 里有、当前 index 里没有的路径
    wrongly_removed = sorted(p for p in orig if p not in cur)

    print(f"原始 index = {len(orig)} 条；当前 index = {len(cur)} 条")
    print(f"对方的暂存删除意图 = {len(staged_del)} 条")
    print(f"被误移除的条目 = {len(wrongly_removed)} 条")

    # 1) 把误移除的条目按**原始 blob** 原样加回
    if wrongly_removed:
        payload = [f"{orig[p][0]} {orig[p][1]}\t{p}" for p in wrongly_removed]
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SystemExit(f"加回失败: {proc.stderr}")
        print(f"  已加回 {len(wrongly_removed)} 条（blob 取自原始 index）")

    # 2) 确认对方的暂存删除意图确实不在 index 里
    cur2 = index_map()
    still = [p for p in staged_del if p in cur2]
    print(f"  对方暂存删除意图仍留在 index 的 = {len(still)} {still[:5]}")
    if still:
        for p in still:
            subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                           capture_output=True, timeout=300)
        print(f"  已强制移除 {len(still)} 条")

    # 3) 核验
    cur3 = index_map()
    bad_blob = [p for p in orig
                if p in cur3 and cur3[p][1] != orig[p][1] and p not in staged_del]
    print("═══ 核验 ═══")
    print(f"  当前 index = {len(cur3)} 条")
    print(f"  对方真实暂存（原始 index 有、且 blob ≠ fcb6057）被改动 = {len(bad_blob)} {bad_blob[:5]}")
    print(f"  对方暂存删除意图仍在 index = {len([p for p in staged_del if p in cur3])}")

    # 4) 「对方下一次提交」三方合并预演
    tree = git("write-tree").stdout.strip()
    commit = git("commit-tree", tree, "-p", "76d61f8",
                 "-m", "R7 预演：对方下一次提交（不落库）").stdout.strip()
    mb = git("merge-base", "76d61f8", commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, "76d61f8", commit],
                        capture_output=True, text=True, timeout=600)
    print(f"  三方合并预演退出码 = {mt.returncode}")
    if mt.returncode != 0:
        print(mt.stdout[:1200]); print(mt.stderr[:1200])
        raise SystemExit("预演有冲突")
    merged = mt.stdout.splitlines()[0].strip()
    names = {l.split("\t", 1)[1] for l in
             subprocess.run(["git", "-C", REPO, "ls-tree", "-r", merged],
                            capture_output=True, text=True).stdout.splitlines()}
    for k in ["h3studio/twopass_policy.py", "server.py", "web/index.html",
              "tests/test_r7_review_fixes.py",
              "docs/第7轮前后端代码审查报告-20261119.md"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
