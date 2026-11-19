#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7：零污染推进 `local/maintainer`（**参数化**版，后续轮次直接用）。

【为什么参数化】
前几版脚本把 OLD / NEW 写死在文件里，导致每次推进都要改一次脚本、
提交一次、再推进一次 —— 死循环（step4 已经改了两轮）。
本版从命令行 / 环境变量取，脚本本身不再需要随主线改动。

【用法】
    # 方式 1：显式指定
    python scripts/r7_advance_maintainer.py <old_commit> <new_commit>

    # 方式 2：从环境变量（CI / 一次性 shell）
    R7_OLD=12248b6 R7_NEW=7489061 python scripts/r7_advance_maintainer.py

    # 方式 3：不给参数 = 自动推断
    #   OLD = 当前 local/maintainer 的 HEAD
    #   NEW = 与之最近的一个「含本轮成果」的 commit（由调用方决定）

【权威口径（两轮踩坑后固化）】
  参照 = `/tmp/r7_shared_index_before.bak`（本轮一切操作前的原始 index 快照）
  * 对方的暂存删除意图 = BASE tree 有、但**原始 index** 里没有的路径
  * 主线成果 = NEW tree 里的路径，**必须**在 index 里
  * 交叉冲突（既在主线 tree 又在对方删除清单）→ **对方意图优先**
  * 对方真实暂存（原始 index 有、且 blob ≠ BASE tree）→ blob 逐字节不动

【核验清单】
  1. 对方真实暂存 blob 逐字节不变
  2. 对方在改文件的工作区内容一个字节不动
  3. 对方的暂存删除意图原样保留（不在 index 里）
  4. 主线成果在 index 里全部就位（除被对方删除的）
  5. 对方「下一次 commit -a」模拟：关键成果不丢
  6. 对方「下一次提交」三方合并预演：零冲突
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
BASE = "fcb6057"
MAIN = "local/maintainer"
ORIGINAL_INDEX = os.environ.get("R7_ORIGINAL_INDEX",
                                "/tmp/r7_shared_index_before.bak")


def git(*args: str, index: str | None = None, wt: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if index:
        env["GIT_INDEX_FILE"] = index
    return subprocess.run(["git", "-C", wt or WT, "-c", "core.quotePath=false", *args],
                          capture_output=True, text=True, timeout=900, env=env)


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


def resolve_targets() -> tuple[str, str]:
    old = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("R7_OLD", "")
    new = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("R7_NEW", "")
    if not old:
        old = git("rev-parse", MAIN).stdout.strip()
    if not new:
        raise SystemExit("必须给 NEW commit（命令行参数或 R7_NEW 环境变量）")
    return old, new


def main() -> int:
    old, new = resolve_targets()
    print("═══ 0. 前置核验 ═══")
    head = git("rev-parse", "HEAD").stdout.strip()
    main_head = git("rev-parse", MAIN).stdout.strip()
    if not head.startswith(old):
        raise SystemExit(f"共享 worktree HEAD 不是 {old}（{head}），停")
    if not main_head.startswith(old):
        raise SystemExit(f"{MAIN} 不是 {old}（{main_head}），停")
    if git("merge-base", "--is-ancestor", old, new).returncode != 0:
        raise SystemExit(f"{new} 不是 {old} 的后代，停")
    if not os.path.exists(ORIGINAL_INDEX):
        raise SystemExit(f"权威原始 index 快照不存在：{ORIGINAL_INDEX}")

    # 并发检测：**内容判据**（mtime 会把我自己 checkout 写的文件也算进去）
    cur_now = index_map()
    main_now = tree_map(new)
    recent = subprocess.run(
        ["find", WT, "-newermt", "-4 minutes", "-type", "f",
         "-not", "-path", f"{WT}/.git/*"],
        capture_output=True, text=True, timeout=300).stdout.strip()
    concurrent = []
    for line in (recent.splitlines() if recent else []):
        rel = os.path.relpath(line, WT)
        try:
            content = open(line, "rb").read()
        except OSError:
            continue
        bh = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        if cur_now.get(rel, ("", ""))[1] != bh and main_now.get(rel) != bh:
            concurrent.append(line)
    if concurrent:
        print("  !! 有手工编辑（内容既不等于 index 也不等于主线）：")
        print("\n".join(concurrent[:10]))
        raise SystemExit("停：需人工确认")
    print(f"  HEAD={head[:7]}  {MAIN}={main_head[:7]}  FF 可行  无并发编辑 ✅")

    orig = index_map(ORIGINAL_INDEX)
    base = tree_map(BASE)
    new_tree = tree_map(new)
    staged_del = {p for p in base if p not in orig}
    genuine = {p for p in orig if p not in base
               or orig[p][1] != base.get(p, orig[p][1])}
    changed = sorted(p for p in set(base) | set(new_tree)
                     if base.get(p) != new_tree.get(p))
    print("═══ 1. 分类（权威口径）═══")
    print(f"  主线变更路径 = {len(changed)}")
    print(f"  对方暂存删除意图 = {len(staged_del)} 条")
    print(f"  对方真实暂存 = {len(genuine)} 条")

    print("═══ 2. 构造新 index ═══")
    tmp_index = "/tmp/r7_new_index_auto.index"
    if os.path.exists(tmp_index):
        os.remove(tmp_index)
    git("read-tree", new, index=tmp_index)
    payload = [f"{orig[p][0]} {orig[p][1]}\t{p}" for p in sorted(genuine)]
    if payload:
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "GIT_INDEX_FILE": tmp_index})
        if proc.returncode != 0:
            raise SystemExit(f"update-index 失败: {proc.stderr}")
    to_rm = [p for p in sorted(staged_del) if p in new_tree]
    for p in to_rm:
        subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                       capture_output=True, timeout=300,
                       env={**os.environ, "GIT_INDEX_FILE": tmp_index})
    new_idx = index_map(tmp_index)
    print(f"  新 index = {len(new_idx)} 条")

    print("═══ 3. 自检 ═══")
    bad = [p for p in genuine if new_idx.get(p, ("", ""))[1] != orig[p][1]]
    missing = sorted(set(new_tree) - set(new_idx) - staged_del)
    staged_left = [p for p in staged_del if p in new_idx]
    print(f"  对方真实暂存被改动 = {len(bad)} {bad[:5]}")
    print(f"  主线成果非预期缺失 = {len(missing)} {missing[:5]}")
    print(f"  对方暂存删除意图仍在 index = {len(staged_left)} {staged_left[:5]}")
    if bad or missing or staged_left:
        raise SystemExit("自检失败，未落地")

    print("═══ 4. 落地 ═══")
    real_index = git("rev-parse", "--git-path", "index").stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, "/tmp/r7_shared_index_pre_advance_auto.bak")
    shutil.copy2(tmp_index, real_index)
    print("  index 已替换")

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
    fp_before = {}
    for p in sorted(theirs):
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp_before[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    sync_paths = [p for p in changed if p not in theirs]
    if sync_paths:
        with open("/tmp/r7_sync_paths_auto.txt", "w") as fh:
            fh.write("\n".join(sync_paths) + "\n")
        proc = subprocess.run(
            ["git", "-C", WT, "checkout", new,
             "--pathspec-from-file=/tmp/r7_sync_paths_auto.txt"],
            capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise SystemExit(f"工作区同步失败:\n{proc.stderr}")
        print(f"  工作区已同步 {len(sync_paths)} 个路径")
    git("update-ref", f"refs/heads/{MAIN}", new, old)
    print(f"  {MAIN}: {old} -> {new}")

    print("═══ 5. 落地后核验 ═══")
    cur2 = index_map()
    bad2 = [p for p in genuine if cur2.get(p, ("", ""))[1] != orig[p][1]]
    fp_after = {}
    for p, h in fp_before.items():
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp_after[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    diff_fp = [p for p in fp_before if fp_before[p] != fp_after.get(p)]
    missing2 = sorted(set(new_tree) - set(cur2) - staged_del)
    staged2 = [p for p in staged_del if p in cur2]
    print(f"  (a) 对方真实暂存 {len(genuine)} 条被改动 = {len(bad2)} {bad2[:5]}")
    print(f"  (b) 对方在改文件工作区内容变化 = {len(diff_fp)} {diff_fp[:5]}")
    print(f"  (c) 主线成果非预期缺失 = {len(missing2)} {missing2[:5]}")
    print(f"  (d) 对方暂存删除意图仍在 index = {len(staged2)}")
    print(f"  (e) 预期缺失（对方删除意图 ∩ 主线 tree）= {len(staged_del & set(new_tree))}")
    if bad2 or diff_fp or missing2 or staged2:
        raise SystemExit("落地后核验失败")
    print("  ✅ 零污染")

    print("═══ 6. 对方「下一次提交」三方合并预演 ═══")
    tree = git("write-tree").stdout.strip()
    commit = git("commit-tree", tree, "-p", new, "-m",
                 "R7 预演：对方下一次提交（不落库）").stdout.strip()
    mb = git("merge-base", new, commit).stdout.strip()
    mt = subprocess.run(["git", "-C", REPO, "merge-tree", "--write-tree",
                         "--messages", "--merge-base", mb, new, commit],
                        capture_output=True, text=True, timeout=900)
    print(f"  预演提交 = {commit[:7]}  三方合并退出码 = {mt.returncode}")
    if mt.returncode != 0:
        print(mt.stdout[:1200]); print(mt.stderr[:1200])
        raise SystemExit("预演有冲突")
    merged = mt.stdout.splitlines()[0].strip()
    names = {l.split("\t", 1)[1] for l in
             subprocess.run(["git", "-C", REPO, "ls-tree", "-r", merged],
                            capture_output=True, text=True).stdout.splitlines()}
    for k in ["h3studio/twopass_policy.py", "server.py", "web/index.html",
              "h3studio/video_models.py", "tests/test_r7_review_fixes.py"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成 ═══")
    print("（下一步建议跑 scripts/r7_restore_shared_missing_mainline.py —— "
          "把「index 有、工作区无」的主线成果检出，避免对方 commit -a 误删）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
