#!/bin/bash
# c9 复测：并行抽样 POST /api/agent/chat，记录 HTTP 码与耗时
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
mkdir -p "$OUT"
for i in 1 2 3; do
  (
    S=$(date +%s)
    C=$(curl -s -m 170 -o "$OUT/c9_sample${i}_body.json" -w "%{http_code}" \
        -X POST -H 'Content-Type: application/json' \
        -d '{"message":"ping"}' http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
    E=$(date +%s)
    echo "{\"sample\":$i,\"http\":\"$C\",\"elapsed_sec\":$((E-S))}" > "$OUT/c9_sample${i}.json"
  ) &
done
wait
cat "$OUT"/c9_sample1.json "$OUT"/c9_sample2.json "$OUT"/c9_sample3.json
