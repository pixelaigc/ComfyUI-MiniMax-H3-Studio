#!/usr/bin/env bash
# 独立验收实例（端口 8199）—— 用于「媒体库卡片详情」这一轮的真实浏览器取证。
#
# 【为什么不碰 8190】线上 8190 由 systemd unit h3-studio 管着
# （WorkingDirectory=/root/projects/h3studio-live），本任务明确要求
# 「不部署 8190、不重启服务」。验收必须跑**本 worktree 的代码**，
# 所以另起一个独立实例。
#
# 【数据目录独立】H3STUDIO_DATA_DIR 指向 /tmp/h3media-verify/data，
# 里面是从线上拷来的 jobs.db 副本（只读不改线上库）——
# 这样能看到**真实历史任务**的卡片，而不是空列表。
#
# 环境变量：
#   H3STUDIO_PORT        —— 独立端口（默认 8199）
#   H3STUDIO_DATA_DIR    —— 独立数据目录
#   AGENT_SESSION_DB / AGENT_MEMORY_DB —— 独立 Agent 库
set -euo pipefail

ROOT=/root/projects/h3studio-media-card
PORT="${H3STUDIO_PORT:-8199}"
DATA_DIR="${H3STUDIO_DATA_DIR:-/tmp/h3media-verify/data}"

mkdir -p "$DATA_DIR"
export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="$DATA_DIR"
export AGENT_SESSION_DB="$DATA_DIR/agent_sessions.db"
export AGENT_MEMORY_DB="$DATA_DIR/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
