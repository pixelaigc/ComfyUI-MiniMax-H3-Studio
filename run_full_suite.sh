#!/bin/bash
# 串行全量 pytest 回归跑批器（基线 / 候选各一棵树，串行，不并行）
# 用法: run_full_suite.sh <worktree> <tag>
set -u
WT="$1"
TAG="$2"
LOG="/root/projects/h3s-resume-cand/full_${TAG}.txt"
cd "$WT" || exit 9
{
  echo "### WORKTREE=$WT"
  echo "### HEAD=$(git rev-parse HEAD)"
  echo "### CMD=python3 -m pytest tests/ -q -p no:randomly"
  echo "### START=$(date -Is)"
} > "$LOG"
python3 -m pytest tests/ -q -p no:randomly >> "$LOG" 2>&1
rc=$?
echo "EXIT=$rc" >> "$LOG"
echo "### END=$(date -Is)" >> "$LOG"
exit $rc
