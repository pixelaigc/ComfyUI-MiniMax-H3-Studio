#!/usr/bin/env bash
# 复跑「基线与改动版都失败」的 21 个文件（改动版工作树，单独跑以排除并发干扰）。
set -u
cd /root/projects/h3s-deftruth
OUT=/root/projects/h3s-deftruth/evidence/agent-cap-truth/phase2/regression/rerun_both_fail.txt
: > "$OUT"
FILES="tests/test_agent.py tests/test_agent_media_av_upload.py tests/test_agent_media_upload.py
tests/test_audit_fixes.py tests/test_continuation_retry.py tests/test_d11_moss_lora_final.py
tests/test_e2e_gen_flow_contract.py tests/test_enhance_architecture_fixes.py
tests/test_enhance_menu.py tests/test_enhance_owner_badge.py tests/test_enhance_unify.py
tests/test_lora_file_integrity.py tests/test_moss_lora_damaged_and_partial.py
tests/test_music_flow_browser.py tests/test_page_size.py tests/test_qwen_refs_browser.py
tests/test_ref_context_remove.py tests/test_yue2_flow_review_fixes.py"
for f in $FILES; do
  echo "=== $f ===" | tee -a "$OUT"
  timeout 400 python3 -m pytest "$f" -q --timeout=120 -p no:cacheprovider \
      --tb=line -rN 2>&1 | tail -4 | tee -a "$OUT"
  echo | tee -a "$OUT"
done
echo "DONE" | tee -a "$OUT"
