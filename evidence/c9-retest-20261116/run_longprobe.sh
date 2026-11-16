#!/bin/bash
# 长超时探针：验证「上游单槽串行 + 别人的长请求占槽」假说
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
M=qwen3.8-flash-next-iq3_xxs
B=http://127.0.0.1:5000/v1/chat/completions
S=$(date +%s%3N)
C=$(curl -s -m 560 -o "$OUT/longprobe_body.json" -w "%{http_code}" \
  -X POST -H 'Content-Type: application/json' \
  -d "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"say ok\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
  "$B" 2>/dev/null)
E=$(date +%s%3N)
echo "{\"case\":\"longprobe_max_tokens_1024_thinking_off\",\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/longprobe.json.meta"
