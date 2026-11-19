#!/usr/bin/env bash
# R7：全量 pytest 对拍（逐文件、限时、可中断、可续跑）。
#
# 【为什么要逐文件而不是一次性 pytest tests/】
# 本项目有 79 个测试文件会起 playwright 浏览器；一次性跑会卡在某个
# browser 用例上（实测两次全量跑都停在 playwright sync_api 的 select()，
# py-spy 定位），拿不到最终统计。逐文件 + 单文件超时 ⇒
# ① 每个文件都有明确结论（pass/fail/timeout），
# ② timeout 的文件可单独复查，不会让整轮对拍作废。
#
# 用法：bash scripts/r7_suite_compare.sh <worktree> <out_file> [exclude_dir]
set -uo pipefail

WT="${1:?worktree}"
OUT="${2:?out_file}"
EXCLUDE="${3:-}"

PY=/home/ubuntu/comfyui/venv/bin/python
PER_FILE_TIMEOUT="${PER_FILE_TIMEOUT:-180}"

cd "$WT" || exit 1
: > "$OUT"

files=$(ls tests/*.py 2>/dev/null | sort)
[ -n "$EXCLUDE" ] && files=$(echo "$files" | grep -v "$EXCLUDE")

total=0; pass=0; fail=0; timeout=0; skip=0
: > "${OUT}.failed"
: > "${OUT}.timeout"
: > "${OUT}.passed"

for f in $files; do
  total=$((total+1))
  log=$(mktemp)
  timeout "$PER_FILE_TIMEOUT" "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=line > "$log" 2>&1
  rc=$?
  summary=$(grep -E "^[0-9]+ (passed|failed)|passed|failed|error" "$log" | tail -1)
  if [ $rc -eq 0 ]; then
    pass=$((pass+1)); echo "$f" >> "${OUT}.passed"
    printf "  PASS  %-58s %s\n" "$f" "${summary:0:60}"
  elif [ $rc -eq 124 ]; then
    timeout=$((timeout+1)); echo "$f" >> "${OUT}.timeout"
    printf "  TIMEOUT(%ss) %-48s\n" "$PER_FILE_TIMEOUT" "$f"
  elif grep -qE "no tests ran|collected 0 items" "$log"; then
    skip=$((skip+1))
    printf "  SKIP(0 test) %-48s\n" "$f"
  else
    fail=$((fail+1)); echo "$f" >> "${OUT}.failed"
    printf "  FAIL  %-58s %s\n" "$f" "${summary:0:60}"
    grep -E "^FAILED|^ERROR" "$log" | head -6 >> "${OUT}.failed"
  fi
  rm -f "$log"
done

{
  echo "worktree=$WT"
  echo "total_files=$total pass=$pass fail=$fail timeout=$timeout skip=$skip"
} | tee -a "$OUT"
