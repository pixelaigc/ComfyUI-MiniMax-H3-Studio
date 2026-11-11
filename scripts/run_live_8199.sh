#!/usr/bin/env bash
# 独立实跑实例（端口 8199）—— 不碰 8190 线上服务与 systemd unit。
#
# 【为什么要单独脚本】
#   * 线上 8190 由 systemd unit h3-studio 管着（WorkingDirectory=/root/projects/h3studio-live），
#     本任务明确要求「不要重启 8190 服务与 systemd unit」。
#   * 实跑实例必须用**本 worktree 的代码**（local/feature/enhance-system-echo）。
#   * 数据目录独立（data-live/），任务落库证据不污染线上 jobs.db。
#
# 沿用既有约定（scripts/run_live_8199.sh）：
#   H3STUDIO_PORT        —— 独立端口
#   H3STUDIO_DATA_DIR    —— 独立数据目录（jobs.db / outputs）
#   AGENT_SESSION_DB / AGENT_MEMORY_DB —— 独立 Agent 库
set -euo pipefail

ROOT=/root/projects/h3studio-echo-fix
PORT="${H3STUDIO_PORT:-8199}"
export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="$ROOT/data-live"
export AGENT_SESSION_DB="$ROOT/data-live/agent_sessions.db"
export AGENT_MEMORY_DB="$ROOT/data-live/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
