#!/usr/bin/env bash
# 全量 pytest 的**失败集合**基线对比脚本（本轮 H3 面板修复用）。
#
# 【为什么不用一次全量跑】
# 各 worktree 的浏览器测试会打同一个端口、抢同一个浏览器 ->
# 并发跑必 flake。基线与分支必须**串行**跑，且要按文件分组跑。
#
# 【产出】
#   <outdir>/branch.txt   分支的失败用例集合（每行 "file::test"）
#   <outdir>/baseline.txt 基线的失败用例集合
#   <outdir>/diff.txt     差集（分支失败 - 基线失败）-> 必须为空
set -uo pipefail

BRANCH_DIR="${1:?用法: compare_suite.sh <branch_worktree> <baseline_worktree> <outdir>}"
BASE_DIR="${2:?}"
OUT="${3:?}"
PY=/home/ubuntu/comfyui/venv/bin/python

mkdir -p "$OUT"

run_tree() {
  local dir="$1" tag="$2"
  cd "$dir" || return 1
  # 逐个测试文件跑，文件之间**串行**（避免浏览器互抢）
  : > "$OUT/$tag.txt"
  local files
  files=$(ls tests/test_*.py | sort)
  for f in $files; do
    echo "### $f" >&2
    raw="$OUT/.raw.txt"
    # 【2026-11-12 修正】**不要**设 H3STUDIO_PORT / H3STUDIO_DATA_DIR。
    # server.py 的 load_config() 里环境变量优先级**高于** config.json，
    # 而测试夹具 StudioServer.start() 是用 config.json 写入自己随机取的
    # free_port()，然后去轮询那个随机端口。
    # 一旦外部注入 H3STUDIO_PORT=8291，server 就绑到 8291，
    # 夹具却仍在等随机端口 -> start() 恒返回 False ->
    # 全部浏览器用例报 "Studio 实例启动失败"（假失败）。
    # 数据目录同理：夹具用自己 tmp 目录，外层覆盖会污染共享 data-live。
    AGENT_SESSION_DB="$dir/data-live/agent_sessions.db" \
    AGENT_MEMORY_DB="$dir/data-live/agent_memory.db" \
    PYTHONPATH="$dir" \
    timeout 600 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no > "$raw" 2>&1 || true
    # pytest 的失败行格式： "FAILED tests/xxx.py::TestCls::test_name - AssertionError..."
    # 提取 "<本文件>::<用例名>"，去掉后面的 " - 原因"
    grep -E "^(FAILED|ERROR) " "$raw" | while IFS= read -r line; do
      case "$line" in
        FAILED*|ERROR*)
          rest="${line#* }"
          name="${rest%% *}"
          # name 已经是 "tests/xxx.py::TestCls::test_name"（pytest 自带路径）
          # 统一成 "<用例 id>" —— 不带文件名前缀，便于跨树比较
          printf '%s\n' "$name" >> "$OUT/$tag.txt"
          ;;
      esac
    done
  done
  sort -u "$OUT/$tag.txt" -o "$OUT/$tag.txt"
  echo "$tag failures: $(wc -l < "$OUT/$tag.txt")" >&2
}

run_tree "$BASE_DIR"  baseline
run_tree "$BRANCH_DIR" branch

echo "=== NEW failures (branch - baseline) ===" >&2
comm -13 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/diff.txt"
echo "=== FIXED failures (baseline - branch) ===" >&2
comm -23 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/fixed.txt"
echo "=== done ===" >&2
