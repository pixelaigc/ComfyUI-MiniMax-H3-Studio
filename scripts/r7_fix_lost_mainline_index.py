#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：修复「第二步推进误移除主线新增文件的 index 条目」。

【事故（我自己的）】
`r7_advance_maintainer_step2.py` 第 6 步用「上一轮推进前的 index」当参照来
复原对方的暂存删除意图。那个 index 是**我第一轮构造出来的**，
参照集被污染 ⇒ 157 条里混进了 **126 条主线新增文件**
（本轮的 docs / evidence / scripts），它们被 `update-index --force-remove`
从 index 里删掉 ⇒ 在对方 worktree 里变成「未暂存删除」
⇒ **对方下一次 `git commit -a` 会把本轮成果全删掉**。

【正确口径】
  对方的暂存删除意图 = **fcb6057 tree 有、且原始 index 没有** 的路径（31 条）
  主线成果必须留在 index = **76d61f8 tree 有** 的路径
两者交叉求解：
  * 被误移除的「主线成果」→ 用 76d61f8 tree 的 blob 加回
  * 对方的暂存删除意图 → 若在 76d61f8 tree 里也有，则按对方意图移除
    （这 31 条是对方主动清理的生成物，主线里也留着 —— 对方意图优先）
"""
from __future__ import annotations

import os
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"
BASE = "fcb6057"
NEW = "76d61f8"


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
    new = tree_map(NEW)

    # 权威口径：对方的暂存删除意图
    staged_del = {p for p in base if p not in orig}
    # 主线成果（必须留在 index）
    mainline = set(new)
    # 被误移除的主线成果
    lost = sorted(mainline - set(cur))
    print(f"原始 index = {len(orig)} 条；当前 index = {len(cur)} 条")
    print(f"对方暂存删除意图 = {len(staged_del)} 条")
    print(f"主线成果（{NEW} tree）= {len(mainline)} 条；其中当前 index 缺失 = {len(lost)}")

    # 1) 把丢失的主线成果按新主线 blob 加回
    if lost:
        payload = [f"100644 {new[p]}\t{p}" for p in lost]
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SystemExit(f"加回失败: {proc.stderr}")
        print(f"  已加回 {len(lost)} 条主线成果")

    # 2) 对方的暂存删除意图优先：若它也在主线 tree 里，从 index 移除
    still = [p for p in sorted(staged_del) if p in index_map()]
    if still:
        for p in still:
            subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                           capture_output=True, timeout=300)
        print(f"  按对方意图强制移除 {len(still)} 条（对方清理的生成物，主线里也留着的）")

    # 3) 核验
    # 对方真实暂存（原始 index 有、且 blob ≠ fcb6057）必须逐字节不变
    genuine = {p for p in orig if p not in base or orig[p][1] != base[p]}
    cur2 = index_map()
    bad = [p for p in genuine if p in cur2 and cur2[p][1] != orig[p][1]]
    # 主线成果缺失 = 主线 tree 有、index 没有，且**不是**对方的暂存删除意图
    # （对方主动清理的生成物，主线里也留着 —— 对方意图优先，属预期缺失）
    lost2 = sorted(mainline - set(cur2) - staged_del)
    staged_left = [p for p in staged_del if p in cur2]
    expected_missing = sorted(staged_del & mainline)
    print("═══ 核验 ═══")
    print(f"  当前 index = {len(cur2)} 条")
    print(f"  主线成果**非预期**缺失 = {len(lost2)} {lost2[:5]}")
    print(f"  预期缺失（对方的暂存删除意图，主线里也有）= {len(expected_missing)}")
    print(f"  对方暂存删除意图仍在 index = {len(staged_left)} {staged_left[:5]}")
    print(f"  对方真实暂存 {len(genuine)} 条被改动 = {len(bad)} {bad[:5]}")
    if lost2 or staged_left or bad:
        raise SystemExit("核验失败")

    # 4) 「对方下一次提交」三方合并预演
    tree = git("write-tree").stdout.strip()
    commit = git("commit-tree", tree, "-p", NEW,
                 "-m", "R7 预演：对方下一次提交（不落库）").stdout.strip()
    mb = git("merge-base", NEW, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, NEW, commit],
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
              "docs/第7轮前后端代码审查报告-20261119.md",
              "evidence/r7-pytest-branch.txt.passed"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成：地雷已排除 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
