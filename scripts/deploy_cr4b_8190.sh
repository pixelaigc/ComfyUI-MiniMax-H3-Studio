#!/usr/bin/env bash
# 第 4 轮补漏部署（B-14 死代码删除 + 报告补充）—— 上线 8190（2026-11-16）
#
# 与 deploy_cr4_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-20261115
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "审查分支    = $(git -C "$DEV" rev-parse HEAD 2>/dev/null || git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

git branch "backup/live-before-cr4b-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-cr4b-${TS}" local/maintainer
echo "回滚点：backup/live-before-cr4b-${TS} / backup/maintainer-before-cr4b-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cr4b_mergetree.txt
if grep -q '<<<<<<<' /tmp/cr4b_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/cr4b_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-cr4b): 第4轮补漏 —— 删 B-14 死代码 bindSeg + 报告补复核 agent 的现场证据

基线：live 1b12368（第4轮代码修复 + 报告已上线）
合并：local/feature/code-review-20261115（fd0d34e）

- B-14【Low】web/app.js 的 bindSeg 零调用点，删除（复核 agent 现场核实：
  全仓仅 1 命中即定义行本身；对应 DOM resSeg/aspectChips/modelCards/loraCards
  全部 0 命中）。
- 报告补强：B-2 残余绕过（共享槽位 + 本地文件通道）线上复验证据、
  B-10 定性修正（非白屏而是静默无反应）、修复④ 运行时核验与运维风险。

验证：node --check 通过；针对性回归 5 failed / 123 passed
（5 条为基线已有的过期断言，本轮未新增失败）。
MSG
)
echo "MERGE_COMMIT=$MERGE_COMMIT"

echo
echo "=== 3) 原子移动分支指针 ==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "maintainer -> $(git rev-parse local/maintainer)"
echo "live       -> $(git rev-parse local/deploy/qwen-ref-assets-live)"

echo
echo "=== 4) live worktree 精确 checkout ==="
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- \
  web/app.js \
  docs/代码审查报告-20261116-第4轮.md
git -C "$LIVE" status --porcelain | grep -v '^??' | head -10
echo "(上面应为空 = 无被跟踪文件的改动)"
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

echo
echo "=== 5) 重启服务 + 探活 ==="
systemctl restart h3-studio.service
for i in $(seq 1 60); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" || echo 000)
  if [ "$CODE" = "200" ]; then echo "启动完成（第 ${i} 秒）"; break; fi
done
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "PID: $PID_BEFORE -> $PID_AFTER"
systemctl is-active h3-studio.service
for p in / /api/jobs /api/queue /api/video-options /api/image-options /api/generation-types; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "回滚点     = backup/live-before-cr4b-${TS}"
