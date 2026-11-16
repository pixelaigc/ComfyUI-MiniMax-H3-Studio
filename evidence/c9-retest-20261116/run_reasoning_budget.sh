#!/bin/bash
# 关键实验：上游支持请求级 reasoning_budget_tokens —— 带上它能否把思考压下来
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
M=qwen3.8-flash-next-iq3_xxs
B=http://127.0.0.1:5000/v1/chat/completions
TOOLS='[{"type":"function","function":{"name":"list_modes","description":"List supported generation modes","parameters":{"type":"object","properties":{}}}}]'

run () {
  local tag="$1"; local payload="$2"
  local S=$(date +%s%3N)
  local C=$(curl -s -m 200 -o "$OUT/rb_${tag}.json" -w "%{http_code}" \
    -X POST -H 'Content-Type: application/json' -d "$payload" "$B" 2>/dev/null)
  local E=$(date +%s%3N)
  echo "{\"case\":\"$tag\",\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/rb_${tag}.meta"
}

# E: reasoning_budget_tokens=0 (关思考预算) + enable_thinking=false
run E "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"reasoning_budget_tokens\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}"

# F: reasoning_budget_tokens=256 (小预算) + tools（工作台真实形状）
run F "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"tools\":$TOOLS,\"reasoning_budget_tokens\":256,\"chat_template_kwargs\":{\"enable_thinking\":false}}"

cat "$OUT"/rb_E.meta "$OUT"/rb_F.meta
