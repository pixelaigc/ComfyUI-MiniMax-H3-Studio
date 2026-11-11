#!/usr/bin/env bash
# 逐文件跑 pytest，对比 baseline 与分支的失败名单（**并行**）。
#
# 为什么逐文件：整套跑一轮要 25+ 分钟（有几个测试文件本身要 20~30 秒），
# 而且会撞上 1500s 超时 —— 拿不到完整失败名单。
# 逐文件跑能拿到**逐条**失败项；并行跑把总耗时压到 ~5 分钟。
#
# 为什么并行安全：每个文件独立进程、独立 tmp 日志，互不干扰；
# baseline 与分支是**两个独立 worktree**，不会互相污染。
#
# 用法：bash scripts/compare_pytest_files.sh <baseline_dir> <branch_dir> <out_dir>
set -u
PY=/home/ubuntu/comfyui/venv/bin/python
BASE_DIR=$1
BRANCH_DIR=$2
OUT=$3
JOBS=${JOBS:-6}
mkdir -p "$OUT" "$OUT/logs"

# 收集测试文件列表
cd "$BASE_DIR" || exit 1
FILES=$(ls tests/test_*.py | sort)
cd "$BRANCH_DIR" || exit 1
FILES_B=$(ls tests/test_*.py | sort)

# 分支上**新增**的测试文件：只在分支跑，baseline 没有对应物。
# 这不是「列表不一致」的错误 —— 是本次改动新增的测试。
NEW_FILES=$(comm -13 <(echo "$FILES") <(echo "$FILES_B"))
ONLY_BASE=$(comm -23 <(echo "$FILES") <(echo "$FILES_B"))
if [ -n "$ONLY_BASE" ]; then
    echo "!! baseline 有分支没有的测试文件（分支可能漏了文件）："
    echo "$ONLY_BASE"
    exit 2
fi
if [ -n "$NEW_FILES" ]; then
    echo "== 分支新增测试文件（只跑分支）=="
    echo "$NEW_FILES"
fi
# 公共文件（两边都有）—— 这是回归对比的主体
FILES_COMMON=$(comm -12 <(echo "$FILES") <(echo "$FILES_B"))

run_file() {
    # $1=worktree $2=file $3=outfile
    local wt=$1 f=$2 outf=$3
    local name=$(basename "$f" .py)
    local tag=$(basename "$wt")
    cd "$wt" || return
    timeout 240 "$PY" -m pytest "$f" -q -p no:cacheprovider --tb=no -rf \
        > "$OUT/logs/$tag-$name.log" 2>&1
    local rc=$?
    local summary=$(grep -E "^[0-9]+ (passed|failed)|passed|failed" \
        "$OUT/logs/$tag-$name.log" | tail -1)
    if [ "$rc" -eq 124 ]; then
        echo "TIMEOUT $name :: (exceeded 240s)" >> "$outf"
    elif [ "$rc" -ne 0 ]; then
        echo "FAIL $name :: $summary" >> "$outf"
        grep "^FAILED " "$OUT/logs/$tag-$name.log" | sed "s|^|  $name :: |" >> "$outf"
    else
        echo "PASS $name :: $summary" >> "$outf"
    fi
}

export -f run_file
export PY OUT

echo "=== 并行跑 baseline 与分支（JOBS=$JOBS）==="
pids=()
# 公共文件：两边都跑（回归对比主体）
for f in $FILES_COMMON; do
    ( run_file "$BASE_DIR" "$f" "$OUT/base.txt" ) &
    pids+=($!)
    ( run_file "$BRANCH_DIR" "$f" "$OUT/branch.txt" ) &
    pids+=($!)
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 0.3; done
done
# 分支新增文件：只在分支跑（baseline 无对应物）
for f in $NEW_FILES; do
    ( run_file "$BRANCH_DIR" "$f" "$OUT/branch.txt" ) &
    pids+=($!)
    while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do sleep 0.3; done
done
wait

# 排序，便于 diff
sort -o "$OUT/base.txt" "$OUT/base.txt"
sort -o "$OUT/branch.txt" "$OUT/branch.txt"

echo "=== DIFF (branch vs baseline) ==="
diff "$OUT/base.txt" "$OUT/branch.txt" > "$OUT/diff.txt" 2>&1
if [ -s "$OUT/diff.txt" ]; then
    cat "$OUT/diff.txt"
else
    echo "(no difference)"
fi
echo "=== baseline 失败项: $(grep -c '^  ' "$OUT/base.txt" || true) ==="
echo "=== branch   失败项: $(grep -c '^  ' "$OUT/branch.txt" || true) ==="
echo "=== baseline TIMEOUT: $(grep -c '^TIMEOUT' "$OUT/base.txt" || true) ==="
echo "=== branch   TIMEOUT: $(grep -c '^TIMEOUT' "$OUT/branch.txt" || true) ==="
