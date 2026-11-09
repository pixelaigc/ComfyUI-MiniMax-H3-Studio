#!/usr/bin/env bash
# 独立实跑实例（端口 8199）—— 不碰 8190 线上服务与 systemd unit。
#
# 【为什么要单独脚本】
#   * 线上 8190 由 systemd unit h3-studio 管着（WorkingDirectory=/root/projects/h3studio-live），
#     绝不能在这里 restart/stop/改配置。
#   * 实跑实例必须用**本 worktree 的代码**（local/feature/agent-enh-backend），
#     所以不能复用线上那个进程。
#   * 数据目录独立（data-live/），任务落库证据不污染线上 jobs.db。
set -euo pipefail

ROOT=/root/projects/h3studio-agent-enh-be
PORT=8199
export H3STUDIO_DATA_DIR="$ROOT/data-live"
export AGENT_SESSION_DB="$ROOT/data-live/agent_sessions.db"
export AGENT_MEMORY_DB="$ROOT/data-live/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
