#!/usr/bin/env bash
# 只读预演：我的改动与「对方 worktree 的未提交 WIP」是否真冲突。
#
# 【为什么需要】
# git 的 merge-tree 只能比较**已提交**的内容，看不见对方未提交的 WIP。
# 而对方 worktree 正有 61 条暂存 + 30 条未暂存，其中包含
# web/app.js / web/index.html / web/video-panel.js —— 与我本轮改的
# 3 个文件**同名**。
#
# 【做法】纯只读：
#   1. 把对方的 WIP 内容导出到临时目录（只读复制，不动对方 worktree）
#   2. 对每个重叠文件做三方合并预演：
#      base   = merge-base(我的分支, local/maintainer)
#      ours   = 我的分支的版本
#      theirs= 对方 WIP 的版本
#      用 git merge-file 预演，看是否冲突
#
# 【输出】退出码 + 受影响文件清单（供如实回报阻塞点）
set -u

SEAM3=/root/projects/h3studio-cont-seg3
WIP=/root/projects/h3studio-cont-seam-hires
TMP=$(mktemp -d /root/projects/h3studio-cont-seg3/evidence/evidence-20261118-seam-repeat/preview-XXXXXX)
cd "$SEAM3"

BASE=$(git merge-base local/feature/cont-seg3 local/maintainer)
echo "merge-base = $(git rev-parse --short "$BASE")"
echo

# 本轮改动涉及的文件
git diff --name-only "$BASE" local/feature/cont-seg3 > "$TMP/mine.txt"

# 对方 WIP 涉及的文件（只读查询）
cd "$WIP"
git diff --cached --name-only > "$TMP/wip_staged.txt"
git diff --name-only > "$TMP/wip_unstaged.txt"
cat "$TMP/wip_staged.txt" "$TMP/wip_unstaged.txt" | sort -u > "$TMP/wip.txt"

echo "════ 重叠文件（我改的 ∩ 对方 WIP 改的）════"
comm -12 <(sort -u "$TMP/mine.txt") "$TMP/wip.txt" | tee "$TMP/overlap.txt"
echo "  重叠数: $(wc -l < "$TMP/overlap.txt")"
echo

echo "════ 逐文件三方合并预演 ════"
CONFLICT=0
CLEAN=0
while read -r f; do
    [ -z "$f" ] && continue
    cd "$SEAM3"
    FLAT=$(echo "$f" | tr "/" "__")
    git show "$BASE:$f" > "$TMP/base_$FLAT" 2>/dev/null || { echo "  $f: base 不存在（新增文件）"; continue; }
    git show "local/feature/cont-seg3:$f" > "$TMP/ours_$FLAT" 2>/dev/null || continue
    # 对方的 WIP 内容：优先用工作树（工作树含 staged+unstaged 的最终形态）
    if [ -f "$WIP/$f" ]; then
        cp "$WIP/$f" "$TMP/theirs_$FLAT"
    else
        # 对方把它删了 -> 用 /dev/null（= 删除）
        : > "$TMP/theirs_$FLAT"
    fi
    if git merge-file -p --diff3 "$TMP/ours_$FLAT" "$TMP/base_$FLAT" "$TMP/theirs_$FLAT" \
        > "$TMP/merged_$FLAT" 2>/dev/null; then
        echo "  ✅ $f —— 无冲突"
        CLEAN=$((CLEAN+1))
    else
        echo "  ⚠ $f —— **有冲突**（需人工合并）"
        CONFLICT=$((CONFLICT+1))
    fi
done < "$TMP/overlap.txt"

echo
echo "════ 结论 ════"
echo "  无冲突: $CLEAN    有冲突: $CONFLICT"
echo "  预演目录: $TMP"
if [ "$CONFLICT" -gt 0 ]; then
    echo "  ⇒ 合并**内容层面**会冲突（需人工合并）"
else
    echo "  ⇒ 合并**内容层面**无冲突（阻塞纯粹来自 git 对脏 index 的保护）"
fi
exit 0
