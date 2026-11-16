#!/usr/bin/env bash
# 第 4 轮代码审查收口 —— 上线到 8190（2026-11-16）
#
# ⚠️ 纪律（沿用 2026-11-15 的部署脚本）：
#   * 不碰主工作树 /root/projects/comfyui-minimax-h3-studio（他人 WIP）
#   * 改用 `git update-ref` 原子移动分支指针，不动任何 worktree 的文件
#   * live worktree 只精确 checkout 我改的文件（不动别人未提交内容）
#   * 只重启 h3-studio.service
#
# 流程：
#   0) 现状快照 + 建回滚点
#   1) 造合并提交（基于 local/deploy/qwen-ref-assets-live 最新 tip = 含 event-loop 修复）
#   2) 原子移动 local/maintainer 与 live 分支指针
#   3) live worktree 精确 checkout 改动的文件
#   4) 重启服务 + 探活
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
DEV=/root/projects/h3s-cr-20261115
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-20261115
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "local/maintainer = $MAINTAINER_BEFORE"
echo "live             = $LIVE_BEFORE"
echo "审查分支          = $(git -C "$DEV" rev-parse HEAD) ($BRANCH)"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

git branch "backup/live-before-cr4-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-cr4-${TS}" local/maintainer
echo "回滚点：backup/live-before-cr4-${TS} / backup/maintainer-before-cr4-${TS}"

# 合并前先确认审查分支相对 live 的差异只在预期文件
echo
echo "=== 1) 预演合并（不写）==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cr4_mergetree.txt
if grep -q '<<<<<<<' /tmp/cr4_mergetree.txt; then
  echo "!! 合并冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交（基于 live 最新 tip）==="
# 【为什么不用 git checkout -B】live worktree 里有大量未跟踪文件
# （audits/ evidence/ docs/ 等，都是别人的取证产物），
# `git checkout -B` 会尝试切分支并被未跟踪文件阻塞。
# 改用 `git commit-tree` 在**不碰任何 worktree** 的前提下造合并提交：
#   tree   = git merge-tree 的结果
#   parents = live tip + 审查分支
#   message = 合并说明
MERGE_TREE=$(cat /tmp/cr4_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-cr4): 第4轮代码审查收口 —— 切模式清槽位状态 + 重建如实回填 + 提交兜底 + 步数/尺寸双轨收敛

基线：live 3380f37（含 8190 event-loop 阻塞修复）
合并：local/feature/code-review-20261115（0de2afb + a03c248 + b7a4ec6）

本轮修复（全部有现场实测证据）：
- B-3【High】切模式不清 state.files / state.assetRefs（幽灵输入根因）
- B-4【Medium】#uploadRow 重建后槽位与界面脱节（按 state 如实回填）
- B-5【Medium】video-panel.js 裸引用 app.js 全局 state（跨文件隐式耦合）
- B-6【Medium】降级路径尺寸用写死常量（state.res/aspect 全仓零写入）
- B-7【Low】_debug 被生产代码当正常 API 用（currentSize 提正式 API）
- B-9【Low】过期注释描述已删除的 usePe 两态
- B-10【Medium】#genBtn handler 无顶层 try/catch（未捕获 rejection）
- B-11【Low】state.steps 与 videoState.steps 双轨（降级路径面板 50 / 提交 4）
- B-12【Medium】state.audioTrackSource 跨面板残留
- D-2 联动：app.js ?v= cr20261115d / video-panel.js ?v= ac1steps2

验证：新增 tests/test_slot_state_reset.py（6 用例）+ 变异 12/12 全捕获；
回归对跑：基线 15 failed / 1427 passed -> 本轮 14 failed / 1434 passed
（新增失败 0 条，额外修好 1 条）；浏览器 12 面板 console 报错 0。
MSG
)
echo "MERGE_COMMIT=$MERGE_COMMIT"
git log --oneline -1 "$MERGE_COMMIT"

echo
echo "=== 3) 原子移动分支指针 ==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
echo "local/maintainer -> $(git rev-parse local/maintainer)"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "live branch      -> $(git rev-parse local/deploy/qwen-ref-assets-live)"

echo
echo "=== 4) live worktree 精确 checkout ==="
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- \
  web/app.js web/video-panel.js web/index.html \
  tests/test_frontend_assets_hygiene.py tests/test_video_sampling_params.py \
  tests/test_slot_state_reset.py scripts/mutation_slot_state_reset.py
git -C "$LIVE" status --porcelain | head -20
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

echo
echo "=== 5) 重启服务 + 探活 ==="
systemctl restart h3-studio.service
for i in $(seq 1 60); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" || echo 000)
  if [ "$CODE" = "200" ]; then
    echo "启动完成（第 ${i} 秒），HTTP $CODE"
    break
  fi
done
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER"
systemctl is-active h3-studio.service

for p in / /api/jobs /api/options /api/queue /api/video-options /api/image-options; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "merge      = $MERGE_COMMIT"
echo "PID        = $PID_AFTER"
echo "回滚点     = backup/live-before-cr4-${TS}"
