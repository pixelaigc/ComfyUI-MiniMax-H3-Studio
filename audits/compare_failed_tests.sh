#!/usr/bin/env bash
# 对「clean fix 全量跑出的 23 条失败」逐条在 baseline 与 fix 两棵树上单独复跑，
# 判定它们是**基线本来就红**（与我的改动无关）还是**我引入的新失败**。
# 用法：bash audits/compare_failed_tests.sh
set -uo pipefail
BASE=/root/projects/h3s-baseline-closeout
FIX=/root/projects/h3s-closeout-20261115
LIST=/root/projects/comfyui-minimax-h3-studio/audits/fix_failed_list.txt
OUT=/root/projects/comfyui-minimax-h3-studio/audits/failed_compare_23.txt

: > "$OUT"
while read -r line; do
  t="${line#FAILED }"
  echo "### $t" >> "$OUT"
  for tree in BASE FIX; do
    d=$BASE; [ "$tree" = FIX ] && d=$FIX
    res=$(cd "$d" && timeout 600 /home/ubuntu/comfyui/venv/bin/python -m pytest "$t" \
            -q --tb=no -p no:cacheprovider 2>&1 | grep -E "^[0-9]+ (passed|failed)|passed|failed" | tail -1)
    echo "  $tree: $res" >> "$OUT"
  done
done < "$LIST"
echo "DONE -> $OUT"
