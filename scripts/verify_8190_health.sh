#!/usr/bin/env bash
# 8190 探活：HTTP 接口 + 静态资源，逐个记录状态码与耗时
# 用法: bash scripts/verify_8190_health.sh [base_url]
set -uo pipefail
BASE="${1:-http://127.0.0.1:8190}"
OUT="${OUT:-/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-立即上线/8190-health.txt}"
mkdir -p "$(dirname "$OUT")"

# 真实存在的端点（已核对 server.py 路由表）：
#   /                       首页
#   /api/video-options      视频面板选项（含 sampling / model_list_source）
#   /api/video-categories   视频类别（video-tabs 新增）
#   /api/comfy-models       ComfyUI 模型清单（model-list-fix 新增）
#   /api/image-loras        图像 LoRA 清单
#   /api/image-options      图像面板选项
#   /api/agent/tools        Agent 工具注册表
#   /api/agent/sessions     Agent 会话列表
#   /api/generation-types   生成类型
#   /api/music-options      音乐面板
#   /api/jobs               任务列表
#   /api/system             系统信息
#   /api/hardware           硬件信息
#   /api/providers          Provider 列表
#   /api/prompt-history     提示词历史
ENDPOINTS=(
  "/"
  "/api/video-options"
  "/api/video-categories"
  "/api/comfy-models"
  "/api/image-loras"
  "/api/image-options"
  "/api/agent/tools"
  "/api/agent/sessions"
  "/api/generation-types"
  "/api/music-options"
  "/api/jobs"
  "/api/system"
  "/api/hardware"
  "/api/providers"
  "/api/prompt-history"
)
# 静态资源（index.html 里实际引用的）
STATIC=(
  "/static/app.js"
  "/static/video-panel.js"
  "/static/video-panel-fallback.js"
  "/static/video-nav.js"
  "/static/agent.js"
  "/static/agent-media.js"
  "/static/agent_sessions.js"
  "/static/style.css"
  "/static/enhance-menu.js"
  "/static/music.js"
  "/static/assets.js"
  "/static/providers.js"
  "/static/prompt-history.js"
  "/static/moss-layout.js"
  "/static/qwen-refs.js"
  "/static/interaction-guard.js"
  "/static/music-enhance.js"
  "/static/enhance-menu-shared.js"
)

{
  echo "=== 8190 探活 ==="
  echo "base=$BASE  time=$(date '+%F %T %Z')"
  echo
  echo "--- API 端点 ---"
} | tee "$OUT"

fail=0
for ep in "${ENDPOINTS[@]}"; do
  read -r code time <<<"$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -m 30 "$BASE$ep")"
  printf '  %-28s %s  %ss\n' "$ep" "$code" "$time" | tee -a "$OUT"
  [ "$code" = "200" ] || fail=$((fail+1))
done

echo "--- 静态资源 ---" | tee -a "$OUT"
for ep in "${STATIC[@]}"; do
  read -r code time <<<"$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -m 30 "$BASE$ep")"
  printf '  %-32s %s  %ss\n' "$ep" "$code" "$time" | tee -a "$OUT"
  [ "$code" = "200" ] || fail=$((fail+1))
done

echo | tee -a "$OUT"
echo "非 200 数量 = $fail" | tee -a "$OUT"
exit $fail
