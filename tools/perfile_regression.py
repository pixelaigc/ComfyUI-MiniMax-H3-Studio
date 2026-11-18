#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""逐文件对拍：基线 worktree vs 分支，同一测试文件串行跑，对比失败集合。

【为什么逐文件而不是一次性 pytest tests/】
一次性跑 159 个测试文件在本机**跑不完**（实测 50 分钟被 timeout 杀掉，
只跑到第 24 个文件，其中已含浏览器 E2E 与 live_* 真机测试 —— 它们要起
Playwright / 连真实服务）。而且中途被 kill 时 `tail -40` 缓冲把进度全丢了，
拿不到失败清单。
⇒ 逐文件跑：每个文件单独一个进程、单独记录结果，**任何时刻中断都已
   有可比较的清单**；且能精确对比「基线失败的集合」与「分支失败的集合」。

【判据】
  新增失败 = (分支失败的测试) − (基线失败的测试)  —— 必须为空
  新增 ERROR 同理。

用法：
  python3 tools/compare_regression.py --base /tmp/wsbase --branch /root/projects/h3studio-live
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

PY = "/home/ubuntu/comfyui/venv/bin/python"

#: 只跑**不依赖真实服务 / 网络 / GPU** 的测试文件。
#: live_* 前缀的测试要连 8190 与 ComfyUI，基线跑的时候线上是**分支的代码**
#: （不是基线的），结果不可比 —— 一律排除，并在报告里说明。
SKIP_PREFIX = ("live_",)
SKIP_NAMES = {
    # 要起浏览器 + 真机服务的（耗时且环境敏感）
    "test_cont_duration_ui_browser.py",
    "test_cont_prompt_history_browser.py",
    "test_continuation_frontend.py",
    "test_h3_video_modes_ui.py",
    "test_frontend_assets_hygiene.py",
}


def _files(root: str) -> list[str]:
    d = os.path.join(root, "tests")
    out = []
    for name in sorted(os.listdir(d)):
        if not name.startswith("test_") or not name.endswith(".py"):
            continue
        if name in SKIP_NAMES or name.startswith(SKIP_PREFIX):
            continue
        out.append(name)
    return out


def run_one(root: str, name: str, timeout: int = 300) -> dict:
    """跑一个测试文件，返回 {failed: [...], error: [...], rc: int}。"""
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", f"tests/{name}", "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=root, capture_output=True, text=True, timeout=timeout)
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return {"failed": [], "error": [f"__TIMEOUT__ {name}"], "rc": -9}
    failed = re.findall(r"^FAILED\s+(\S+)", out, flags=re.M)
    error = re.findall(r"^ERROR\s+(\S+)", out, flags=re.M)
    return {"failed": sorted(failed), "error": sorted(error),
            "rc": proc.returncode}


def collect(root: str, names: list[str], label: str) -> dict:
    res = {}
    t0 = time.time()
    for i, name in enumerate(names, 1):
        r = run_one(root, name)
        res[name] = r
        flag = "OK " if r["rc"] == 0 else "FAIL"
        print(f"  [{flag}] {label} {i}/{len(names)} {name} "
              f"(failed={len(r['failed'])} error={len(r['error'])})",
              flush=True)
    print(f"  {label} 用时 {time.time() - t0:.0f}s", flush=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--only", default="", help="逗号分隔的文件名子集")
    args = ap.parse_args()

    names = _files(args.base)
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        names = [n for n in names if n in want]
    print(f"逐文件对拍：{len(names)} 个测试文件")
    print(f"  基线 = {args.base}")
    print(f"  分支 = {args.branch}")
    print()

    print("── 基线 ──")
    base = collect(args.base, names, "base")
    print("── 分支 ──")
    branch = collect(args.branch, names, "brn ")

    base_failed = {f for r in base.values() for f in r["failed"]}
    base_error = {f for r in base.values() for f in r["error"]}
    brn_failed = {f for r in branch.values() for f in r["failed"]}
    brn_error = {f for r in branch.values() for f in r["error"]}

    new_failed = sorted(brn_failed - base_failed)
    new_error = sorted(brn_error - base_error)
    fixed = sorted(base_failed - brn_failed)

    print()
    print("═══ 对拍结果 ═══")
    print(f"  基线失败 {len(base_failed)} 条 / 错误 {len(base_error)} 条")
    print(f"  分支失败 {len(brn_failed)} 条 / 错误 {len(brn_error)} 条")
    print(f"  **新增失败 {len(new_failed)} 条**")
    for f in new_failed:
        print(f"     {f}")
    print(f"  **新增 ERROR {len(new_error)} 条**")
    for f in new_error:
        print(f"     {f}")
    if fixed:
        print(f"  分支修好的 {len(fixed)} 条：")
        for f in fixed[:20]:
            print(f"     {f}")
    ok = not new_failed and not new_error
    print(f"\n结论：{'新增失败 0 ✅' if ok else '有新增失败 ❌'}")

    if args.json_out:
        out = {
            "files": len(names),
            "base_failed": sorted(base_failed),
            "base_error": sorted(base_error),
            "branch_failed": sorted(brn_failed),
            "branch_error": sorted(brn_error),
            "new_failed": new_failed,
            "new_error": new_error,
            "verdict_pass": ok,
            "skipped": sorted(SKIP_NAMES | {n for n in SKIP_PREFIX}),
            "per_file": {"base": base, "branch": branch},
        }
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print(f"报告：{args.json_out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
