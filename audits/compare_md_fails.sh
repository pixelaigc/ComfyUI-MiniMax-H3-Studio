#!/usr/bin/env bash
# 把 fix 树跑出的失败用例逐条在 baseline(8314f43) 与 fix 两棵树上单独复跑，
# 判定「本分支新引入的失败」条数。
set -uo pipefail
BASE=/root/projects/h3s-md-base
FIX=/root/projects/h3s-closeout-20261115
LIST=/root/projects/comfyui-minimax-h3-studio/audits/md_fail_names.txt
OUT=/root/projects/comfyui-minimax-h3-studio/audits/md_fail_compare.txt
: > "$OUT"
while read -r t; do
  [ -z "$t" ] && continue
  echo "### $t" >> "$OUT"
  for tree in BASE FIX; do
    d=$BASE; [ "$tree" = FIX ] && d=$FIX
    res=$(cd "$d" && timeout 600 /home/ubuntu/comfyui/venv/bin/python -m pytest "$t" \
            -q --tb=no -p no:cacheprovider 2>&1 | grep -E "passed|failed" | tail -1)
    echo "  $tree: $res" >> "$OUT"
  done
done < "$LIST"
echo "DONE -> $OUT"
