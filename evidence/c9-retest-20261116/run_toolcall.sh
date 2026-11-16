#!/bin/bash
# c9 修复后线上实测 · 真实工具调用（不是 ping）：让 Agent 用 list_modes 工具列生成模式
OUT=/root/projects/h3studio-live/evidence/c9-retest-20261116
S=$(date +%s%3N)
C=$(curl -s -m 560 -o "$OUT/fix_toolcall_body.json" -w "%{http_code}" \
    -X POST -H 'Content-Type: application/json' \
    -d '{"message":"请用 list_modes 工具列出本工作台支持的生成模式"}' \
    http://127.0.0.1:8190/api/agent/chat 2>/dev/null)
E=$(date +%s%3N)
echo "{\"case\":\"tool_call_list_modes\",\"http\":\"$C\",\"elapsed_ms\":$((E-S))}" > "$OUT/fix_toolcall.json"
