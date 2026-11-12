#!/usr/bin/env bash
# 立即上线部署脚本：把合并结果落到 local/maintainer + 回灌 live worktree
#
# 用法: bash scripts/deploy_now.sh <merged-sha>
# 纪律：
#   - 不 reset / 不 stash / 不 checkout -- . / 不 force push
#   - 不碰主工作树（/root/projects/comfyui-minimax-h3-studio）的他人 WIP
#   - 只操作 local/maintainer（经 /root/projects/h3studio-merge worktree）
#     与 live worktree（/root/projects/h3studio-live）
#
# 拓扑说明（2026-11-12 实测）：
#   live 分支 local/deploy/qwen-ref-assets-live 上有一批**线上专属的
#   merge commit**（merge(deploy): 回灌 local/maintainer …），
#   maintainer 不是 live 的祖先，也不是 live 的后代 —— 两者分叉。
#   所以回灌 live 不能用 ff-only，必须用 `git merge --no-ff`，
#   把 maintainer 的新提交并进 live（live 的历史 merge commit 保留）。
set -euo pipefail

MERGED_SHA="${1:?用法: deploy_now.sh <merged-sha>}"
REPO=/root/projects/comfyui-minimax-h3-studio
MERGE_WT=/root/projects/h3studio-merge
LIVE_WT=/root/projects/h3studio-live
TS=$(date +%Y%m%d-%H%M%S)

echo "=== 1/6 备份 local/maintainer ==="
BACKUP="backup/maintainer-before-立即上线-$TS"
git -C "$REPO" branch "$BACKUP" local/maintainer
echo "  备份分支: $BACKUP"

echo "=== 2/6 更新 local/maintainer ==="
if git -C "$MERGE_WT" merge-base --is-ancestor local/maintainer "$MERGED_SHA" 2>/dev/null; then
  git -C "$MERGE_WT" merge --ff-only "$MERGED_SHA"
  echo "  ff 快进 -> $(git -C "$MERGE_WT" rev-parse --short HEAD)"
else
  git -C "$MERGE_WT" merge --no-ff -m "merge(deploy-now): 合入上线合并结果 $MERGED_SHA" "$MERGED_SHA"
  echo "  no-ff 合并 -> $(git -C "$MERGE_WT" rev-parse --short HEAD)"
fi

echo "=== 3/6 检查 live worktree 干净 ==="
DIRTY=$(git -C "$LIVE_WT" status --porcelain | wc -l)
if [ "$DIRTY" -ne 0 ]; then
  echo "  !!! live worktree 有 $DIRTY 个未提交改动 —— 中止，不覆盖"
  git -C "$LIVE_WT" status --short
  exit 1
fi
echo "  live worktree 干净 (HEAD=$(git -C "$LIVE_WT" rev-parse --short HEAD))"

echo "=== 4/6 回灌 live worktree（--no-ff，保留 live 的线上 merge 历史）==="
git -C "$LIVE_WT" merge --no-ff -m "merge(deploy-now): 回灌 local/maintainer —— 8 条已交付分支统一上线（视频类别标签/音视频上传/采样参数/面板兜底/模型清单/分辨率标签/会话状态/复读误判）" local/maintainer
echo "  live 现在 = $(git -C "$LIVE_WT" rev-parse --short HEAD)  分支=$(git -C "$LIVE_WT" branch --show-current)"

echo "=== 5/6 重启 h3-studio.service ==="
systemctl restart h3-studio.service
sleep 8
systemctl is-active h3-studio.service
systemctl status h3-studio.service --no-pager -n 6

echo "=== 6/6 探活 ==="
curl -s -o /dev/null -w '  GET / -> %{http_code} (%{time_total}s)\n' -m 20 http://127.0.0.1:8190/

echo
echo "备份分支（回滚用）: $BACKUP"
echo "回滚步骤:"
echo "  git -C $MERGE_WT merge --ff-only $BACKUP"
echo "  git -C $LIVE_WT merge --no-ff $BACKUP"
echo "  systemctl restart h3-studio.service"
