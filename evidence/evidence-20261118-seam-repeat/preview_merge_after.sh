#!/usr/bin/env bash
# 预演 2：若把 local/maintainer 推进到 8d7d3fc（含我的改动），
# 对方随后在他们的 worktree 里提交（他们的 WIP 基于 d9e07ef）——
# 未来把他们的提交合回主线时会不会冲突？
#
# 关键：base 必须取 **推进后的 HEAD**（8d7d3fc），而不是 d9e07ef。
# 取错了会得出「无冲突」的假结论。
set -u
SEAM3=/root/projects/h3studio-cont-seg3
WIP=/root/projects/h3studio-cont-seam-hires
TMP=$(mktemp -d /root/projects/h3studio-cont-seg3/evidence/evidence-20261118-seam-repeat/preview2-XXXXXX)
cd "$SEAM3"

AFTER=8d7d3fc
echo "推进后的主线 = $(git rev-parse --short "$AFTER")"
echo

cd "$SEAM3"
git diff --name-only d9e07ef local/feature/cont-seg3 | sort > "$TMP/mine.txt"
cd "$WIP"
git diff --cached --name-only > "$TMP/wip_s.txt"
git diff --name-only > "$TMP/wip_u.txt"
cat "$TMP/wip_s.txt" "$TMP/wip_u.txt" | sort -u > "$TMP/wip.txt"
comm -12 "$TMP/mine.txt" "$TMP/wip.txt" > "$TMP/overlap.txt"

echo "════ 场景：主线 = $AFTER（已含我的改动），对方提交 their-commit（基于 d9e07ef）════"
echo "  base = $AFTER   ours = $AFTER   theirs = 对方 WIP"
echo
CONFLICT=0; CLEAN=0
while read -r f; do
    [ -z "$f" ] && continue
    cd "$SEAM3"
    FLAT=$(echo "$f" | tr "/" "__")
    git show "$AFTER:$f" > "$TMP/base_$FLAT" 2>/dev/null || continue
    cp "$TMP/base_$FLAT" "$TMP/ours_$FLAT"
    if [ -f "$WIP/$f" ]; then
        # 对方的「工作树最终形态」= 他们在 $AFTER 基础上的改动
        # （他们的工作树基于 d9e07ef，但内容层面我们比的是最终形态）
        cp "$WIP/$f" "$TMP/theirs_$FLAT"
    else
        : > "$TMP/theirs_$FLAT"
    fi
    if git merge-file -p --diff3 "$TMP/ours_$FLAT" "$TMP/base_$FLAT" "$TMP/theirs_$FLAT" \
        > "$TMP/merged_$FLAT" 2>/dev/null; then
        echo "  ✅ $f —— 未来合回主线无冲突"
        CLEAN=$((CLEAN+1))
    else
        echo "  ⚠ $f —— **未来合回主线会冲突**"
        CONFLICT=$((CONFLICT+1))
    fi
done < "$TMP/overlap.txt"

echo
echo "  无冲突 $CLEAN / 有冲突 $CONFLICT"
echo "  预演目录: $TMP"
exit 0
