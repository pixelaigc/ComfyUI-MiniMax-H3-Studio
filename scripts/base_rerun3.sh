#!/usr/bin/env bash
# 基线工作树单独跑 test_enhance_review_fixes.py 三轮 —— 判定该失败是否为环境时序问题。
set -u
OUT=/root/projects/h3s-deftruth/evidence/agent-cap-truth/phase2/regression/rerun_after_fixes.txt
{
  echo "=== 基线工作树单独跑 3 轮（test_enhance_review_fixes.py）==="
  cd /root/projects/h3s-base-truth || exit 1
  for i in 1 2 3; do
    echo "--- base round $i ---"
    timeout 200 python3 -m pytest tests/test_enhance_review_fixes.py \
      -q --timeout=120 -p no:cacheprovider --tb=line -rN 2>&1 | tail -2
  done
} >> "$OUT" 2>&1
echo "DONE-BASE3" >> "$OUT"
