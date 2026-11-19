#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：消除共享 worktree 里「对方 commit -a 会删掉主线成果」的地雷。

【地雷是什么】
共享 worktree `/root/projects/h3studio-cont-seam-hires` 的 index 里，
有一批**主线成果**（第 6 轮新增的 `h3studio/twopass_policy.py`、
`h3studio/twopass_runtime.py` 等）在 index 里存在、但**工作区里没有文件**。

⇒ 对方下一次 `git commit -a` 会把它们**暂存成删除**，从而从仓库里删掉。

【这地雷是谁埋的】
不是本轮推进造成的 —— 原始 index（`/tmp/r7_shared_index_before.bak`，1610 条，
本轮一切操作之前）里它们就在，blob 与现在**完全一致**（`d8b6ebc1…`），
且本轮所有 sync 清单里都没有它们。
⇒ 是**对方 worktree 的既有状态**（第 6 轮推进主线时留下的）。

【本脚本做什么】
把「index 有、工作区无」的主线成果**检出到工作区** —— 让 index 与工作区一致，
对方 commit -a 就不会误删。

【安全规则】
  * 只处理「主线 tree 有」且「工作区缺」的路径
  * 跳过对方**明确暂存删除**的 31 条（那是对方清理生成物的意图）
  * 逐个 checkout，不碰对方工作区里已有的文件
"""
from __future__ import annotations

import os
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
MAIN = "local/maintainer"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"
BASE = "fcb6057"


def git(*args: str, index: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=900, env=env)


def index_paths(index: str | None = None) -> set[str]:
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "ls-files", "-s", "-z"],
                         capture_output=True, timeout=300,
                         env={**os.environ, "GIT_INDEX_FILE": index}
                         if index else None).stdout
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
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "ls-tree", "-r", "-z", rev],
                         capture_output=True, timeout=300).stdout
    out = set()
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        _meta, path = rec.split(b"\t", 1)
        out.add(path.decode("utf-8", "replace"))
    return out


def main() -> int:
    orig = index_paths(ORIGINAL_INDEX)
    base = tree_paths(BASE)
    main_tree = tree_paths(MAIN)
    cur = index_paths()
    staged_del = base - orig          # 对方明确的暂存删除意图

    # 「index 有、工作区无」的路径
    missing = sorted(p for p in cur
                     if not os.path.exists(os.path.join(WT, p)))
    print(f"index 条目 = {len(cur)}；工作区缺文件 = {len(missing)}")

    # 分三类
    mainline_at_risk = [p for p in missing
                        if p in main_tree and p not in staged_del]
    others = [p for p in missing if p not in mainline_at_risk]

    print(f"  ① 主线成果且工作区缺 = {len(mainline_at_risk)}"
          f"（对方 commit -a 会删掉 ⇒ 必须检出）")
    print(f"  ② 其余 = {len(others)}"
          f"（对方自己的删除/清理意图，不动）")

    if mainline_at_risk:
        with open("/tmp/r7_restore_missing.txt", "w") as fh:
            fh.write("\n".join(mainline_at_risk) + "\n")
        proc = subprocess.run(
            ["git", "-C", WT, "checkout", MAIN,
             "--pathspec-from-file=/tmp/r7_restore_missing.txt"],
            capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            print(proc.stderr[:800])
            raise SystemExit("检出失败")
        print(f"  已检出 {len(mainline_at_risk)} 条到工作区")

    print("\n═══ 复核 ═══")
    cur2 = index_paths()
    still = [p for p in mainline_at_risk
             if not os.path.exists(os.path.join(WT, p))]
    print(f"  仍缺的主线成果 = {len(still)} {still[:5]}")
    print(f"  当前 index 条目 = {len(cur2)}")

    # 复跑「对方 commit -a」模拟，确认本轮关键成果不再消失
    import shutil
    SIM = "/tmp/r7_commit_a_sim2.index"
    real_index = git("rev-parse", "--git-path", "index").stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, SIM)
    subprocess.run(["git", "-C", WT, "add", "-A"],
                   capture_output=True,
                   env={**os.environ, "GIT_INDEX_FILE": SIM}, timeout=900)
    tree = git("write-tree", index=SIM).stdout.strip()
    sim = tree_paths(tree)
    must_keep = [
        "server.py", "h3studio/video_models.py", "h3studio/artifact_meta.py",
        "h3studio/continuation_service.py", "h3studio/model_recommendations.py",
        "web/index.html", "web/video-panel.js",
        "tests/test_r7_review_fixes.py", "tests/mutation_r7_review_fixes.py",
        "docs/第7轮前后端代码审查报告-20261119.md",
        "h3studio/twopass_policy.py",
    ]
    print("\n  模拟对方 commit -a 后，本轮+第6轮关键成果是否还在：")
    bad = []
    for p in must_keep:
        ok = p in sim
        if not ok:
            bad.append(p)
        print(f"    {'✅' if ok else '❌'} {p}")
    os.remove(SIM)
    if bad:
        print(f"\n❌ 仍有 {len(bad)} 条会丢：{bad}")
        return 1
    print("\n✅ 地雷已排除：对方 commit -a 不会再删掉本轮/第 6 轮成果")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
