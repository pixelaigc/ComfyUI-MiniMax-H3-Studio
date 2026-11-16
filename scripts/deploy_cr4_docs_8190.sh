#!/usr/bin/env bash
# 第 4 轮审查报告（docs）回灌 live —— 2026-11-16
#
# 与 deploy_cr4_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次新增的文件
#   * 只重启 h3-studio.service
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-20261115
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "live = $LIVE_BEFORE"
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

git branch "backup/live-before-cr4-docs-${TS}" local/deploy/qwen-ref-assets-live

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cr4_docs_mergetree.txt
if grep -q '<<<<<<<' /tmp/cr4_docs_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/cr4_docs_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-cr4-docs): 回灌第4轮审查报告与线上验证证据

基线：live 3f8c851（第4轮代码修复已上线）
合并：local/feature/code-review-20261115（18d2f02）

内容：
- docs/代码审查报告-20261116-第4轮.md
- docs/evidence-audit-cr4/（4 张线上截图 + 浏览器原始输出 + 变异输出 + 回归对比 diff）

纯文档，不改任何运行时代码。
MSG
)
echo "MERGE_COMMIT=$MERGE_COMMIT"

echo
echo "=== 3) 原子移动 live 指针 ==="
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "live branch -> $(git rev-parse local/deploy/qwen-ref-assets-live)"
# maintainer 也同步（保持两条线一致）
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$(git rev-parse local/maintainer)"

echo
echo "=== 4) live worktree 精确 checkout ==="
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- \
  docs/代码审查报告-20261116-第4轮.md \
  docs/evidence-audit-cr4
git -C "$LIVE" status --porcelain | grep -v '^??' | head -10
echo "(上面应为空 = 无被跟踪文件的改动)"
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

echo
echo "=== 5) 重启 + 探活 ==="
systemctl restart h3-studio.service
for i in $(seq 1 60); do
  sleep 1
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" || echo 000)
  if [ "$CODE" = "200" ]; then echo "启动完成（第 ${i} 秒）"; break; fi
done
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "PID: $PID_BEFORE -> $PID_AFTER"
systemctl is-active h3-studio.service
for p in / /api/jobs /api/queue /api/video-options /api/image-options; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "回滚点     = backup/live-before-cr4-docs-${TS}"
