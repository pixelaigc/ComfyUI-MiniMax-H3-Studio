#!/usr/bin/env bash
# D1 验收用独立实跑实例（端口 8199）—— 不碰 8190 线上服务与 systemd unit。
#
# 【为什么不复用 scripts/run_live_8199_t2v.sh】那个脚本指向
# /root/projects/h3studio-t2v-sampler（另一个 worktree / 另一个分支）。
# 验收证据必须跑在**本 worktree 本分支**的代码上，
# 否则「证据对应的代码」与「分支 HEAD」对不上，证据不可采信。
#
# 【数据目录为什么不用 /tmp】2026-11-13 宿主重启后 /tmp 被清空，
# 上一轮证据包里 9 个视频产物的绝对路径**全部失效**（0/9 可达）。
# 验收产物必须**扛得住宿主重启**，所以放在仓库内的
# `data-accept/`（`.gitignore` 已排除 `*.db` 与 `outputs/`，
# 数据目录本身不入库，但**不会**被 /tmp 清空）。
set -euo pipefail

ROOT=/root/projects/h3studio-t2v-continue
PORT=${H3STUDIO_ACCEPT_PORT:-8199}
DATA=${H3STUDIO_ACCEPT_DATA_DIR:-$ROOT/data-accept}

mkdir -p "$DATA"

# 【端口可能被并发 agent 占掉】2026-11-13 重启后 8199 已被另一个 worktree
# （h3studio-gen-queue）占用 -> 本实例 bind 失败退出。
# 这里**自动顺延**到第一个空闲端口，并把实际端口写进
# data-accept/PORT 供验收脚本读取 —— 不再硬编码 8199。
# 【为什么不用 `ss | grep -q`】`grep -q` 命中后立刻退出 -> `ss` 收到
# SIGPIPE -> 在 `set -o pipefail` 下整条管道返回非零 -> 端口明明被占用
# 却被判成「空闲」（2026-11-13 实测踩到）。改用 `grep -c`（读完全部输入，
# 无 SIGPIPE），计数 >0 才算占用。
port_busy() {
  local n
  n=$(ss -ltn 2>/dev/null | grep -c ":$1 " || true)
  [ "${n:-0}" -gt 0 ]
}

if port_busy "$PORT"; then
  for cand in $(seq 8199 8240); do
    if ! port_busy "$cand"; then
      PORT="$cand"
      break
    fi
  done
fi
echo "$PORT" > "$DATA/PORT"
echo "[run_live_8199_d1] 实际端口: $PORT（数据目录 $DATA）"

export H3STUDIO_PORT="$PORT"
export H3STUDIO_DATA_DIR="$DATA"
export AGENT_SESSION_DB="$DATA/agent_sessions.db"
export AGENT_MEMORY_DB="$DATA/agent_memory.db"
export PYTHONPATH="$ROOT"

cd "$ROOT"
exec /home/ubuntu/comfyui/venv/bin/python server.py
