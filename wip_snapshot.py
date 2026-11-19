#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给他人脏 worktree 做「零影响」快照取证。

在合并/部署前后各跑一次，比对 index 与工作区内容的哈希是否逐字节一致。
只读，不改任何东西。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                          text=True).stdout


def snapshot(repo: str) -> dict:
    """取 index 状态 + 工作区已跟踪文件内容哈希（不读未跟踪大文件）。"""
    head = sh("git rev-parse HEAD", repo).strip()
    branch = sh("git branch --show-current", repo).strip()
    index_raw = sh("git ls-files -s", repo)
    index_lines = sorted(l for l in index_raw.splitlines() if l.strip())
    index_hash = hashlib.sha256(index_raw.encode()).hexdigest()

    # 工作区里「已跟踪文件」的实际内容哈希
    tracked = sh("git ls-files -z", repo).split("\0")
    tracked = [p for p in tracked if p]
    h = hashlib.sha256()
    per_file = {}
    for p in sorted(tracked):
        fp = Path(repo) / p
        try:
            b = fp.read_bytes()
        except Exception:
            b = b"<unreadable>"
        d = hashlib.sha256(b).hexdigest()
        per_file[p] = d
        h.update(p.encode())
        h.update(d.encode())
    worktree_hash = h.hexdigest()

    status = sorted(l for l in sh("git status --porcelain", repo).splitlines() if l.strip())
    return {
        "repo": repo,
        "head": head,
        "branch": branch,
        "index_entries": len(index_lines),
        "index_sha256": index_hash,
        "worktree_content_sha256": worktree_hash,
        "status_lines": len(status),
        "status_digest": hashlib.sha256("\n".join(status).encode()).hexdigest(),
        "per_file": per_file,
    }


def main():
    args = sys.argv[1:]
    tag = "now"
    if args and not args[0].startswith("/"):
        tag = args.pop(0)
    repos = args or [
        "/root/projects/h3studio-cont-seam-hires",
        "/root/projects/comfyui-minimax-h3-studio",
    ]
    out = {}
    for r in repos:
        out[r] = snapshot(r)
    dest = (Path(__file__).parent / f"wip_snapshot_{tag}.json")
    json.dump(out, open(dest, "w"), ensure_ascii=False, indent=1)
    for r, v in out.items():
        print(f"{r}")
        print(f"  branch={v['branch']} head={v['head'][:12]}")
        print(f"  index_entries={v['index_entries']} index_sha256={v['index_sha256'][:16]}")
        print(f"  worktree_content_sha256={v['worktree_content_sha256'][:16]}")
        print(f"  status_lines={v['status_lines']} status_digest={v['status_digest'][:16]}")
    print(f"\nwritten: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
