#!/usr/bin/env bash
# 复跑「改动版全量里失败、基线里通过」的文件，逐个记录结果。
# 目的：区分**真实新增回归**与**并发/共享素材库/时序抖动**造成的假失败。
set -u
cd /root/projects/h3s-deftruth
OUT=/root/projects/h3s-deftruth/evidence/agent-cap-truth/phase2/regression/rerun_after_fixes.txt
: > "$OUT"
FILES="tests/test_agent_backend_enh.py tests/test_agent_fabricated_job_fix.py tests/test_enhance_review_fixes.py tests/test_music_yue2.py tests/test_video_sampling_params.py"
for f in $FILES; do
  echo "=== $f ===" | tee -a "$OUT"
  timeout 400 python3 -m pytest "$f" -q --timeout=120 -p no:cacheprovider \
      --tb=line -rN 2>&1 | tail -4 | tee -a "$OUT"
  echo | tee -a "$OUT"
done
echo "DONE" | tee -a "$OUT"
