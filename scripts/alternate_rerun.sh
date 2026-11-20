#!/usr/bin/env bash
# 交替验证 test_enhance_review_fixes.py：改动版 / 基线 / 改动版 / 基线
# 目的：判定「改动版失败」是**改动引入**还是**环境时序**。
set -u
OUT=/root/projects/h3s-deftruth/evidence/agent-cap-truth/phase2/regression/rerun_after_fixes.txt
{
  echo "=== 交替验证：test_enhance_review_fixes.py（改动版 / 基线 各两轮）==="
  for tree in h3s-deftruth h3s-base-truth h3s-deftruth h3s-base-truth; do
    echo "--- $tree ---"
    cd "/root/projects/$tree" || exit 1
    timeout 200 python3 -m pytest tests/test_enhance_review_fixes.py \
      -q --timeout=120 -p no:cacheprovider --tb=line -rN 2>&1 | tail -2
  done
} >> "$OUT" 2>&1
echo "DONE" >> "$OUT"
