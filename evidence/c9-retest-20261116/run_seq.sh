#!/bin/bash
# c9 复测 · 顺序抽样（避开并发闸门 429 干扰），每轮间隔 8s
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
mkdir -p "$OUT"
for i in 1 2 3 4 5; do
  S=$(date +%s%3N)
  C=$(curl -s -m 170 -o "$OUT/c9_seq${i}_body.json" -w "%{http_code}" \
      -X POST -H 'Content-Type: application/json' \
      -d '{"message":"用 list_modes 工具列一下本工作台支持的生成模式"}' \
      http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
  E=$(date +%s%3N)
  echo "{\"sample\":$i,\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/c9_seq${i}.json"
  sleep 8
done
cat "$OUT"/c9_seq*.json
