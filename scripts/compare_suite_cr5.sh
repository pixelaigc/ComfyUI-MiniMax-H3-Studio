#!/usr/bin/env bash
# 第5轮代码审查 —— 全量 pytest **失败集合**对拍（基线 vs 本轮），4 路分片并行。
#
# 【为什么分片】
# 137 个测试文件串行跑要 40+ 分钟（其中浏览器用例单文件就能吃掉 300s timeout）。
# 分 4 片并行把时间压到 1/4 左右。
#
# 【为什么「失败集合」而不是「passed/failed 总数」】
# 各 worktree 的浏览器测试会抢同一个浏览器/端口 -> 并发跑必 flake。
# 所以：**分片内串行**（一个 worktree 同时只跑一个 pytest），
# 但**分片间并行**（不同分片跑不同文件子集，端口互不重叠）。
#
# 【产出】
#   <outdir>/baseline.txt   基线失败用例集合（每行 "tests/xxx.py::TestCls::test_name"）
#   <outdir>/branch.txt     本轮失败用例集合
#   <outdir>/diff.txt       新增失败（本轮 - 基线）-> 必须为空
#   <outdir>/fixed.txt      修好的（基线 - 本轮）
set -uo pipefail

BRANCH_DIR="${1:?用法: compare_suite_cr5.sh <branch_worktree> <baseline_worktree> <outdir> [shards]}"
BASE_DIR="${2:?}"
OUT="${3:?}"
SHARDS="${4:-4}"
PY=/home/ubuntu/comfyui/venv/bin/python

mkdir -p "$OUT"

# 收集测试文件清单（排序保证可复现）
mapfile -t FILES < <(cd "$BRANCH_DIR" && ls tests/test_*.py | sort)

# ---- 文件分片：轮询分配，保证每片文件数均衡 ----
declare -a SHARD_FILES
for ((i=0; i<SHARDS; i++)); do SHARD_FILES[$i]=""; done
idx=0
for f in "${FILES[@]}"; do
  s=$((idx % SHARDS))
  SHARD_FILES[$s]+="$f"$'\n'
  idx=$((idx+1))
done

run_shard() {
  local tree="$1" tag="$2" shard="$3"
  local files
  files=$(printf '%s' "${SHARD_FILES[$shard]}")
  cd "$tree" || return 1
  : > "$OUT/.raw.$tag.$shard.txt"
  local f
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "### $f" >> "$OUT/.raw.$tag.$shard.txt"
    # 【不要设 H3STUDIO_PORT / H3STUDIO_DATA_DIR】
    # server.py 的 load_config() 里环境变量优先级**高于** config.json，
    # 而测试夹具 StudioServer.start() 是用 config.json 写入自己随机取的
    # free_port()，然后去轮询那个随机端口。
    # 一旦外部注入 H3STUDIO_PORT=8291，server 就绑到 8291，夹具却仍在等
    # 随机端口 -> start() 恒返回 False -> 全部浏览器用例报「Studio 实例启动失败」
    # （假失败）。数据目录同理。
    AGENT_SESSION_DB="$tree/data-live/agent_sessions.db" \
    AGENT_MEMORY_DB="$tree/data-live/agent_memory.db" \
    PYTHONPATH="$tree" \
    timeout 600 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no \
      >> "$OUT/.raw.$tag.$shard.txt" 2>&1 || true
  done <<< "$files"
}

echo "=== 启动 $SHARDS 路分片（baseline + branch 共 $((SHARDS*2)) 路）===" >&2
for s in $(seq 0 $((SHARDS-1))); do
  run_shard "$BASE_DIR"   "baseline" "$s" &
  run_shard "$BRANCH_DIR" "branch"   "$s" &
done
wait

echo "=== 汇总失败集合 ===" >&2
for tag in baseline branch; do
  : > "$OUT/$tag.txt"
  for s in $(seq 0 $((SHARDS-1))); do
    grep -E "^(FAILED|ERROR) " "$OUT/.raw.$tag.$s.txt" 2>/dev/null \
      | while IFS= read -r line; do
        rest="${line#* }"
        name="${rest%% *}"
        printf '%s\n' "$name"
      done >> "$OUT/$tag.txt"
  done
  sort -u "$OUT/$tag.txt" -o "$OUT/$tag.txt"
  echo "$tag failures: $(wc -l < "$OUT/$tag.txt")" >&2
done

echo "=== NEW failures (branch - baseline) ===" >&2
comm -13 "$OUT/baseline.txt" "$OUT/branch.txt" > "$OUT/diff.txt"
cat "$OUT/diff.txt"
echo "=== FIXED failures (baseline - branch) ===" >&2
comm -23 "$OUT/baseline.txt" "$OUT/branch.txt" > "$OUT/fixed.txt"
cat "$OUT/fixed.txt"
echo "=== done ===" >&2
