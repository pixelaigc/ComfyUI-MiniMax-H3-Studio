#!/usr/bin/env bash
# Agent 相关回归集 —— 本轮（收尾轮）用的**固定**测试集合。
#
# 【为什么写成脚本而不是每次手敲】
# 差集计算要求「改动树」与「基线树」跑**完全同一批**测试文件。
# 手敲容易漏文件 -> 差集数字就不可信。
#
# 用法：
#   bash scripts/run_agent_regression.sh <repo_root> <out_file>
#
# 排除项说明（都是**浏览器/端到端**测试 —— 要起真 Chromium + 真 ComfyUI，
# 耗时 30 分钟以上且本身有环境相关的抖动；它们单独跑）：
#   test_agent_layout_browser.py
#   test_agent_paste_browser.py
#   test_music_flow_browser.py
#   test_qwen_refs_browser.py
#   test_agent_oneshot_e2e.py
#   test_page_size.py
#   test_grid_flash.py

set -u
REPO="${1:?需要 repo root}"
OUT="${2:?需要输出文件}"
PY=/home/ubuntu/comfyui/venv/bin/python

cd "$REPO" || exit 2

FILES=(
  tests/test_agent.py
  tests/test_agent_backend_enh.py
  tests/test_agent_contract.py
  tests/test_agent_fabricated_job_fix.py
  tests/test_agent_frontend_enh.py
  tests/test_agent_injection_key_path.py
  tests/test_agent_legacy_fixes.py
  tests/test_agent_media_av_upload.py
  tests/test_agent_media_semantics_backend.py
  tests/test_agent_media_semantics_frontend.py
  tests/test_agent_media_upload.py
  tests/test_agent_power.py
  tests/test_agent_prompt_history_hook.py
  tests/test_agent_review_fixes.py
  tests/test_agent_session_frontend.py
  tests/test_agent_session_mgmt.py
  tests/test_agent_session_search_cascade.py
  tests/test_agent_session_status_artifacts.py
  tests/test_agent_stream_sse.py
  tests/test_agent_tool_status_fix.py
  tests/test_continuation_capability.py
  tests/test_node_probe_completeness.py
  tests/test_t2v_panel.py
  tests/test_t2v_panel_fix.py
  tests/test_t2v_meta_consistency.py
  tests/test_t2v_card_dom.py
  tests/test_media_card_detail.py
)

echo "=== repo: $REPO ==="
date -Is

# 【为什么要剔除不存在的文件】
# 基线树（local/maintainer f4bc747）**没有**本次新增的测试文件
# （test_agent_power.py / test_node_probe_completeness.py）。
# 直接跑会报 "file not found" -> 那不是「失败」，是「不存在」。
# 这里显式剔除并**打印出来** —— 差集必须只比「两边都有的文件」，
# 否则会把「基线缺文件」误算成「基线通过」或「改动树新增失败」。
#
# 剔除逻辑：先在基线树试一次（--collect-only），不存在的文件被剔除；
# 两边都跑**剔除后**的同一份清单 -> 差集 apples-to-apples。
EXISTING=()
MISSING=()
for f in "${FILES[@]}"; do
  if [ -f "$f" ]; then
    EXISTING+=("$f")
  else
    MISSING+=("$f")
  fi
done

echo "=== 文件数: ${#EXISTING[@]}（共 ${#FILES[@]}） ==="
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "=== 本树不存在的文件（已剔除，不计入通过/失败） ==="
  for f in "${MISSING[@]}"; do echo "  SKIP $f"; done
fi
echo "=== 实际跑的清单 ==="
for f in "${EXISTING[@]}"; do echo "  RUN  $f"; done

# -p no:randomly 关掉随机顺序（差集必须可复现）
"$PY" -m pytest "${EXISTING[@]}" -q -p no:randomly --no-header 2>&1 | tee "$OUT"

echo "=== exit: $? ==="
date -Is
