#!/bin/bash
# c9 复测 · 快速抽样（timeout 60s，模拟宿主验收轮的实际超时口径）
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
for i in 1 2 3; do
  S=$(date +%s%3N)
  C=$(curl -s -m 60 -o "$OUT/c9_q${i}_body.json" -w "%{http_code}" \
      -X POST -H 'Content-Type: application/json' \
      -d '{"message":"ping"}' http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
  E=$(date +%s%3N)
  echo "{\"sample\":$i,\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/c9_q${i}.json"
  sleep 5
done
cat "$OUT"/c9_q1.json "$OUT"/c9_q2.json "$OUT"/c9_q3.json
