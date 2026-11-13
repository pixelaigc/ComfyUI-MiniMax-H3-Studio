#!/usr/bin/env bash
# D1 验收用独立实跑实例（端口 8199）—— 不碰 8190 线上服务与 systemd unit。
#
# 【为什么不复用 scripts/run_live_8199_t2v.sh】那个脚本指向
# /root/projects/h3studio-t2v-sampler（另一个 worktree / 另一个分支）。
# 本轮的验收证据必须跑在**本 worktree 本分支**的代码上，
# 否则「证据对应的代码」与「分支 HEAD」对不上，证据不可采信。
#
# 【数据目录】独立（/tmp/h3t2v-d1-data），不污染线上 jobs.db。
set -euo pipefail

ROOT=/root/projects/h3studio-t2v-continue
PORT=8199
export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="/tmp/h3t2v-d1-data"
export AGENT_SESSION_DB="/tmp/h3t2v-d1-data/agent_sessions.db"
export AGENT_MEMORY_DB="/tmp/h3t2v-d1-data/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
