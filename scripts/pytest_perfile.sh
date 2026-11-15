#!/usr/bin/env bash
# 逐文件跑 pytest，记录每个文件的失败用例名 —— 用于「基线 vs 候选」精确对比。
#
# 为什么不用整目录一次跑：整目录跑会被浏览器用例阶段拖到超时，
# 拿不到 -rf 汇总行（失败名单）。逐文件跑能拿到每��文件的完整名单，
# 且能并行推进。
#
# 用法: bash scripts/pytest_perfile.sh <worktree> <outdir> [并行度]
set -uo pipefail

WT="$1"
OUT="$2"
JOBS="${3:-6}"

mkdir -p "$OUT"
cd "$WT"

# 只收集纯 pytest 用例文件（排除 verify_*/probe_* 这类脚本）
mapfile -t FILES < <(ls tests/test_*.py 2>/dev/null | sort)
echo "worktree=$WT  files=${#FILES[@]}  out=$OUT"

run_one() {
  local f="$1"
  local tag
  tag=$(echo "$f" | tr '/' '_')
  # 每个文件单独跑，120s 上限（浏览器类用例可能慢）
  timeout 150 python3 -m pytest "$f" -q --tb=no -rf > "$OUT/$tag.txt" 2>&1
  local rc=$?
  {
    echo "FILE $f rc=$rc"
    grep "^FAILED" "$OUT/$tag.txt" | sed 's/^FAILED /FAILED /' || true
    grep -E "^[0-9]+ (passed|failed)" "$OUT/$tag.txt" | tail -1 || true
  } >> "$OUT/_summary.txt"
}
export -f run_one
export OUT

printf '%s\n' "${FILES[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$@"' _ {}

echo "=== DONE $WT ==="
