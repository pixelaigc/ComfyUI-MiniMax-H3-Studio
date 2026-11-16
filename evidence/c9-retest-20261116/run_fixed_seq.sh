#!/bin/bash
# c9 修复后线上实测：顺序抽样 POST /api/agent/chat（timeout 560s，覆盖新的 482.88s 预算）
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
mkdir -p "$OUT"
for i in 1 2 3; do
  S=$(date +%s%3N)
  C=$(curl -s -m 560 -o "$OUT/fix_s${i}_body.json" -w "%{http_code}" \
      -X POST -H 'Content-Type: application/json' \
      -d '{"message":"ping"}' http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
  E=$(date +%s%3N)
  echo "{\"sample\":$i,\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/fix_s${i}.json"
  sleep 10
done
cat "$OUT"/fix_s1.json "$OUT"/fix_s2.json "$OUT"/fix_s3.json
