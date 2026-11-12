#!/usr/bin/env bash
# 立即上线部署脚本：把合并结果落到 local/maintainer + 回灌 live worktree
#
# 用法: bash scripts/deploy_now.sh <merged-branch> <merged-sha>
# 纪律：
#   - 不 reset / 不 stash / 不 checkout -- . / 不 force push
#   - 不碰主工作树（/root/projects/comfyui-minimax-h3-studio）的他人 WIP
#   - 只操作 local/maintainer（经 /root/projects/h3studio-merge worktree）
#     与 live worktree（/root/projects/h3studio-live）
set -euo pipefail

MERGED_BRANCH="${1:?用法: deploy_now.sh <merged-branch> <merged-sha>}"
MERGED_SHA="${2:?用法: deploy_now.sh <merged-branch> <merged-sha>}"
REPO=/root/projects/comfyui-minimax-h3-studio
MERGE_WT=/root/projects/h3studio-merge
LIVE_WT=/root/projects/h3studio-live
TS=$(date +%Y%m%d-%H%M%S)

echo "=== 1/5 备份 local/maintainer ==="
BACKUP="backup/maintainer-before-立即上线-$TS"
git -C "$REPO" branch "$BACKUP" local/maintainer
echo "  备份分支: $BACKUP"

echo "=== 2/5 更新 local/maintainer 到 $MERGED_SHA ==="
# 用 ff-only 把 maintainer 快进到合并结果（合并结果是 maintainer 的后代）
git -C "$MERGE_WT" merge --ff-only "$MERGED_SHA"
echo "  maintainer 现在 = $(git -C "$MERGE_WT" rev-parse --short HEAD)"

echo "=== 3/5 检查 live worktree 干净 ==="
DIRTY=$(git -C "$LIVE_WT" status --porcelain | wc -l)
if [ "$DIRTY" -ne 0 ]; then
  echo "  !!! live worktree 有 $DIRTY 个未提交改动 —— 中止，不覆盖"
  git -C "$LIVE_WT" status --short
  exit 1
fi
echo "  live worktree 干净"

echo "=== 4/5 回灌 live worktree ==="
git -C "$LIVE_WT" merge --ff-only "$MERGED_SHA"
echo "  live 现在 = $(git -C "$LIVE_WT" rev-parse --short HEAD)  分支=$(git -C "$LIVE_WT" branch --show-current)"

echo "=== 5/5 重启 h3-studio.service ==="
systemctl restart h3-studio.service
sleep 8
systemctl is-active h3-studio.service
systemctl status h3-studio.service --no-pager -n 6

echo
echo "备份分支（回滚用）: $BACKUP"
echo "回滚步骤:"
echo "  git -C $MERGE_WT merge --ff-only $BACKUP   # 或 git -C $MERGE_WT reset --keep $BACKUP（需人工确认）"
echo "  git -C $LIVE_WT merge --ff-only $BACKUP"
echo "  systemctl restart h3-studio.service"
