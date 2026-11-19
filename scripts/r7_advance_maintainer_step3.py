#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R7 第三步推进：把 `local/maintainer` 从 `76d61f8` 推到 `207e9fa`。

【背景】
本轮并行任务在我部署后又在线上推了 `4392cce`（失败卡片不再显示成全黑图）。
我已把 `4392cce` 合入主线得到 `207e9fa`（merge commit，无冲突）。
现在需要把共享 worktree 的 `local/maintainer` 从 `76d61f8` 推到 `207e9fa`。

【本脚本的关键修正（第二轮踩过的坑）】
第二轮我用「上一轮推进前的 index」当参照来复原对方的暂存删除意图 ——
那个 index 是**我自己构造的**，不是对方的原始 index ⇒ 参照集被污染
⇒ 误把 126 条主线新增文件从 index 里删掉（对方下一次 commit -a 会删掉本轮成果）。

**正确口径（权威）**：
  * 参照 = `/tmp/r7_shared_index_before.bak`（1610 条，本轮一切操作前的原始快照）
  * 对方的暂存删除意图 = fcb6057 tree 有、但**原始 index** 里没有的路径（31 条）
  * 主线成果 = 207e9fa tree 里的路径，**必须**在 index 里
  * 交叉冲突（既在主线 tree 又在对方删除清单）→ **对方意图优先**（对方主动清理的
    生成物，主线里也留着，属对方清理范围）

【核验清单】
  1. 对方真实暂存 34 条 blob 逐字节不变
  2. 对方在改文件的工作区内容一个字节不动
  3. 对方的 31 条暂存删除意图原样保留（不在 index 里）
  4. 主线成果在 index 里全部就位（除被对方删除的 31 条）
  5. 对方「下一次提交」三方合并预演零冲突
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

WT = "/root/projects/h3studio-cont-seam-hires"
REPO = "/root/projects/h3studio-review-r7"
OLD = "76d61f8"
NEW = "207e9fa"
BASE = "fcb6057"
MAIN = "local/maintainer"
ORIGINAL_INDEX = "/tmp/r7_shared_index_before.bak"
TMP_INDEX = "/tmp/r7_new_index3"


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


def index_map(index: str | None = None, wt: str | None = None) -> dict[str, tuple[str, str]]:
    raw = subprocess.run(["git", "-C", wt or WT, "-c", "core.quotePath=false",
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
    """tree 路径必须用 -z + core.quotePath=false 取，否则中文路径会被 git 转义
    成 \\347\\253... 形式，与 index_map 的真实路径对不上（会造出上百条假匹配）。"""
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
    print("═══ 0. 前置核验 ═══")
    head = git("rev-parse", "HEAD", wt=WT).stdout.strip()
    main_head = git("rev-parse", MAIN).stdout.strip()
    if not head.startswith(OLD):
        raise SystemExit(f"共享 worktree HEAD 不是 {OLD}（{head}），停")
    if not main_head.startswith(OLD):
        raise SystemExit(f"{MAIN} 不是 {OLD}（{main_head}），停")
    if git("merge-base", "--is-ancestor", OLD, NEW).returncode != 0:
        raise SystemExit(f"{NEW} 不是 {OLD} 的后代，停")
    # 并发保护：共享 worktree 近 4 分钟不得有文件写入
    # 【判据】不能只看 mtime —— 上一轮**我自己的** checkout 同步写过的路径
    # 也会落在时间窗里，会把第二次推进永远挡住。
    # 正确判据 = 内容判据：
    #   文件内容若等于「当前 index 里的 blob」或「主线 tree 里的 blob」
    #   ⇒ 是 git checkout（我或任何人跑的标准操作）写的，不是并发编辑
    #   内容既不等于 index 也不等于主线 ⇒ **有人在手改**，必须停下
    recent_all = subprocess.run(
        ["find", WT, "-newermt", "-4 minutes", "-type", "f",
         "-not", "-path", f"{WT}/.git/*"],
        capture_output=True, text=True, timeout=300).stdout.strip()
    cur_now = index_map()
    main_now = tree_map(NEW)
    concurrent = []
    for line in recent_all.splitlines():
        rel = os.path.relpath(line, WT)
        full = os.path.join(WT, rel)
        try:
            content = open(full, "rb").read()
        except OSError:
            continue
        bh = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
        in_index = cur_now.get(rel, ("", ""))[1] == bh
        in_main = main_now.get(rel) == bh
        if not (in_index or in_main):
            concurrent.append(line)
    if concurrent:
        print("  !! 共享 worktree 近 4 分钟有**手工编辑**（内容既不等于 index 也不等于主线）：")
        print("\n".join(concurrent[:10]))
        raise SystemExit("停：需人工确认")
    print(f"  HEAD={head[:7]}  {MAIN}={main_head[:7]}  FF 可行  无并发编辑 ✅"
          f"（近 4 分钟写入 {len(recent_all.splitlines())} 条，内容全部与 index/主线一致"
          f" ⇒ 是本会话自己 checkout 写的）")

    orig = index_map(ORIGINAL_INDEX)
    cur = index_map()
    base = tree_map(BASE)
    new = tree_map(NEW)

    # 权威分类
    staged_del = {p for p in base if p not in orig}       # 对方的暂存删除意图
    genuine = {p for p in orig if p not in base
               or orig[p][1] != base.get(p, orig[p][1])}   # 对方真实暂存
    changed = sorted(p for p in set(base) | set(new)
                     if base.get(p) != new.get(p))
    overlap = [p for p in changed if p in staged_del]
    print("═══ 1. 分类（权威口径）═══")
    print(f"  主线变更路径 = {len(changed)}")
    print(f"  对方暂存删除意图 = {len(staged_del)} 条（原始 index 参照）")
    print(f"  对方真实暂存 = {len(genuine)} 条")
    print(f"  与对方删除意图冲突的路径 = {len(overlap)}（对方意图优先）")

    # 2. 构造新 index：基底 = 新主线 tree，叠回对方真实暂存
    print("═══ 2. 构造新 index ═══")
    if os.path.exists(TMP_INDEX):
        os.remove(TMP_INDEX)
    git("read-tree", NEW, index=TMP_INDEX, check=True)
    payload = [f"{orig[p][0]} {orig[p][1]}\t{p}" for p in sorted(genuine)]
    if payload:
        proc = subprocess.run(["git", "-C", WT, "update-index", "--index-info"],
                              input="\n".join(payload) + "\n",
                              capture_output=True, text=True, timeout=300,
                              env={**os.environ, "GIT_INDEX_FILE": TMP_INDEX})
        if proc.returncode != 0:
            raise SystemExit(f"update-index 失败: {proc.stderr}")
    new_idx = index_map(TMP_INDEX)
    print(f"  新 index = {len(new_idx)} 条（原 {len(cur)} 条）")

    print("═══ 3. 自检 ═══")
    bad = [p for p in genuine if new_idx.get(p, ("", ""))[1] != orig[p][1]]
    mainline_missing = sorted(set(new) - set(new_idx) - staged_del)
    staged_still = [p for p in staged_del if p in new_idx]
    print(f"  对方真实暂存被改动 = {len(bad)} {bad[:5]}")
    print(f"  主线成果缺失（非预期）= {len(mainline_missing)} {mainline_missing[:5]}")
    print(f"  对方暂存删除意图仍在 index = {len(staged_still)} {staged_still[:5]}")
    if bad or mainline_missing:
        raise SystemExit("自检失败，未落地")

    print("═══ 4. 落地 ═══")
    real_index = git("rev-parse", "--git-path", "index", wt=WT).stdout.strip()
    if not os.path.isabs(real_index):
        real_index = os.path.join(WT, real_index)
    shutil.copy2(real_index, "/tmp/r7_shared_index_pre_advance3.bak")
    shutil.copy2(TMP_INDEX, real_index)
    print("  index 已替换（备份 /tmp/r7_shared_index_pre_advance3.bak）")

    # 工作区：只同步「对方没在改」的路径
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
        with open("/tmp/r7_sync_paths3.txt", "w") as fh:
            fh.write("\n".join(sync_paths) + "\n")
        proc = subprocess.run(
            ["git", "-C", WT, "checkout", NEW,
             "--pathspec-from-file=/tmp/r7_sync_paths3.txt"],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise SystemExit(f"工作区同步失败:\n{proc.stderr}")
        print(f"  工作区已同步 {len(sync_paths)} 个路径")
    git("update-ref", f"refs/heads/{MAIN}", NEW, OLD, check=True)
    print(f"  {MAIN}: {OLD} -> {NEW}")

    print("═══ 5. 落地后核验 ═══")
    # 对方的暂存删除意图：index 基底是新主线 tree，这 31 条在主线里也存在
    # ⇒ 必须在 index 里移除，否则对方 worktree 里它们会变成「未暂存删除」，
    #    对方下一次 commit 会把主线里的这些路径也删掉。
    # 【关键】用**权威口径**的 staged_del（原始 index 参照），不要用上一轮
    # 推进前的 index —— 那个是我自己构造的，参照集被污染过（第二轮踩过）。
    cur2 = index_map()
    to_remove = [p for p in sorted(staged_del) if p in cur2]
    if to_remove:
        for p in to_remove:
            subprocess.run(["git", "-C", WT, "update-index", "--force-remove", "--", p],
                           capture_output=True, timeout=300)
        print(f"  按对方意图移除 index 条目 {len(to_remove)} 条"
              f"（对方清理的生成物，主线里也留着 —— 对方意图优先）")
        cur2 = index_map()

    bad2 = [p for p in genuine if cur2.get(p, ("", ""))[1] != orig[p][1]]
    fp_after = {}
    for p, h in fp_before.items():
        full = os.path.join(WT, p)
        if os.path.isfile(full):
            fp_after[p] = hashlib.sha256(open(full, "rb").read()).hexdigest()
    diff_fp = [p for p in fp_before if fp_before[p] != fp_after.get(p)]
    missing2 = sorted(set(new) - set(cur2) - staged_del)
    staged2 = [p for p in staged_del if p in cur2]
    print(f"  (a) 对方真实暂存 {len(genuine)} 条被改动 = {len(bad2)} {bad2[:5]}")
    print(f"  (b) 对方在改文件工作区内容变化 = {len(diff_fp)} {diff_fp[:5]}")
    print(f"  (c) 主线成果非预期缺失 = {len(missing2)} {missing2[:5]}")
    print(f"  (d) 对方暂存删除意图仍在 index = {len(staged2)}")
    print(f"  (e) 预期缺失（对方删除意图 ∩ 主线 tree）= {len(staged_del & set(new))}")
    if bad2 or diff_fp or missing2 or staged2:
        raise SystemExit("落地后核验失败")
    print("  ✅ 零污染")

    print("═══ 6. 对方「下一次提交」三方合并预演 ═══")
    tree = git("write-tree", wt=WT).stdout.strip()
    commit = git("commit-tree", tree, "-p", NEW, "-m",
                 "R7 预演：对方下一次提交（不落库）", wt=WT).stdout.strip()
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
              "tests/test_r7_review_fixes.py", "web/style.css"]:
        print(f"  预演结果树含 {k}: {'✅' if k in names else '❌'}")
    print("═══ 完成 ═══")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
