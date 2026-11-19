#!/usr/bin/env bash
# 基线 vs 候选分支的逐文件 pytest 对比（后台可断点重跑）。
#
# 为什么要**两个 worktree 各跑一遍**而不是只跑候选分支：
#   本仓库的测试集里有**已知的历史失败**（LoRA 上下文阻尼、moss 相关等）。
#   只跑候选分支无法区分「新引入的失败」与「本来就红的」。
#   逐文件对比能给出「新增失败集合为空」的硬证据。
#
# 用法: bash scripts/run_perfile_compare.sh <baseline_wt> <candidate_wt> <outdir>
set -uo pipefail

BASE_WT="$1"
CAND_WT="$2"
OUT="$3"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$OUT"
BASE_DIR="$OUT/base"
CAND_DIR="$OUT/cand"
rm -rf "$BASE_DIR" "$CAND_DIR"
mkdir -p "$BASE_DIR" "$CAND_DIR"

echo "[perfile] baseline = $BASE_WT"
bash "$SCRIPT_DIR/pytest_perfile.sh" "$BASE_WT" "$BASE_DIR" 4 > "$OUT/base.log" 2>&1
echo "[perfile] baseline done"

echo "[perfile] candidate = $CAND_WT"
bash "$SCRIPT_DIR/pytest_perfile.sh" "$CAND_WT" "$CAND_DIR" 4 > "$OUT/cand.log" 2>&1
echo "[perfile] candidate done"

# 汇总两份 _summary.txt
python3 - "$OUT" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

out = Path(sys.argv[1])


def parse(path: Path):
    data = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^FILE (\S+) rc=(\d+)$", line)
        if m:
            data[m.group(1)] = {"rc": int(m.group(2)), "failed": []}
            continue
        m = re.match(r"^FAILED (\S+)", line)
        if m and data:
            last = list(data)[-1]
            data[last]["failed"].append(m.group(1))
    return data


base = parse(out / "base" / "_summary.txt")
cand = parse(out / "cand" / "_summary.txt")

base_fail = {k: set(v["failed"]) for k, v in base.items() if v["failed"]}
cand_fail = {k: set(v["failed"]) for k, v in cand.items() if v["failed"]}

all_files = sorted(set(base) | set(cand))
new_fail = {}
fixed = {}
for f in all_files:
    b = base_fail.get(f, set())
    c = cand_fail.get(f, set())
    if c - b:
        new_fail[f] = sorted(c - b)
    if b - c:
        fixed[f] = sorted(b - c)

summary = {
    "baseline_files": len(base),
    "candidate_files": len(cand),
    "baseline_failing_files": len(base_fail),
    "candidate_failing_files": len(cand_fail),
    "baseline_failed_cases": sum(len(v) for v in base_fail.values()),
    "candidate_failed_cases": sum(len(v) for v in cand_fail.values()),
    "new_failures": new_fail,
    "new_failure_count": sum(len(v) for v in new_fail.values()),
    "fixed": fixed,
    "base": {k: sorted(v["failed"]) for k, v in base.items() if v["failed"]},
    "cand": {k: sorted(v["failed"]) for k, v in cand.items() if v["failed"]},
}
(out / "perfile-compare.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"[perfile] baseline: {summary['baseline_failing_files']} files / "
      f"{summary['baseline_failed_cases']} cases failed")
print(f"[perfile] candidate: {summary['candidate_failing_files']} files / "
      f"{summary['candidate_failed_cases']} cases failed")
print(f"[perfile] NEW failures (candidate-only): {summary['new_failure_count']}")
for f, cases in new_fail.items():
    print(f"   !! {f}: {cases}")
print(f"[perfile] fixed: {sum(len(v) for v in fixed.values())}")
print(f"[perfile] report: {out/'perfile-compare.json'}")
PYEOF

echo "[perfile] ALL DONE"
touch "$OUT/DONE"
