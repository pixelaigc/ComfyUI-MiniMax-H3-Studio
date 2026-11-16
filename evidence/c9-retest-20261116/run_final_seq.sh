#!/bin/bash
# c9 修正版部署后 · 线上最终抽样（顺序 4 次，timeout 560s）
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
mkdir -p "$OUT"
for i in 1 2 3 4; do
  S=$(date +%s%3N)
  C=$(curl -s -m 560 -o "$OUT/final_s${i}_body.json" -w "%{http_code}" \
      -X POST -H 'Content-Type: application/json' \
      -d '{"message":"ping"}' http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
  E=$(date +%s%3N)
  echo "{\"sample\":$i,\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/final_s${i}.json"
  sleep 8
done
cat "$OUT"/final_s1.json "$OUT"/final_s2.json "$OUT"/final_s3.json "$OUT"/final_s4.json
