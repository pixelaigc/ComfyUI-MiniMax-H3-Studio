#!/usr/bin/env bash
# 第 3 项：r2v 接续链扩样本（串行，≥5 条链）
#
# 【为什么串行】
# ComfyUI 一次只跑一个采样；并行提交只会把队列堆起来，
# 让 comfyui-watchdog 的 15s 探活超时概率上升（上轮 20:53 就打断过 3 条链）。
# 串行跑，每条链独立 seed，素材完全受控（同一参考图 + 同一参考视频）。
#
# 用法：bash tools/run_r2v_expand.sh <起始seed> <条数> [outfile]
set -u

SEED0="${1:-20261201}"
N="${2:-5}"
OUT="${3:-/tmp/r2v_expand.json}"
PY=/home/ubuntu/comfyui/venv/bin/python
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cd "$ROOT"
for i in $(seq 1 "$N"); do
  SEED=$((SEED0 + i - 1))
  echo "════════ 链 $i/$N  seed=$SEED  $(date '+%F %T') ════════"
  "$PY" tools/r2v_chain_runner.py --seed "$SEED" --tag "expand$((SEED0 + i - 1))" \
        --json-out "$OUT" --control 2>&1 | grep -v "FutureWarning\|import pynvml\|nvm\|openclaw\|任务进程组\|job control\|npmrc\|globalconfig" | tail -20
  echo "── 链 $i 结束 $(date '+%F %T') ──"
done
echo "全部完成，结果在 $OUT"
