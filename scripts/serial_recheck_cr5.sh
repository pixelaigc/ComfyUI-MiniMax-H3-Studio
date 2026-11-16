#!/usr/bin/env bash
# 第5轮 —— **串行**复验：只跑「并发对拍报出的差异文件」。
#
# 【为什么需要串行复验】
# 并发对拍（compare_suite_cr5.sh）里，不同分片同时跑 pytest，
# 而浏览器测试会抢同一个 Chrome / 端口 -> 一侧失败、另一侧**根本没跑到**
# 该文件。于是 comm 报出「新增失败」，其实是「一边跑了挂、一边没跑」。
# 这类假差异必须用**串行**（同一时刻只跑一个 pytest）复跑才能定性。
#
# 用法：
#   bash scripts/serial_recheck_cr5.sh <branch_worktree> <base_worktree> <outdir> <file1> [file2 ...]
set -uo pipefail

BRANCH_DIR="${1:?用法: serial_recheck_cr5.sh <branch> <base> <outdir> <file...>}"
BASE_DIR="${2:?}"
OUT="${3:?}"
shift 3
FILES=("$@")

PY=/home/ubuntu/comfyui/venv/bin/python
mkdir -p "$OUT"

run_side() {
  local tree="$1" tag="$2"
  : > "$OUT/$tag.txt"
  cd "$tree" || return 1
  local f
  for f in "${FILES[@]}"; do
    echo "### $f" >> "$OUT/$tag.txt"
    AGENT_SESSION_DB="$tree/data-live/agent_sessions.db" \
    AGENT_MEMORY_DB="$tree/data-live/agent_memory.db" \
    PYTHONPATH="$tree" \
    timeout 900 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no \
      >> "$OUT/$tag.txt" 2>&1 || true
  done
}

echo "=== 串行跑 baseline 侧 ===" >&2
run_side "$BASE_DIR" baseline
echo "=== 串行跑 branch 侧 ===" >&2
run_side "$BRANCH_DIR" branch

for tag in baseline branch; do
  grep -E "^(FAILED|ERROR) " "$OUT/$tag.txt" 2>/dev/null \
    | while IFS= read -r line; do
        rest="${line#* }"
        name="${rest%% *}"
        printf '%s\n' "$name"
      done | sort -u > "$OUT/$tag.failures"
  echo "$tag failures: $(wc -l < "$OUT/$tag.failures")" >&2
done

echo "=== NEW failures (branch - baseline) ===" >&2
comm -13 "$OUT/baseline.failures" "$OUT/branch.failures" | tee "$OUT/new_failures.txt"
echo "=== count NEW: $(wc -l < "$OUT/new_failures.txt") ===" >&2
echo "=== FIXED failures (baseline - branch) ===" >&2
comm -23 "$OUT/baseline.failures" "$OUT/branch.failures" | tee "$OUT/fixed_failures.txt"
echo "=== count FIXED: $(wc -l < "$OUT/fixed_failures.txt") ===" >&2
echo "=== done ===" >&2
