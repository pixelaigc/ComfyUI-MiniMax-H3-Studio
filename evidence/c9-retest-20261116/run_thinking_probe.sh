#!/bin/bash
# 隔离实验：分别用 4 种 payload 直连上游，定位「为什么 enable_thinking=false 没生效」
# A: 无 tools + enable_thinking=false
# B: 有 tools + enable_thinking=false（工作台 Agent 的真实形状）
# C: 无 tools + reasoning_effort=none
# D: 有 tools + 无任何 thinking 控制（对照组）
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
M=qwen3.8-flash-next-iq3_xxs
B=http://127.0.0.1:5000/v1/chat/completions

run () {
  local tag="$1"; shift
  local S=$(date +%s%3N)
  local C=$(curl -s -m 115 -o "$OUT/thr_${tag}.json" -w "%{http_code}" \
    -X POST -H 'Content-Type: application/json' -d "$1" "$B" 2>/dev/null)
  local E=$(date +%s%3N)
  echo "{\"case\":\"$tag\",\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/thr_${tag}.json.meta"
}

TOOLS='[{"type":"function","function":{"name":"list_modes","description":"List supported generation modes","parameters":{"type":"object","properties":{}}}}]'

run A "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"chat_template_kwargs\":{\"enable_thinking\":false}}"

run B "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"tools\":$TOOLS,\"chat_template_kwargs\":{\"enable_thinking\":false}}"

run C "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"reasoning_effort\":\"none\"}"

run D "{\"model\":\"$M\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":1024,\"temperature\":0.2,\"top_p\":0.95,\"stream\":false,\"tools\":$TOOLS}"

cat "$OUT"/thr_*.meta
