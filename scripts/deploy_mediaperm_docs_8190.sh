#!/usr/bin/env bash
# 把「取证报告 + 复现脚本」这**纯文档**提交合入主线并部署到 8190。
#
# 【为什么单独一个脚本】代码修复已由 deploy_mediaperm_8190.sh 部署（7e35b4c）。
# 这一轮只补交**证据文档 + 复现脚本**（不碰任何运行时代码），
# 走同一套并发安全纪律：关系式判据 + 回滚点 + 完整性自检 + 精确 checkout。
#
# 【重要】本脚本**只** checkout 文档/脚本文件，绝不碰 server.py / h3studio/ / web/。
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/fix/mediaperm-perf
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "取证分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# ------------------------------------------------------------------
# 【并发安全】关系式判据（不写死 SHA）
# ------------------------------------------------------------------
if [ "$LIVE_BEFORE" != "$MAINTAINER_BEFORE" ]; then
  echo "!! live 与 maintainer 不一致（$LIVE_BEFORE vs $MAINTAINER_BEFORE），中止部署"
  exit 1
fi
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$BRANCH"; then
  echo "!! 取证分支没有包含当前 maintainer（$MAINTAINER_BEFORE），中止部署"
  exit 1
fi
echo "live == maintainer，且取证分支已包含最新 mainline"

LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动"

echo
echo "--- 本次改动将落到 live 的文件 ---"
git diff --name-only "$MAINTAINER_BEFORE" "$BRANCH" | sed 's/^/  /'
CHANGED=$(git diff --name-only "$MAINTAINER_BEFORE" "$BRANCH")
# 【安全闸】本脚本只允许文档 / 脚本 / 证据 —— 绝不能碰运行时代码。
BAD=$(echo "$CHANGED" | grep -E '^(server\.py|h3studio/|web/)' || true)
if [ -n "$BAD" ]; then
  echo "!! 本次改动包含运行时代码，本脚本不负责部署它："
  echo "$BAD"
  exit 1
fi
echo "安全闸通过：只含文档/脚本/证据，未触及运行时代码"

git branch "backup/live-before-perfdocs-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-perfdocs-${TS}" local/maintainer
echo "回滚点：backup/live-before-perfdocs-${TS} / backup/maintainer-before-perfdocs-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/perfdocs_mergetree.txt
if grep -q '<<<<<<<' /tmp/perfdocs_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/perfdocs_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
docs(mediaperm): 合入生成媒体库载入慢的实测取证报告 + 复现脚本

纯文档/脚本提交，不含任何运行时代码改动（代码修复在 7e35b4c 已上线）。

【报告】evidence/mediaperm-20261121/REPORT.md
【复现脚本】同目录 mediaperm_*.py（9 个，可直接重跑）
【并行取证】evidence/refctx-20261121/report.md（参考素材右键移除：线上可用）
            evidence/shots-20261121/visual-findings.md（96 张截图 + 58 页测量）
MSG
)
echo "合并提交 = $MERGE_COMMIT"

echo
echo "=== 3) 更新分支指针 ==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "maintainer = $(git rev-parse local/maintainer)"
echo "live       = $(git rev-parse local/deploy/qwen-ref-assets-live)"

echo
echo "=== 4) 精确 checkout 到 live（只 checkout 新增/修改，不删任何东西）==="
git diff --name-only -z --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 -r git checkout "$MERGE_COMMIT" --)
echo "已 checkout"

echo
echo "=== 5) 完整性自检 ==="
git diff --name-only -z --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/perfdocs_missing.txt
MISSING=$(cat /tmp/perfdocs_missing.txt)
if [ -n "$MISSING" ]; then
  echo "!! 以下文件部署后缺失："; echo "$MISSING"; exit 1
fi
DIFF=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$DIFF" ]; then
  echo "!! live 工作区与 HEAD 不一致："; echo "$DIFF"; exit 1
fi
echo "完整性自检通过"

echo
echo "=== 6) 重启服务（纯文档改动本不需要，但保持「上线即与 HEAD 一致」纪律）==="
systemctl restart h3-studio.service
sleep 6
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER（原 $PID_BEFORE）"

echo
echo "=== 7) 探活 ==="
for p in "/" "/api/jobs" "/api/agent/settings" "/api/system" \
         "/api/generation-types" "/api/jobs/favorites"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 修复仍在线自检 ==="
grep -c "_effect_lora_display_map" "$LIVE/h3studio/job_meta.py"
grep -c "asyncio.to_thread(job_meta.warm_effect_lora_display_map)" "$LIVE/server.py"
grep -c "_HISTORY_PRESENT_CACHE" "$LIVE/server.py"

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-perfdocs-${TS} / backup/maintainer-before-perfdocs-${TS}"
