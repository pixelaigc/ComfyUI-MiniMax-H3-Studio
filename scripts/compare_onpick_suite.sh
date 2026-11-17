#!/usr/bin/env bash
# 本次修复的**回归失败集合对比**：分支 vs 基线（base = 0722404）。
#
# 【为什么不用一次全量跑】各 worktree 的浏览器测试会打同一个端口、
# 抢同一个浏览器 -> 并发跑必 flake。基线与分支必须**串行**跑。
#
# 【范围】只跑**依赖 web/app.js 或 web/index.html** 的测试文件 ——
# 本次改动只碰了这两个文件（+ 新增测试），其余文件与本次改动无关，
# 跑它们只会引入并发噪声。
#
# 【产出】
#   <outdir>/branch.txt   分支的失败用例集合（每行 "file::test"）
#   <outdir>/baseline.txt 基线的失败用例集合
#   <outdir>/diff.txt     差集（分支失败 - 基线失败）-> 必须为空
set -uo pipefail

BRANCH_DIR="${1:?用法: compare_onpick_suite.sh <branch_worktree> <baseline_worktree> <outdir>}"
BASE_DIR="${2:?}"
OUT="${3:?}"
PY=/usr/local/lib/hermes-agent/venv/bin/python

mkdir -p "$OUT"

# 依赖 app.js / index.html 的测试文件清单（两个 worktree 必须一致）
FILES=$(cd "$BRANCH_DIR" && grep -ln "app\.js\|index\.html" tests/test_*.py | sort)

run_tree() {
  local dir="$1" tag="$2"
  cd "$dir" || return 1
  : > "$OUT/$tag.txt"
  local f
  for f in $FILES; do
    echo "### $tag $f" >&2
    # 【不设 H3STUDIO_PORT / H3STUDIO_DATA_DIR】——
    # server.py 的 load_config() 里环境变量优先级**高于** config.json，
    # 而测试夹具 StudioServer.start() 用 config.json 写入自己随机取的
    # free_port()。一旦外部注入 H3STUDIO_PORT，server 绑到那个端口、
    # 夹具却仍在等随机端口 -> start() 恒 False -> 全部浏览器用例报
    # 「Studio 实例启动失败」（假失败）。数据目录同理。
    raw="$OUT/.raw.$tag.txt"
    # 【不要加 --timeout】本机没装 pytest-timeout，pytest 会直接
    # 报 "unrecognized arguments" 而**一个用例都不跑** —— 失败集合会是空的，
    # 差集对比就成了假绿（实测踩过）。
    timeout 300 "$PY" -m pytest "$f" -q --tb=no -p no:cacheprovider \
        > "$raw" 2>&1 || true
    # 从 summary 行抓 FAILED 用例
    grep -E "^FAILED " "$raw" | sed 's/^FAILED //' | sed 's/ .*$//' \
        >> "$OUT/$tag.txt"
  done
  sort -u -o "$OUT/$tag.txt" "$OUT/$tag.txt"
  rm -f "$OUT/.raw.$tag.txt"
}

echo "=== 分支跑 ==="
run_tree "$BRANCH_DIR" branch
echo "=== 基线跑 ==="
run_tree "$BASE_DIR" baseline

echo "=== 差集（分支失败 - 基线失败）==="
comm -23 "$OUT/branch.txt" "$OUT/baseline.txt" > "$OUT/diff.txt"
cat "$OUT/diff.txt"
echo "分支失败数: $(wc -l < "$OUT/branch.txt")"
echo "基线失败数: $(wc -l < "$OUT/baseline.txt")"
echo "新增失败数: $(wc -l < "$OUT/diff.txt")"

if [ -s "$OUT/diff.txt" ]; then
  echo "!!! 有新增失败 —— 必须修"
  exit 1
fi
echo "✅ 新增失败 0"
