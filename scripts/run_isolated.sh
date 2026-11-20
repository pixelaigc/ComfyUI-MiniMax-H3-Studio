#!/usr/bin/env bash
# 起一个隔离实例（用于验证，不碰线上 8190）。
# 用法: scripts/run_isolated.sh <src_dir> <port> [data_dir]
set -euo pipefail
SRC="$1"; PORT="$2"; DATA="${3:-/root/projects/comfyui-minimax-h3-studio/data}"
TMP=$(mktemp -d /tmp/h3s-iso-XXXXXX)
mkdir -p "$TMP/app"
cp -a "$SRC"/. "$TMP/app"/
rm -rf "$TMP/app/.git" "$TMP/app/data" "$TMP/app/evidence" "$TMP/app/__pycache__"
ln -s "$DATA" "$TMP/app/data"
echo "$TMP" > /tmp/h3s-iso-latest.txt
cd "$TMP/app"
H3STUDIO_PORT="$PORT" H3STUDIO_DATA_DIR="$DATA" \
  exec /home/ubuntu/comfyui/venv/bin/python -u server.py
