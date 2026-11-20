#!/usr/bin/env bash
# ==================================================================
# 全量 pytest 的**失败集合**基线对比（基线树 vs 候选树，串行）
#
# 【为什么要隔离 H3STUDIO_DATA_DIR —— 这条是硬要求，不是可选】
# config.json 里的 data_dir 指向**线上共用的** data 目录
# (/root/projects/comfyui-minimax-h3-studio/data)，而 server.DB_PATH
# 就是 <data_dir>/jobs.db。
# ⇒ 任何调用 create_job_record / api_create 的用例都会**往线上
#   jobs.db 里写任务**；线上 worker 随后会把这些测试任务当真实任务
#   跑起来（占 GPU、产出视频）。
#
# 2026-11-20 实测踩坑：直接在 live worktree 跑全量 pytest，往线上
# jobs.db 写进 24 条测试任务，其中 5 条被线上 worker 真的跑完出了
# 视频。已清理（只删 prompt 为 'x' / 'next' 的测试产物，真实任务保留）。
#
# 所以这里**强制**用独立的 H3STUDIO_DATA_DIR —— ��试产物落在 /tmp，
# 绝不碰线上。
#
# 【为什么端口也不注入】server.load_config() 的环境变量优先级**高于**
# config.json，而测试夹具 StudioServer.start() 是用 config.json 写入
# 自己随机取的 free_port()，然后去轮询那个随机端口。一旦外部注入
# H3STUDIO_PORT，server 绑到注入的端口、夹具却仍在等随机端口 ->
# start() 恒返回 False -> 全部浏览器用例报「Studio 实例启动失败」
# （假失败）。
#
# 用法: compare_suite.sh <branch_worktree> <baseline_worktree> <outdir>
# ==================================================================
set -uo pipefail

BRANCH_DIR="${1:?用法: compare_suite.sh <branch_worktree> <baseline_worktree> <outdir>}"
BASE_DIR="${2:?}"
OUT="${3:?}"
PY=/home/ubuntu/comfyui/venv/bin/python

mkdir -p "$OUT"

run_tree() {
  local dir="$1" tag="$2"
  cd "$dir" || return 1
  : > "$OUT/$tag.txt"
  local files
  files=$(ls tests/test_*.py | sort)
  for f in $files; do
    echo "### $f" >&2
    raw="$OUT/.raw.txt"
    # 【关键】独立的 data_dir —— 隔离测试产物，不写线上 jobs.db
    ISOLATED_DATA="$(mktemp -d /tmp/h3suite-${tag}-XXXXXX)"
    AGENT_SESSION_DB="$ISOLATED_DATA/agent_sessions.db" \
    AGENT_MEMORY_DB="$ISOLATED_DATA/agent_memory.db" \
    H3STUDIO_DATA_DIR="$ISOLATED_DATA" \
    PYTHONPATH="$dir" \
    timeout 600 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no > "$raw" 2>&1 || true
    rm -rf "$ISOLATED_DATA"
    grep -E "^(FAILED|ERROR) " "$raw" | while IFS= read -r line; do
      case "$line" in
        FAILED*|ERROR*)
          rest="${line#* }"
          name="${rest%% *}"
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

echo "=== NEW failures (branch - baseline)  应为空 ===" >&2
comm -13 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/diff.txt"
echo "=== FIXED failures (baseline - branch) ===" >&2
comm -23 "$OUT/baseline.txt" "$OUT/branch.txt" | tee "$OUT/fixed.txt"
echo "=== done ===" >&2
