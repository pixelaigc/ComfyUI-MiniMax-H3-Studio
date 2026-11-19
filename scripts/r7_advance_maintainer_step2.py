#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 第二步推进：把 `local/maintainer` 从 `d0a622f` 推到 `76d61f8`。

【与 r7_advance_maintainer_safe.py 的区别】
第一版脚本是为「跨多 commit 的首次推进」写的（含 landmine 分类）。
本轮推进只多了一个 commit（全是 docs / evidence / scripts，**零生产代码**），
但仍要过同一套零污染核验，所以复用同一套流程，参数化 OLD/NEW。

【核验清单（与第一版一致）】
  1. 对方真实暂存的 blob 逐字节不变
  2. 对方在改文件的工作区内容一个字节不动
  3. 对方「暂存删除」意图原样保留
  4. 主线新增文件在 index 全部就位
  5. 对方「下一次提交」三方合并预演零冲突
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
OLD = "d0a622f"
NEW = "76d61f8"
MAIN = "local/maintainer"
TMP_INDEX = "/tmp/r7_new_index2"


def git(*args: str, wt: str | None = None, index: str | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    proc = subprocess.run(["git", "-C", wt or REPO, *args],
                          capture_output=True, text=True, timeout=600, env=env)
    if check and proc.returncode != 0:
        raise SystemExit(f"git 失败: {' '.join(args)}\n{proc.stderr}")
    return proc


def index_map(index: str | None = None) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    raw = git("ls-files", "-s", wt=WT, index=index).stdout
    for line in raw.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        out[parts[3]] = {"mode": parts[0], "blob": parts[1]}
    return out


def tree_map(rev: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in git("ls-tree", "-r", rev).stdout.splitlines():
        meta, path = line.split("\t", 1)
        out[path] = meta.split()[2]
    return out


def status_pairs() -> list[tuple[str, str]]:
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
    print("═══ 0. 前置核验 ═══")
    head = git("rev-parse", "HEAD", wt=WT).stdout.strip()
    main_head = git("rev-parse", MAIN).stdout.strip()
    if not head.startswith(OLD):
        raise SystemExit(f"共享 worktree HEAD 不是 {OLD}（是 {head}），停")
    if not main_head.startswith(OLD):
        raise SystemExit(f"{MAIN} 不是 {OLD}（是 {main_head}），停")
    if git("merge-base", "--is-ancestor", OLD, NEW).returncode != 0:
        raise SystemExit(f"{NEW} 不是 {OLD} 的后代（非快进），停")
    recent = subprocess.run(
        ["find", WT, "-newermt", "-5 minutes", "-type", "f",
         "-not", "-path", f"{WT}/.git/*"],
        capture_output=True, text=True, timeout=300).stdout.strip()
    if recent:
        print("  !! 共享 worktree 近 5 分钟被写入，疑似并发：")
        print("\n".join(recent.splitlines()[:10]))
        raise SystemExit("停：需人工确认")
    print(f"  HEAD={head[:7]}  {MAIN}={main_head[:7]}  FF 可行  无并发写入 ✅")

    idx_before = index_map()
    old_tree = tree_map(OLD)
    new_tree = tree_map(NEW)
    st = status_pairs()
    theirs = {p for xy, p in st if xy[0] in "MADR" or xy[1] in "MADR"}
    genuine = {p for p in idx_before
               if p not in old_tree or idx_before[p]["blob"] != old_tree[p]}
    changed = sorted(p for p in set(old_tree) | set(new_tree)
                     if old_tree.get(p) != new_tree.get(p))
    overlap = [p for p in changed if p in theirs]
    sync_paths = [p for p in changed if p not in theirs]
    print("═══ 1. 分类 ═══")
    print(f"  主线变更路径 = {len(changed)}；对方在改 = {len(theirs)}（重叠 {len(overlap)}）")
    print(f"  需同步工作区 = {len(sync_paths)}；对方真实暂存 = {len(genuine)} 条")

    fp_before = {}
    for p in sorted(theirs):
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp_before[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()

    print("═══ 2. 构造新 index ═══")
    if os.path.exists(TMP_INDEX):
        os.remove(TMP_INDEX)
    git("read-tree", NEW, index=TMP_INDEX, check=True)
    payload = [f"{idx_before[p]['mode']} {idx_before[p]['blob']}\t{p}"
               for p in sorted(genuine)]
    if payload:
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "GIT_INDEX_FILE": TMP_INDEX})
        if proc.returncode != 0:
            raise SystemExit(f"update-index 失败: {proc.stderr}")
    new_idx = index_map(TMP_INDEX)
    print(f"  新 index 条目 = {len(new_idx)}（原 {len(idx_before)}）")

    print("═══ 3. 自检 ═══")
    bad = [p for p in genuine if new_idx.get(p, {}).get("blob") != idx_before[p]["blob"]]
    land_new = [p for p in new_tree if p not in old_tree]
    miss_new = [p for p in land_new if p not in new_idx]
    changed_mod = [p for p in changed if p in old_tree]
    miss_mod = [p for p in changed_mod
                if p not in theirs and new_idx.get(p, {}).get("blob") != new_tree[p]]
    print(f"  对方真实暂存被改动 = {len(bad)} {bad[:5]}")
    print(f"  主线新增文件缺失 = {len(miss_new)}")
    print(f"  主线修改文件未跟上 = {len(miss_mod)} {miss_mod[:5]}")
    if bad or miss_new or miss_mod:
        raise SystemExit("自检失败，未落地")

    print("═══ 4. 落地 ═══")
    real_index = git("rev-parse", "--git-path", "index", wt=WT).stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, "/tmp/r7_shared_index_pre_advance2.bak")
    shutil.copy2(TMP_INDEX, real_index)
    print("  index 已替换（备份 /tmp/r7_shared_index_pre_advance2.bak）")
    if sync_paths:
        with open("/tmp/r7_sync_paths2.txt", "w") as fh:
            fh.write("\n".join(sync_paths) + "\n")
        proc = subprocess.run(
            ["git", "-C", WT, "checkout", NEW,
             "--pathspec-from-file=/tmp/r7_sync_paths2.txt"],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SystemExit(f"工作区同步失败:\n{proc.stderr}")
        print(f"  工作区已同步 {len(sync_paths)} 个路径")
    git("update-ref", f"refs/heads/{MAIN}", NEW, OLD, check=True)
    print(f"  {MAIN}: {OLD} -> {NEW}")

    print("═══ 5. 落地后核验 ═══")
    idx_after = index_map()
    bad_after = [p for p in genuine
                 if idx_after.get(p, {}).get("blob") != idx_before[p]["blob"]]
    fp_after = {}
    for p, h in fp_before.items():
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp_after[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    diff_fp = [p for p in fp_before if fp_before[p] != fp_after.get(p)]
    land_new_after = [p for p in land_new if p not in idx_after]
    print(f"  (a) 对方真实暂存被改动 = {len(bad_after)} {bad_after[:5]}")
    print(f"  (b) 对方在改文件工作区内容变化 = {len(diff_fp)} {diff_fp[:5]}")
    print(f"  (c) 主线新增文件在 index 缺失 = {len(land_new_after)}")
    if bad_after or diff_fp or land_new_after:
        raise SystemExit("落地后核验失败")
    print("  ✅ 零污染")

    print("═══ 6. 复原对方的暂存删除意图 ═══")
    backup = "/tmp/r7_shared_index_pre_advance2.bak"
    raw = subprocess.run(["git", "-C", WT, "ls-files", "-s", "-z"],
                         capture_output=True, timeout=300,
                         env={**os.environ, "GIT_INDEX_FILE": backup}).stdout
    idx_backup = set()
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        _m, path = rec.split(b"\t", 1)
        idx_backup.add(path.decode("utf-8", "replace"))
    staged_del = sorted((set(old_tree) - idx_backup) & set(new_tree))
    to_remove = [p for p in staged_del if p in idx_after]
    for p in to_remove:
        subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                       capture_output=True, text=True, timeout=300)
    print(f"  对方暂存删除意图 = {len(staged_del)} 条，已复原 {len(to_remove)} 条")

    idx_final = index_map()
    still = [p for p in to_remove if p in idx_final]
    missing = [p for p in land_new if p not in idx_final]
    print(f"  核验：仍留在 index = {len(still)}；主线新增缺失 = {len(missing)}")

    print("═══ 7. 对方「下一次提交」三方合并预演 ═══")
    tree = git("write-tree", wt=WT).stdout.strip()
    commit = git("commit-tree", tree, "-p", NEW,
                 "-m", "R7 预演：对方下一次提交（不落库）", wt=WT).stdout.strip()
    mb = git("merge-base", NEW, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, NEW, commit],
                        capture_output=True, text=True, timeout=600)
    print(f"  预演提交 = {commit[:7]}  三方合并退出码 = {mt.returncode}")
    if mt.returncode != 0:
        print(mt.stdout[:1500]); print(mt.stderr[:1500])
        raise SystemExit("预演有冲突")
    merged = mt.stdout.splitlines()[0].strip()
    names = {l.split("\t", 1)[1] for l in git("ls-tree", "-r", merged).stdout.splitlines()}
    for k in ["h3studio/twopass_policy.py", "server.py", "web/index.html",
              "tests/test_r7_review_fixes.py",
              "docs/第7轮前后端代码审查报告-20261119.md"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
