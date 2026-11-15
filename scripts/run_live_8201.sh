#!/usr/bin/env bash
# 独立实跑实例（端口 8201）—— 用于 2026-11-15 修复③ 的真机浏览器实测。
#
# 【为什么不直接用 8190】8190 是线上服务（systemd h3-studio.service，
# WorkingDirectory=/root/projects/h3studio-live）。本任务在**独立 worktree**
# （/root/projects/h3s-uicontrols，分支 local/feature/ui-controls-preview）
# 上开发，实测期间**不碰**线上服务 —— 等验证通过再合并部署。
#
# 数据目录也独立（data-live8201/），实测产生的任务与证据不污染线上 jobs.db。
set -euo pipefail

ROOT=/root/projects/h3s-uicontrols
PORT="${H3STUDIO_PORT:-8201}"
export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="$ROOT/data-live8201"
export AGENT_SESSION_DB="$ROOT/data-live8201/agent_sessions.db"
export AGENT_MEMORY_DB="$ROOT/data-live8201/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
