#!/usr/bin/env bash
# 起一个**隔离的** staging 实例（独立端口 + 独立 data_dir），
# 用来在不动线上 8190 的前提下验证修复效果。
#
# 用法: run_staging_8291.sh <worktree> [port]
set -uo pipefail

WT="${1:?用法: run_staging_8291.sh <worktree> [port]}"
PORT="${2:-8291}"
DATA="/tmp/h3stage-data-${PORT}"
LOG="/tmp/h3studio-staging-${PORT}.log"
PIDFILE="/tmp/h3studio-staging-${PORT}.pid"

mkdir -p "$DATA"
cd "$WT" || exit 9

# 独立 data_dir —— 否则 staging 会写进线上共用的 jobs.db
export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="$DATA"
export PYTHONPATH="$WT"

setsid nohup /home/ubuntu/comfyui/venv/bin/python server.py > "$LOG" 2>&1 &
echo $! > "$PIDFILE"
echo "staging pid=$(cat "$PIDFILE") port=$PORT data=$DATA log=$LOG"
