#!/usr/bin/env bash
# 部署「窄屏矩阵 + 3 条新回归守卫 + 判据修正」到 8190（2026-11-21 第二轮）
#
# 与既有 deploy_*.sh 同纪律：
#   * 合并前重新核实 live 现状（并发安全）—— live 必须 == maintainer
#   * 我的分支必须包含最新 maintainer（否则先 rebase，绝不覆盖别人）
#   * 只精确 checkout「本次改动的文件」（本轮全是 tests/ + scripts/，不碰产品代码）
#   * 建回滚点分支
#   * 只重启 h3-studio.service
#   * 部署后完整性自检 + 探活 + 静态资源版本核对
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/favorites-v2-20261120
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
BRANCH_HEAD=$(git rev-parse "$BRANCH")
echo "  maintainer = $MAINTAINER_BEFORE"
echo "  live       = $LIVE_BEFORE"
echo "  本轮分支    = $BRANCH_HEAD"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "  重启前 PID=$PID_BEFORE"

# ---- 并发安全 1：live 必须仍等于 maintainer ----
if [ "$LIVE_BEFORE" != "$MAINTAINER_BEFORE" ]; then
  echo "!! live 与 maintainer 不一致（$LIVE_BEFORE vs $MAINTAINER_BEFORE），中止部署"
  exit 1
fi

# ---- 并发安全 2：本轮分支必须已包含最新 maintainer ----
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$BRANCH"; then
  echo "!! 本轮分支没有包含当前 maintainer（$MAINTAINER_BEFORE）"
  echo "   说明对方又推进了 mainline —— 先 rebase 再部署。中止"
  exit 1
fi
echo "  live == maintainer，且本轮分支已包含最新 mainline"

# ---- live 工作区必须无 tracked 改动 ----
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "  live 工作区：无 tracked 改动"

# ---- 回滚点 ----
git branch "backup/live-before-favleg-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-favleg-${TS}" local/maintainer
echo "  回滚点：backup/live-before-favleg-${TS}"
echo "          backup/maintainer-before-favleg-${TS}"

echo
echo "=== 1) 本次要带上去的文件 ==="
# 【坑】git 默认会把非 ASCII 路径转义成 \"docs/\\344...\" 的形式
#   ⇒ 前缀判断全部失配（会被误判成「产品代码」）。
#   修法：-c core.quotepath=false 让它输出原始 UTF-8 路径。
FILES=$(git -c core.quotepath=false diff --name-only local/maintainer "$BRANCH")
echo "$FILES" | sed 's/^/    /'
COUNT=$(echo "$FILES" | grep -c . || true)
echo "  共 $COUNT 个文件"

# ---- 本轮**不碰**产品代码：只允许 tests/ scripts/ docs/ evidence/ ----
# 【为什么放宽到这四类】本轮除测试与脚本外，还落档了取证（evidence/）
# 和交付报告（docs/）。**产品代码目录（server.py / h3studio/ / web/）
# 一律不许出现在待部署清单里** —— 那是硬红线。
BAD=$(echo "$FILES" | grep -vE '^(tests|scripts|docs|evidence)/' || true)
if [ -n "$BAD" ]; then
  echo "!! 本轮只应带 tests/ scripts/ docs/ evidence/，但包含产品代码："
  echo "$BAD"
  echo "   中止部署（避免误改产品代码）"
  exit 1
fi
echo "  纪律核对通过：不含任何产品代码（server.py / h3studio/ / web/ 均未出现在清单里）"

echo
echo "=== 2) 合并到 maintainer + live 分支 ==="
# 【坑】commit-tree 的第一个参数是 **tree**，不是 commit。
#   传 commit 会报 "不是有效的 'tree' 对象"。
TREE=$(git rev-parse "$BRANCH^{tree}")
echo "  tree = $TREE"
MERGE_COMMIT=$(git commit-tree "$TREE" -p "$MAINTAINER_BEFORE" \
  -m "merge(fav-leg): 窄屏矩阵「进入/退出收藏栏」+ 3 条新回归守卫 + 判据修正

合并来源分支：$BRANCH ($BRANCH_HEAD)

【本轮内容】全部是测试与脚本，**产品代码零改动**：
  · scripts/realscan_fav_leg_matrix.py —— 12 个注册模式 × 8 视口
    （375/390/768 竖屏 + 各自横屏 + 430 短高），完整用户路径含
    「进入收藏栏 → 退出回类型选择 → 再选一次类型」
  · tests/test_mobile_nav_regression_guards.py —— T9 三条新守卫
    · 收藏栏不是死路
    · 768 平板档收藏入口可达
    · 收藏视图点卡片开的是既有 #detailModal 且带真实参数
  · scripts/mutation_test_3guards.py —— 变异+恢复双相验证（4/4 咬人）
  · scripts/probe_fav_reachability.py —— 短视口可达性探针
  · scripts/evidence_build_id_live.py —— 线上构建版本标识取证

【判据修正 3 处】都是测试脚本自身的误判，不是产品缺陷：
  1) 平板档「回类型选择」要先 scrollIntoView（侧栏项在折叠线下方）
  2) 收藏视图终态 = 骨架卡消失 且 有真实卡片（切视图是异步重渲染，
     上一视图的卡片还在 DOM 里 ⇒ 固定 sleep / 等第一张卡都会误判）
  3) 收藏入口要「扫滚动位置找可命中坐标」再真点
     （scrollIntoView 把入口贴到上沿，短视口下正好在顶栏底下 ⇒ 点不到）

【已验证】矩阵 96/96 通过；变异测试 4/4 咬人、恢复后 4/4 重新变绿。" )
echo "  merge commit = $MERGE_COMMIT"

git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "  local/maintainer 与 live 分支已推进到 $MERGE_COMMIT"

echo
echo "=== 3) 在 live worktree 精确 checkout 本次文件 ==="
cd "$LIVE"
for f in $FILES; do
  git checkout "$MERGE_COMMIT" -- "$f"
  echo "    + $f"
done

echo
echo "=== 4) 完整性自检：live 工作区与 HEAD 一致 ==="
cd "$LIVE"
for f in $FILES; do
  if ! git diff --quiet "$MERGE_COMMIT" -- "$f"; then
    echo "!! 文件与 HEAD 不一致：$f"
    exit 1
  fi
done
echo "  $COUNT 个文件全部与 HEAD 一致"

echo
echo "=== 5) 重启服务 ==="
systemctl restart h3-studio.service
sleep 12
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "  重启后 PID=$PID_AFTER"
systemctl show h3-studio.service -p NRestarts -p ActiveEnterTimestamp --no-pager

echo
echo "=== 6) 探活 ==="
for p in "/" "/api/jobs?status=all&page=1&page_size=12" "/api/jobs/favorites" \
         "/api/generation-types" "/api/version" "/api/media/manage?scope=all&months=1"; do
  code=$(curl -s -o /dev/null -m 60 -w '%{http_code}' "http://127.0.0.1:${PORT}${p}")
  t=$(curl -s -o /dev/null -m 60 -w '%{time_total}' "http://127.0.0.1:${PORT}${p}")
  printf "  %-52s %s  %ss\n" "$p" "$code" "$t"
done

echo
echo "=== 7) /api/version 实取 ==="
curl -s -m 20 "http://127.0.0.1:${PORT}/api/version" | head -c 400; echo

echo
echo "=== 8) 静态资源 ?v= 版本核对 ==="
curl -s -m 20 "http://127.0.0.1:${PORT}/" | grep -oE '/static/[a-z0-9.-]+\?v=[a-z0-9]+' | sort -u | head -12

echo
echo "=== 9) 最终状态 ==="
echo "  live HEAD      = $(git rev-parse --short HEAD)"
echo "  local/maintainer = $(git rev-parse --short local/maintainer)"
echo "  一致? $([ "$(git rev-parse HEAD)" = "$(git rev-parse local/maintainer)" ] && echo YES || echo NO)"
echo "  service = $(systemctl is-active h3-studio.service)"
echo "  回滚：git -C $LIVE reset --hard backup/live-before-favleg-${TS}"
echo "         git branch -f local/maintainer backup/maintainer-before-favleg-${TS}"
echo "         git branch -f local/deploy/qwen-ref-assets-live backup/live-before-favleg-${TS}"
