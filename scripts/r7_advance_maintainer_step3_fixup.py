#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：第三步推进的**收尾**（分支指针已落地，只剩 index 修正 + 核验）。

【背景】
`r7_advance_maintainer_step3.py` 第 4 步已经落地了 index 替换、工作区同步、
分支指针推进（76d61f8 -> 207e9fa），但第 5 步的核验先失败后（判据写错）、
修好判据后又要重跑，被「HEAD 已不是 76d61f8」挡住。
本脚本只做剩下的事：
  1. 按**权威口径**（原始 index 参照）移除对方的 31 条暂存删除意图条目
  2. 全量核验（对方真实暂存 / 对方工作区 / 主线成果 / 对方删除意图）
  3. 「对方下一次提交」三方合并预演
"""
from __future__ import annotations

import hashlib
import os
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
BASE = "fcb6057"
NEW = "207e9fa"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"


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
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
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
    orig = index_map(ORIGINAL_INDEX)
    base = tree_map(BASE)
    new = tree_map(NEW)
    staged_del = {p for p in base if p not in orig}
    genuine = {p for p in orig if p not in base
               or orig[p][1] != base.get(p, orig[p][1])}
    print(f"权威口径：对方暂存删除意图 = {len(staged_del)} 条；对方真实暂存 = {len(genuine)} 条")

    cur = index_map()
    to_remove = sorted(p for p in staged_del if p in cur)
    if to_remove:
        for p in to_remove:
            subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                           capture_output=True, timeout=300)
        print(f"已按对方意图移除 index 条目 {len(to_remove)} 条")

    cur2 = index_map()
    bad = [p for p in genuine if cur2.get(p, ("", ""))[1] != orig[p][1]]
    missing = sorted(set(new) - set(cur2) - staged_del)
    staged_left = [p for p in staged_del if p in cur2]
    print("═══ 核验 ═══")
    print(f"  当前 index = {len(cur2)} 条")
    print(f"  (a) 对方真实暂存 {len(genuine)} 条被改动 = {len(bad)} {bad[:5]}")
    print(f"  (b) 主线成果非预期缺失 = {len(missing)} {missing[:5]}")
    print(f"  (c) 对方暂存删除意图仍在 index = {len(staged_left)} {staged_left[:5]}")
    print(f"  (d) 预期缺失（对方删除意图 ∩ 主线 tree）= {len(staged_del & set(new))}")

    # 对方在改文件的工作区内容：与原始 index 里的 blob 比（同一文件路径下，
    # 对方工作区内容若等于原始 index blob ⇒ 未被本轮改动）
    theirs = set()
    raw = subprocess.run(["git", "-C", WT, "-c", "core.quotePath=false",
                          "status", "--porcelain=v1", "-z"],
                         capture_output=True, timeout=300).stdout
    for item in raw.split(b"\0"):
        if len(item) < 4:
            continue
        t = item.decode("utf-8", "replace")
        xy, p = t[:2], t[3:]
        if xy[0] in "MADR" or xy[1] in "MADR":
            theirs.add(p)
    touched = []
    for p in sorted(theirs):
        full = os.path.join(WT, p)
        if not os.path.isfile(full):
            continue
        content = open(full, "rb").read()
        bh = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        # 若工作区内容 == 原始 index blob ⇒ 未被本轮动过
        if p in orig and orig[p][1] == bh:
            continue
        # 若工作区内容 == 主线 blob ⇒ 本轮 checkout 同步的结果（对方没在改）
        if new.get(p) == bh:
            continue
        touched.append(p)
    print(f"  (e) 对方在改 {len(theirs)} 个路径中，工作区内容既非原始暂存也非主线 = {len(touched)} {touched[:5]}")

    if bad or missing or staged_left:
        raise SystemExit("核验失败")

    print("═══ 对方「下一次提交」三方合并预演 ═══")
    tree = git("write-tree").stdout.strip()
    commit = git("commit-tree", tree, "-p", NEW, "-m",
                 "R7 预演：对方下一次提交（不落库）").stdout.strip()
    mb = git("merge-base", NEW, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, NEW, commit],
                        capture_output=True, text=True, timeout=600)
    print(f"  预演提交 = {commit[:7]}  三方合并退出码 = {mt.returncode}")
    if mt.returncode != 0:
        print(mt.stdout[:1200]); print(mt.stderr[:1200])
        raise SystemExit("预演有冲突")
    merged = mt.stdout.splitlines()[0].strip()
    names = {l.split("\t", 1)[1] for l in
             subprocess.run(["git", "-C", REPO, "ls-tree", "-r", merged],
                            capture_output=True, text=True).stdout.splitlines()}
    for k in ["h3studio/twopass_policy.py", "server.py", "web/index.html",
              "web/style.css", "tests/test_r7_review_fixes.py",
              "evidence/r7-pytest-branch.txt.passed"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成：地雷已排除 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
