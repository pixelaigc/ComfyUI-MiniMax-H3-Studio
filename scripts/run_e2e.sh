#!/usr/bin/env bash
# H3 Studio 真实浏览器 E2E 测试集 —— 启动脚本。
#
# 用法：
#   scripts/run_e2e.sh                    # 默认：隔离实例 + 假 ComfyUI（不占 GPU）
#   scripts/run_e2e.sh --live             # 连线上 8190（真实部署，真实 ComfyUI）
#   scripts/run_e2e.sh --headed           # 有头模式（需要 DISPLAY）
#   scripts/run_e2e.sh --real-comfy       # 隔离实例，但连真 ComfyUI（会占 GPU）
#   scripts/run_e2e.sh --only test_image_flow.py
#
# 【为什么默认是「隔离实例 + 假 ComfyUI」】
# 真实 ComfyUI 一次文生视频要 1~7 分钟，还要求 GPU 空闲（本机 4090D
# 常年被别人占满）。E2E 要稳定复现整条链路，就得能**精确控制时序**。
# 假 ComfyUI（tests/e2e/fake_comfyui_e2e.py）会真的产出媒体字节、
# 真的落盘潜空间（接续链路必需），只是不跑真模型。
# 想测真出图就加 --real-comfy 或 --live。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PY="${E2E_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in /home/ubuntu/comfyui/venv/bin/python /usr/bin/python3 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "找不到 python；请设 E2E_PYTHON" >&2
  exit 1
fi

EXTRA=()
HEADED=0
LIVE=0
REAL_COMFY=0
ONLY=""
RUN_ID="$(date +%Y%m%d-%H%M%S)"

while [ $# -gt 0 ]; do
  case "$1" in
    --headed) HEADED=1; shift ;;
    --live) LIVE=1; shift ;;
    --real-comfy) REAL_COMFY=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --run-id) RUN_ID="$2"; shift 2 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [ "$LIVE" = "1" ]; then
  # 连线上部署：真实界面 + 真实后端 + 真实 ComfyUI。
  BASE="http://127.0.0.1:8190"
  if ! curl -fsS -m 5 "$BASE/api/video-categories" >/dev/null 2>&1; then
    echo "线上服务 $BASE 不可达（systemctl status h3-studio.service）" >&2
    exit 1
  fi
  ARGS=(--e2e-base "$BASE" --e2e-run-id "$RUN_ID")
  echo "[e2e] 模式：线上 8190（真实 ComfyUI，会占 GPU、耗时以分钟计）"
else
  ARGS=(--e2e-isolated --e2e-run-id "$RUN_ID")
  if [ "$REAL_COMFY" = "1" ]; then
    ARGS+=(--e2e-real-comfy)
    echo "[e2e] 模式：隔离实例 + 真实 ComfyUI（会占 GPU）"
  else
    echo "[e2e] 模式：隔离实例 + 假 ComfyUI（不占 GPU，时序可控）"
  fi
fi

if [ "$HEADED" = "1" ]; then
  if [ -z "${DISPLAY:-}" ]; then
    # 没有 X 就用 Xvfb 起一个虚拟显示（有头复验用）
    if command -v Xvfb >/dev/null 2>&1; then
      export DISPLAY=:99
      Xvfb :99 -screen 0 1600x1000x24 >/tmp/xvfb-e2e.log 2>&1 &
      XVFB_PID=$!
      trap 'kill $XVFB_PID 2>/dev/null || true' EXIT
      sleep 2
      echo "[e2e] 已起 Xvfb（DISPLAY=:99）"
    else
      echo "没有 DISPLAY 且没有 Xvfb —— 有头模式无法运行" >&2
      exit 1
    fi
  fi
  ARGS+=(--e2e-headed)
fi

TARGET="tests/e2e"
if [ -n "$ONLY" ]; then
  case "$ONLY" in
    /*) TARGET="$ONLY" ;;
    *)  TARGET="tests/e2e/$ONLY" ;;
  esac
fi

echo "[e2e] 证据目录：evidence/e2e/$RUN_ID"
exec "$PY" -m pytest "$TARGET" \
  -p no:randomly \
  --tb=short \
  -o console_output_style=progress \
  "${ARGS[@]}" "$@"
