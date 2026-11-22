#!/usr/bin/env bash
# 起一个隔离实例（用于验证，不碰线上 8190）。
# 用法: scripts/run_isolated.sh <src_dir> <port> [data_dir]
#
# 【2026-11-23 改】不传 data_dir 时，默认**新建一个临时 data_dir**，
# 不再默认用线上 `/root/projects/comfyui-minimax-h3-studio/data`。
# 原因：脚本标题写着「不碰线上 8190」，默认值却直接把实例的
# 数据目录指到线上 —— 这样的实例会往用户的真实媒体库写产物。
# 实测事故：某个「隔离实例」的 data_dir 是临时目录，但它的
# `outputs` 是指向线上媒体库的符号链接，于是线上媒体库多了两个
# 没有任何任务记录认领的视频文件（v-h3-studio-000254/000255）。
# 需要复用线上数据时**显式**传第三个参数（传真实目录，别传符号链接）。
set -euo pipefail
SRC="$1"; PORT="$2"
if [ $# -ge 3 ]; then
  DATA="$3"
  [ -e "$DATA" ] || { echo "data_dir 不存在: $DATA" >&2; exit 2; }
  [ -L "$DATA" ] && { echo "data_dir 是符号链接: $DATA -> $(readlink "$DATA")（请传真实目录）" >&2; exit 2; }
else
  DATA=$(mktemp -d /tmp/h3s-iso-data-XXXXXX)
  echo "[run_isolated] 未指定 data_dir -> 用隔离临时目录 $DATA（不写线上媒体库）"
fi
TMP=$(mktemp -d /tmp/h3s-iso-XXXXXX)
mkdir -p "$TMP/app"
cp -a "$SRC"/. "$TMP/app"/
rm -rf "$TMP/app/.git" "$TMP/app/data" "$TMP/app/evidence" "$TMP/app/__pycache__"
ln -s "$DATA" "$TMP/app/data"
echo "$TMP" > /tmp/h3s-iso-latest.txt
cd "$TMP/app"
H3STUDIO_PORT="$PORT" H3STUDIO_DATA_DIR="$DATA" \
  exec /home/ubuntu/comfyui/venv/bin/python -u server.py
