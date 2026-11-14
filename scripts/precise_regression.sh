#!/usr/bin/env bash
# 受影响测试文件的精确回归（2026-11-15 媒体库卡片 LoRA 真值 + 详情模态框）
#
# 【为什么不全量跑】仓库有 111 个测试文件；全量串行要 20+ 分钟，
# 且 tests/test_agent.py 本身就可能跑很久。本次改动只碰了
#   h3studio/job_meta.py / h3studio/video_models.py / server.py /
#   web/app.js / web/job-meta.js
# 所以只跑**引用了这些模块或相关行为**的文件（下面这份清单由
# grep -l -E "job_meta|video_models|gen_params|lora|detailModal" 得到）。
#
# 【基线对比】每个文件在「改动树」与「基线树」各跑一次，比对失败集合 ——
# 只有「改动树新增的失败」才算回归。
set -uo pipefail

DEV=${1:-/root/projects/h3s-mc-truth}
BASE=${2:-/tmp/h3s-mc-truth-baseline}
OUT=${3:-/tmp/h3s-mc-truth-precise}
PY=/home/ubuntu/comfyui/venv/bin/python

mkdir -p "$OUT"

FILES=$(cd "$DEV" && grep -l -E "job_meta|video_models|gen_params|lora|detailModal" tests/test_*.py | sort)

run_one() {
  local dir="$1" tag="$2" f="$3"
  cd "$dir" || return 1
  local raw="$OUT/.raw.$tag.txt"
  # ⚠️ 绝不设 H3STUDIO_PORT / H3STUDIO_DATA_DIR ——
  #   server.py 的 load_config() 里环境变量优先级**高于** config.json，
  #   而测试夹具 StudioServer.start() 用 config.json 写自己随机端口，
  #   外部注入会让 start() 恒失败 -> 全部浏览器用例假失败。
  timeout 300 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no \
      > "$raw" 2>&1 || true
  grep -E "^(FAILED|ERROR) " "$raw" \
    | sed -E 's/^(FAILED|ERROR) ([^ ]+).*/\2/' >> "$OUT/$tag.txt"
}

: > "$OUT/baseline.txt"
: > "$OUT/branch.txt"

for f in $FILES; do
  echo "### $f"
  run_one "$BASE" baseline "$f"
  run_one "$DEV"  branch   "$f"
done

sort -u "$OUT/baseline.txt" -o "$OUT/baseline.txt"
sort -u "$OUT/branch.txt"   -o "$OUT/branch.txt"

echo
echo "=== 文件数: $(echo "$FILES" | wc -w) ==="
echo "baseline failures: $(wc -l < "$OUT/baseline.txt")"
echo "branch   failures: $(wc -l < "$OUT/branch.txt")"
echo
echo "=== NEW failures (branch - baseline) —— 必须为空 ==="
comm -13 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/diff.txt"
echo
echo "=== FIXED failures (baseline - branch) ==="
comm -23 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/fixed.txt"
echo
echo "=== done ==="
