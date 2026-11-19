#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：把 local/maintainer 推进到新主线，**零污染**共享 worktree。

【背景 —— 为什么不能裸 git update-ref】
local/maintainer 被共享 worktree `/root/projects/h3studio-cont-seam-hires` 检出着。
`git update-ref` 只移动分支指针，**不刷新**那个 worktree 的 index：
  * 主线新增的文件在对方 index 里不存在 -> 变成「暂存删除」
    => 对方下一次 commit 会把本轮成果**删掉**
  * 主线改过的文件在对方 index 里还是旧 blob -> 变成「陈旧 M」
    => 对方下一次 commit 会把本轮修复**回退掉**
（第 6 轮踩过这个坑，docs/接续段标准采样路径改造-20261118.md §5.5 有完整记录。
 那一轮留下的 31 条「暂存删除」污染残留至今还在，本脚本一并清掉。）

【本脚本的做法 —— 复刻 git 原生 FF 的语义】
git 在干净 worktree 里做 FF 时会做三件事：
  1. index 更新到新 HEAD
  2. 工作区文件更新到新 HEAD
  3. 本地修改过的文件**保留本地内容**
本脚本在「有本地修改」的共享 worktree 里手工做同样的事：
  1. 新 index 基底 = 新主线 tree
  2. 把对方**真实暂存**的 34 条 blob 逐字节叠回新 index（对方暂存意图一个字节不动）
  3. 把主线变更但**对方没在改**的路径，工作区 checkout 到新主线版本
  4. 主线变更且**对方也在改**的路径：工作区不动（对方的 WIP 优先，冲突留给对方可见）
  5. 最后才移动分支指针

【硬约束】
  * 对方工作区文件内容，一个字节都不动（59 个在改文件 sha256 全等）
  * 对方真实暂存的 34 条 blob，逐字节全等
  * 对方的 31 条「历史污染暂存删除」会被清掉（它们不是对方的真实意图）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
OLD = "fcb6057"
NEW = "d0a622f"
MAIN = "local/maintainer"
TMP_INDEX = "/tmp/r7_new_index"


def git(*args: str, wt: str | None = None, index: str | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    cmd = ["git", "-C", wt or REPO, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    if check and proc.returncode != 0:
        raise SystemExit(f"git 失败: {' '.join(args)}\n{proc.stderr}")
    return proc


def ls_index(index: str | None = None) -> dict[str, dict[str, str]]:
    out = git("ls-files", "-s", wt=WT, index=index).stdout
    res: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        res[parts[3]] = {"mode": parts[0], "blob": parts[1]}
    return res


def ls_tree(rev: str) -> dict[str, tuple[str, str]]:
    res: dict[str, tuple[str, str]] = {}
    for line in git("ls-tree", "-r", rev).stdout.splitlines():
        meta, path = line.split("\t", 1)
        mode, _typ, blob = meta.split()
        res[path] = (mode, blob)
    return res


def worktree_fingerprint(paths: list[str]) -> dict[str, str]:
    fp: dict[str, str] = {}
    for p in paths:
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    return fp


def status_porcelain() -> list[tuple[str, str]]:
    raw = subprocess.run(["git", "-C", WT, "status", "--porcelain=v1", "-z"],
                         capture_output=True, timeout=300).stdout
    res = []
    for item in raw.split(b"\0"):
        if len(item) < 4:
            continue
        t = item.decode("utf-8", "replace")
        res.append((t[:2], t[3:]))
    return res


def main() -> int:
    dry = "--dry-run" in sys.argv
    print("═══ 0. 前置核验 ═══")
    head = git("rev-parse", "HEAD", wt=WT).stdout.strip()
    main_head = git("rev-parse", MAIN).stdout.strip()
    if not head.startswith(OLD):
        raise SystemExit(f"共享 worktree HEAD 不是 {OLD}（是 {head}），停")
    if not main_head.startswith(OLD):
        raise SystemExit(f"{MAIN} 不是 {OLD}（是 {main_head}），停")
    if git("merge-base", "--is-ancestor", OLD, NEW).returncode != 0:
        raise SystemExit(f"{NEW} 不是 {OLD} 的后代（非快进），停")
    # 并发保护：共享 worktree 近 3 分钟不得有文件写入
    recent = subprocess.run(
        ["find", WT, "-newermt", "-3 minutes", "-type", "f",
         "-not", "-path", f"{WT}/.git/*"],
        capture_output=True, text=True, timeout=300).stdout.strip()
    if recent:
        print("  !! 检测到共享 worktree 近 3 分钟被写入，疑似并发：")
        print("\n".join(recent.splitlines()[:10]))
        raise SystemExit("停：需人工确认")
    print(f"  HEAD={head[:7]}  {MAIN}={main_head[:7]}  FF 可行  无并发写入 ✅")

    # ---- 分类 ----
    idx_before = ls_index()
    old_tree = ls_tree(OLD)
    new_tree = ls_tree(NEW)
    st = status_porcelain()
    theirs = {p for xy, p in st if xy[0] in "MADR" or xy[1] in "MADR"}
    genuine_staged = {p for p in idx_before
                      if p not in old_tree or idx_before[p]["blob"] != old_tree[p][1]}
    changed = sorted(p for p in set(old_tree) | set(new_tree)
                     if old_tree.get(p) != new_tree.get(p))
    overlap = [p for p in changed if p in theirs]
    sync_paths = [p for p in changed if p not in theirs]
    print("═══ 1. 分类 ═══")
    print(f"  主线变更路径 = {len(changed)}")
    print(f"  对方在改路径 = {len(theirs)}（其中与主线重叠 = {len(overlap)}）")
    print(f"  需同步工作区的路径 = {len(sync_paths)}")
    print(f"  对方真实暂存 = {len(genuine_staged)} 条（逐字节保留）")

    fp_before = worktree_fingerprint(sorted(theirs))

    # ---- 构造新 index ----
    print("═══ 2. 构造新 index（基底 = 新主线 + 叠回对方真实暂存）═══")
    if os.path.exists(TMP_INDEX):
        os.remove(TMP_INDEX)
    git("read-tree", NEW, index=TMP_INDEX, check=True)
    payload = []
    for p in sorted(genuine_staged):
        info = idx_before[p]
        payload.append(f"{info['mode']} {info['blob']}\t{p}")
    if payload:
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "GIT_INDEX_FILE": TMP_INDEX})
        if proc.returncode != 0:
            raise SystemExit(f"update-index 失败: {proc.stderr}")
    new_idx = ls_index(TMP_INDEX)
    print(f"  新 index 条目 = {len(new_idx)}（真实 index = {len(idx_before)}）")

    # ---- 核验新 index ----
    bad_staged = [p for p in genuine_staged if new_idx.get(p, {}).get("blob") != idx_before[p]["blob"]]
    land_new = [p for p in new_tree if p not in old_tree]
    miss_new = [p for p in land_new if p not in new_idx]
    changed_mod = [p for p in changed if p in old_tree]
    miss_mod = [p for p in changed_mod if p not in theirs and new_idx.get(p, {}).get("blob") != new_tree[p][1]]
    print("═══ 3. 新 index 自检 ═══")
    print(f"  对方真实暂存被改动 = {len(bad_staged)} {bad_staged[:5]}")
    print(f"  主线新增文件在 index 缺失 = {len(miss_new)}")
    print(f"  主线修改文件 index 未跟上 = {len(miss_mod)} {miss_mod[:5]}")
    if bad_staged or miss_new or miss_mod:
        raise SystemExit("新 index 自检失败，未落地")
    print("  ✅ 全部通过")

    if dry:
        print("（dry-run：未落地）")
        return 0

    # ---- 落地：先备份，再换 index ----
    print("═══ 4. 落地 ═══")
    real_index = git("rev-parse", "--git-path", "index", wt=WT).stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, "/tmp/r7_shared_index_pre_advance.bak")
    shutil.copy2(TMP_INDEX, real_index)
    print(f"  index 已替换（原文件备份 /tmp/r7_shared_index_pre_advance.bak）")

    # ---- 同步工作区（仅对方没在改的路径）----
    if sync_paths:
        with open("/tmp/r7_sync_paths.txt", "w") as fh:
            fh.write("\n".join(sync_paths) + "\n")
        proc = subprocess.run(
            ["git", "-C", WT, "checkout", NEW, "--pathspec-from-file=/tmp/r7_sync_paths.txt"],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SystemExit(f"工作区同步失败:\n{proc.stderr}")
        print(f"  工作区已同步 {len(sync_paths)} 个路径到新主线")
    print(f"  {len(overlap)} 个重叠路径的工作区保持对方内容不动")

    # ---- 移动分支指针 ----
    git("update-ref", f"refs/heads/{MAIN}", NEW, OLD, check=True)
    print(f"  {MAIN}: {OLD} -> {NEW}")

    # ---- 落地后核验 ----
    print("═══ 5. 落地后核验 ═══")
    idx_after = ls_index()
    bad_after = [p for p in genuine_staged
                 if idx_after.get(p, {}).get("blob") != idx_before[p]["blob"]]
    fp_after = worktree_fingerprint(sorted(theirs))
    diff_fp = [p for p in fp_before if fp_before[p] != fp_after.get(p)]
    st_after = status_porcelain()
    staged_del_after = sorted(p for xy, p in st_after if xy[0] == "D")
    land_new_after = [p for p in land_new if p not in idx_after]
    print(f"  (a) 对方真实暂存 {len(genuine_staged)} 条被改动 = {len(bad_after)} {bad_after[:5]}")
    print(f"  (b) 对方在改文件工作区内容变化 = {len(diff_fp)} {diff_fp[:5]}")
    print(f"  (c) 暂存删除残留 = {len(staged_del_after)}（推进前 31 条历史污染应被清掉）")
    print(f"  (d) 主线新增文件在 index 缺失 = {len(land_new_after)}")
    if bad_after or diff_fp or land_new_after:
        raise SystemExit("落地后核验失败")
    if staged_del_after:
        print(f"      仍存在的暂存删除: {staged_del_after[:8]}")
    print("  ✅ 零污染")

    # ---- 对方「下一次提交」三方合并预演 ----
    print("═══ 6. 对方下一次提交的三方合并预演 ═══")
    tree = git("write-tree", wt=WT).stdout.strip()
    commit = git("commit-tree", tree, "-p", NEW,
                 "-m", "R7 预演：对方下一次提交（不落库）", wt=WT).stdout.strip()
    mb = git("merge-base", NEW, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", mb, NEW, commit],
                        capture_output=True, text=True, timeout=600)
    rc = mt.returncode
    print(f"  预演提交 = {commit[:7]}  三方合并退出码 = {rc}")
    if rc != 0:
        print(mt.stdout[:2000])
        print(mt.stderr[:2000])
        raise SystemExit("预演有冲突，需要人工处理")
    merged = mt.stdout.splitlines()[0].strip()
    names = {l.split("\t", 1)[1] for l in
             git("ls-tree", "-r", merged).stdout.splitlines()}
    key = ["h3studio/twopass_policy.py", "server.py", "web/index.html"]
    for k in key:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
