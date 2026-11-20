# -*- coding: utf-8 -*-
"""
生成「全量 pytest 与未改动基线对跑」的 FAILED/ERROR 名单 diff。

用法：
    python3 make_regression_diff.py <baseline_json> <changed_json> <out_md>

输入是两个 ``scripts/run_pytest_per_file.py`` 的输出 JSON：
    {"total_files":N, "failed_files":[...], "results":{file:{"rc":..,"summary":".."}}}

【怎么读这个 diff】
  * ``baseline_only``  —— 基线失败、本轮通过（**修好了**）
  * ``changed_only``   —— 基线通过、本轮失败（**新增回归**）—— 这一项必须是 0
  * ``both_fail``      —— 两边都失败（**既有失败**，不是本轮引入）
    对 ``both_fail`` 的每一项，都要**单独复跑**确认不是并发/共享素材库
    干扰造成的假失败（本仓库的共享素材库会让并发跑的全量互相踩）。
"""
import json
import sys
from pathlib import Path

BASE = Path(sys.argv[1])
CHANGED = Path(sys.argv[2])
OUT = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("FAILED_DIFF.md")


def load(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    results = data.get("results") or {}
    failed = set(data.get("failed_files") or [])
    # 以 rc != 0 为准（failed_files 由脚本生成，两边口径一致）
    for name, info in results.items():
        if int(info.get("rc", 0)) != 0:
            failed.add(name)
    return data, results, failed


def main():
    bdata, bres, bfail = load(BASE)
    cdata, cres, cfail = load(CHANGED)

    base_only = sorted(bfail - cfail)
    changed_only = sorted(cfail - bfail)
    both_fail = sorted(bfail & cfail)
    both_pass = sorted(set(bres) & set(cres) - bfail - cfail)

    lines = []
    W = lines.append
    W("# 全量 pytest 回归 diff · 基线 vs 本轮\n")
    W("| 项 | 值 |")
    W("|---|---|")
    W(f"| 基线代码 | `{bdata.get('base_commit', '（见下）')}` |")
    W(f"| 基线工作树 | `{BASE.parent}` 之外的 `h3s-base-truth`（detached @ 7436883） |")
    W(f"| 本轮工作树 | `h3s-deftruth`（分支 `local/feature/agent-defaults-truth`） |")
    W(f"| 基线文件数 | {bdata.get('total_files')} |")
    W(f"| 本轮文件数 | {cdata.get('total_files')} |")
    W(f"| **新增回归（基线过 → 本轮挂）** | **{len(changed_only)}** |")
    W(f"| 修好（基线挂 → 本轮过） | {len(base_only)} |")
    W(f"| 既有失败（两边都挂） | {len(both_fail)} |")
    W(f"| 两边都过 | {len(both_pass)} |")
    W("")

    W("## 新增回归名单（必须为空）\n")
    if changed_only:
        W("| 文件 | 本轮结果 |")
        W("|---|---|")
        for name in changed_only:
            W(f"| `{name}` | {cres.get(name, {}).get('summary', '')} |")
    else:
        W("**（空）—— 本轮未引入任何新增回归。**")
    W("")

    W("## 修好的（基线失败 → 本轮通过）\n")
    if base_only:
        W("| 文件 | 基线结果 | 本轮结果 |")
        W("|---|---|---|")
        for name in base_only:
            W(f"| `{name}` | {bres.get(name, {}).get('summary', '')} "
              f"| {cres.get(name, {}).get('summary', '')} |")
    else:
        W("（无）")
    W("")

    W("## 既有失败（两边都挂 —— 不是本轮引入）\n")
    if both_fail:
        W("| 文件 | 基线 | 本轮 |")
        W("|---|---|---|")
        for name in both_fail:
            W(f"| `{name}` | {bres.get(name, {}).get('summary', '')} "
              f"| {cres.get(name, {}).get('summary', '')} |")
    else:
        W("（无）")
    W("")

    W("## 逐文件结果（仅列出有差异的）\n")
    W("| 文件 | 基线 | 本轮 | 判定 |")
    W("|---|---|---|---|")
    for name in sorted(set(bres) | set(cres)):
        b = bres.get(name, {}).get("summary", "（未跑）")
        c = cres.get(name, {}).get("summary", "（未跑）")
        if b == c:
            continue
        if name in changed_only:
            verdict = "❌ **新增回归**"
        elif name in base_only:
            verdict = "✅ 修好"
        else:
            verdict = "⚠️ 结果差异（非通过/失败方向）"
        W(f"| `{name}` | {b} | {c} | {verdict} |")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"written: {OUT}")
    print(f"新增回归={len(changed_only)} 修好={len(base_only)} "
          f"既有失败={len(both_fail)} 两边都过={len(both_pass)}")
    if changed_only:
        print("!! 新增回归:", changed_only)
        sys.exit(1)


if __name__ == "__main__":
    main()
