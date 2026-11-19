#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：核验共享 worktree 里对方下一次 `git commit -a` 会不会删掉本轮成果。

【为什么必须单独核】
`git commit -a` 会把「工作区里已删除、但 index 里还有」的路径**暂存成删除**。
共享 worktree 的 index 现在是主线 tree（含本轮成果），
而工作区里有一批文件本来就不在（对方清理过的 evidence / 生成物）。
⇒ 对方一次 `git commit -a`，就可能把**本轮成果**从仓库里删掉。

【核验口径】
模拟对方的 `git commit -a`：
  1. 复制当前 index 到临时 index
  2. 在临时 index 上执行 `git add -A`（等价于 commit -a 的「暂存工作区变化」）
  3. `git write-tree` 得到「对方下一次提交」的树
  4. 与主线 tree 比对：**主线成果是否还在**
  5. 再做一次三方合并预演（对方提交 vs 主线），确认零冲突
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
MAIN = "local/maintainer"
SIM_INDEX = "/tmp/r7_commit_a_sim.index"


def git(*args: str, wt: str | None = None, index: str | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    proc = subprocess.run(["git", "-C", wt or WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=900, env=env)
    if check and proc.returncode != 0:
        raise SystemExit(f"git 失败: {' '.join(args)}\n{proc.stderr}")
    return proc


def tree_paths(rev: str) -> dict[str, str]:
    raw = subprocess.run(["git", "-C", REPO, "-c", "core.quotePath=false",
                          "ls-tree", "-r", "-z", rev],
                         capture_output=True, timeout=300).stdout
    out: dict[str, str] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        out[path.decode("utf-8", "replace")] = meta.decode().split()[2]
    return out


def main() -> int:
    main_tree = tree_paths(MAIN)
    print(f"主线 = {MAIN} @ {git('rev-parse', MAIN, wt=REPO).stdout.strip()[:7]}")
    print(f"主线成果（tree 条目）= {len(main_tree)}")

    real_index = git("rev-parse", "--git-path", "index").stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, SIM_INDEX)

    print("\n═══ 模拟对方的 `git commit -a` ═══")
    proc = git("add", "-A", index=SIM_INDEX)
    if proc.returncode != 0:
        print(proc.stderr[:800])
        raise SystemExit("模拟 add -A 失败")
    tree = git("write-tree", index=SIM_INDEX).stdout.strip()
    print(f"  对方下一次提交的 tree = {tree[:12]}")

    sim_tree = tree_paths(tree)
    lost = sorted(set(main_tree) - set(sim_tree))
    print(f"  对方提交后会从仓库消失的主线条目 = {len(lost)}")
    for p in lost[:25]:
        print("    ❌", p)

    # 关键：本轮本轮的成果（7 个生产文件 + 报告 + 测试）是否还在
    must_keep = [
        "server.py", "h3studio/video_models.py", "h3studio/artifact_meta.py",
        "h3studio/continuation_service.py", "h3studio/model_recommendations.py",
        "web/index.html", "web/video-panel.js",
        "tests/test_r7_review_fixes.py", "tests/mutation_r7_review_fixes.py",
        "docs/第7轮前后端代码审查报告-20261119.md",
        "h3studio/twopass_policy.py",
    ]
    print("\n  本轮关键成果在对方提交里是否还在：")
    for p in must_keep:
        ok = p in sim_tree
        print(f"    {'✅' if ok else '❌'} {p}")

    print("\n═══ 三方合并预演（对方提交 vs 主线）═══")
    commit = git("commit-tree", tree, "-p", MAIN, "-m",
                 "R7 预演：对方 commit -a 的结果（不落库）").stdout.strip()
    mb = git("merge-base", MAIN, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, MAIN, commit],
                        capture_output=True, text=True, timeout=900)
    print(f"  预演提交 = {commit[:7]}  三方合并退出码 = {mt.returncode}")
    if mt.returncode != 0:
        print(mt.stdout[:1500]); print(mt.stderr[:1500])
        raise SystemExit("预演有冲突")
    merged = mt.stdout.splitlines()[0].strip()
    mtree = tree_paths(merged)
    lost2 = sorted(set(main_tree) - set(mtree))
    print(f"  合并结果里主线成果缺失 = {len(lost2)}")
    for p in lost2[:25]:
        print("    ❌", p)
    print("\n  合并结果里本轮关键成果：")
    for p in must_keep:
        ok = p in mtree
        print(f"    {'✅' if ok else '❌'} {p}")
    os.remove(SIM_INDEX)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
