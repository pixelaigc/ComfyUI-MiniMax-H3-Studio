#!/usr/bin/env bash
# 部署「生成媒体库收藏」（提交不跳标签 / 右键收藏 / 左侧收藏栏 /
# 清理跳过收藏 / 收藏管理）到 8190（2026-11-20）
#
# 与既有 deploy_*.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout「本次改动的文件」（**不 checkout 删除类**）
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/favorites-20261120
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "收藏分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# ------------------------------------------------------------------
# 【并发安全】live 必须仍停在本轮开始时看到的状态。
#
# 【为什么不能像别的脚本那样写死一个 SHA】本轮有**另一条任务**在跑
# （内置 Agent 能力真实化 + flf/r2v 面板默认参数），它会推进
# local/maintainer 与 live。如果这里写死 SHA，对方一提交就中止部署。
#
# 这里的判据是「**关系**」而不是「具体 SHA」：
#   * live 必须仍等于 maintainer（两边同步推进，没有分叉）；
#   * 收藏分支必须以当前 maintainer 为基线（说明它已经包含了对方的
#     最新提交 —— 少了这一步就会把对方的改动覆盖掉）。
# 任何一个不成立就中止 —— 宁可不合，也不要覆盖别人的工作。
# ------------------------------------------------------------------
if [ "$LIVE_BEFORE" != "$MAINTAINER_BEFORE" ]; then
  echo "!! live 与 maintainer 不一致（$LIVE_BEFORE vs $MAINTAINER_BEFORE），中止部署"
  exit 1
fi
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$BRANCH"; then
  echo "!! 收藏分支没有包含当前 maintainer（$MAINTAINER_BEFORE），"
  echo "   说明对方又推进了 mainline —— 先 rebase 收藏分支再部署。中止"
  exit 1
fi
echo "live == maintainer，且收藏分支已包含最新 mainline"

# 【live 工作区干净性】只允许**未跟踪**文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

git branch "backup/live-before-fav-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-fav-${TS}" local/maintainer
echo "回滚点：backup/live-before-fav-${TS} / backup/maintainer-before-fav-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/fav_mergetree.txt
if grep -q '<<<<<<<' /tmp/fav_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/fav_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
feat(favorites): 生成媒体库「收藏」—— 提交不跳标签 / 右键收藏 / 左侧收藏栏 / 清理跳过收藏 / 收藏管理

【需求 1｜提交后保持当前标签】
  根因：web/app.js 两处 state.tab = "active"（音乐分支 + 通用分支）在提交
  成功后硬把标签切到「进行中」，把用户当前的筛选悄悄改掉。
  改法：去掉这两处赋值。保持用户当前所选标签与筛选（全部 / 进行中 /
  已完成 / 生成类型标签 / 收藏）不变；新任务在它所属标签下照常出现，
  轮询与状态一致性校验、运行中卡片判定均不受影响。

【需求 2｜卡片右键收藏】
  媒体库右键菜单新增「添加到收藏 / 取消收藏」（已收藏时文案反转）；
  卡片顶栏显示 ★ 标识。收藏状态写服务端 job_favorites 表（真持久化）。

【需求 3｜左侧「收藏」栏】
  左侧「素材」分组内新增「收藏」入口，与「素材库」同一套导航与视觉约定
  （.nav-item + 同风格 SVG + 同一明暗主题）。
  【为什么不新开分组】tests/test_sidebar_fold.py 断言顶层分组**恰好 7 个**
  （video/image/image-edit/music/assets/agent/system），新开分组会打破契约。
  【最终落点】进入的是生成媒体库的「收藏」标签（state.tab="favorites"），
  而不是另开一个页面 —— 需求要求「沿用生成媒体库既有的卡片交互（查看、
  打开详情模态框、右键、筛选/搜索、分页），并保留类型区分」，复用同一个
  标签 = 这些交互全是**同一份**代码，类型区分天然保留，不会出现两套实现漂移。

【需求 4｜清理跳过收藏】
  h3studio/media_cleanup.py 的 preview() 与 cleanup() 共用同一个
  「已收藏则跳过」判据（_favorited_ids）：已收藏的卡片不清记录、
  不删产物文件、不影响文件索引。三个清理口径（全部 / 按月数 / 失败取消）
  全部生效。取消收藏后该卡片重新纳入清理范围。

【需求 5｜收藏管理】
  收藏视图内批量勾选 + 批量取消收藏（服务端单次批量接口，逐个 id 回报结果）。
  「取消收藏」与「删除产物」是两个**独立按钮 / 独立接口 / 不同二次确认
  文案**：取消收藏只解除收藏标记（产物文件保留），删除产物沿用既有删除语义。

【数据库】
  新建 job_favorites 表（job_id TEXT PRIMARY KEY + created_at REAL）。
  只加表、不动任何既有记录 —— 老库不需要迁移，向后兼容。
  删除任务时顺带清掉它的收藏标记（防收藏视图出现死条目）。

【修掉的两个真实缺陷（浏览器实测暴露）】
  1. 骨架卡也渲染了收藏选择框（id 是假的 __skeleton__N）——
     「全选本页」会把假 id 一起选中，批量请求带上不存在的 id。
  2. GET /api/jobs/{id} 没带 favorited 字段（只有列表接口带），
     同一个任务在两个接口里的收藏状态不一致。

【测试】tests/test_favorites.py：16 例（10 后端单测 + 6 真实 Chromium
端到端）全部通过。覆盖持久化 / 幂等 / 404 / 批量 / 三个清理口径跳过收藏 /
提交不跳标签 / 右键收藏与 ★ 标识 / 左侧收藏栏 / 取消收藏 / 批量取消。
MSG
)
echo "合并提交 = $MERGE_COMMIT"

echo
echo "=== 3) 更新分支指针（不切分支、不碰 live 未跟踪文件）==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
echo "maintainer = $(git rev-parse local/maintainer)"
echo "live       = $(git rev-parse local/deploy/qwen-ref-assets-live)"

echo
echo "=== 4) 精确 checkout 本次改动的文件到 live ==="
# 【只 checkout「新增/修改」的文件 —— 绝不 checkout「删除」的文件】
# `git checkout <commit> -- <path>` 对一个在 <commit> 里**不存在**的路径
# 会**真的删掉** live 工作区里的那个文件。
CHANGED_ALL=$(git diff --name-only "$LIVE_BEFORE" "$MERGE_COMMIT")
echo "$CHANGED_ALL" | sed 's/^/  /'
echo "共 $(echo "$CHANGED_ALL" | wc -l) 个差异文件"

CHANGED_KEEP=$(git diff --name-only --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT")
DELETED=$(git diff --name-only --diff-filter=D "$LIVE_BEFORE" "$MERGE_COMMIT")
if [ -n "$DELETED" ]; then
  echo
  echo "--- 以下路径在合并提交里已删除：**不 checkout**（避免误删 live 文件）--"
  echo "$DELETED" | sed 's/^/  SKIP-DEL: /'
fi

echo
echo "--- 实际 checkout 的文件（新增/修改）---"
echo "$CHANGED_KEEP" | sed 's/^/  /'
echo "共 $(echo "$CHANGED_KEEP" | wc -l) 个文件"
# 【中文文件名必须用 NUL 分隔传给 git checkout】
git diff --name-only -z --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 -r git checkout "$MERGE_COMMIT" --)
echo

echo "=== 5) 部署后完整性自检 ==="
git diff --name-only -z --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/fav_missing.txt
MISSING=$(cat /tmp/fav_missing.txt)
if [ -n "$MISSING" ]; then
  echo "!! 以下文件部署后缺失："; echo "$MISSING"; exit 1
fi
DIFF=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$DIFF" ]; then
  echo "!! live 工作区与 HEAD 不一致："; echo "$DIFF"; exit 1
fi
echo "完整性自检通过（文件全部在位 + 工作区与 HEAD 一致）"

echo
echo "=== 6) 重启服务 ==="
systemctl restart h3-studio.service
sleep 6
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER（原 $PID_BEFORE）"

echo
echo "=== 7) 探活 ==="
for p in "/" "/api/jobs" "/api/jobs/favorites" "/api/image-options" \
         "/api/generation-types" "/api/media/manage?scope=all&months=1"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 收藏功能线上接口复核 ==="
echo "--- 收藏表已建（只加表，不动既有记录）---"
/home/ubuntu/comfyui/venv/bin/python - <<'PY'
import json, sqlite3, urllib.request
url = "http://127.0.0.1:8190/api/jobs/favorites"
with urllib.request.urlopen(url, timeout=10) as r:
    print("  GET /api/jobs/favorites ->", json.loads(r.read().decode()))
PY
echo "--- 静态资源版本号已升（收藏模块要能被浏览器拿到）---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(favorites|app|media-manage)\.(js|css)\?v=[^"]+' | sort -u

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-fav-${TS} / backup/maintainer-before-fav-${TS}"
