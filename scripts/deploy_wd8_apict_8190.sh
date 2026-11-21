#!/usr/bin/env bash
# 看门狗第 8 轮 —— /api/jobs Content-Type 校验修复部署到 8190（2026-11-21）
#
# 【改动内容】
#   server.py                        —— api_create 校验 Content-Type（错请求头 4xx，不是 500）
#   tests/test_apicreate_content_type.py —— 9 例回归（真实 aiohttp 测试客户端）
#   scripts/mutation_check_apicreate_ct.py —— 变异测试 5/5 咬住
#
# 【与并发任务的关系 —— 不冲突】
# 并发中的「生成媒体库：提交不跳转标签 + 卡片收藏 + 移动端导航」任务改的是
# web/index.html + web/app.js（+ 它自己的 h3studio/loras.py / workflows.py）。
# 本次**只改 server.py + 新增 tests/ 与 scripts/ 文件**，与它**零文件冲突**。
# 本脚本带 web/ 安全闸：万一本次改动里混进 web/ 文件，直接拒绝部署。
#
# 【并发安全纪律（同 deploy_lorasperf_8190.sh）】
#   * 关系式判据（live == maintainer，且分支包含最新 maintainer）
#   * 回滚点（backup/live-before-* + backup/maintainer-before-*）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/fix/apicreate-content-type-20261121
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "修复分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# ------------------------------------------------------------------
# 【并发安全】关系式判据（不写死 SHA —— 别人推进了自动中止）
# ------------------------------------------------------------------
if [ "$LIVE_BEFORE" != "$MAINTAINER_BEFORE" ]; then
  echo "!! live 与 maintainer 不一致（$LIVE_BEFORE vs $MAINTAINER_BEFORE），中止部署"
  exit 1
fi
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$BRANCH"; then
  echo "!! 修复分支没有包含当前 maintainer（$MAINTAINER_BEFORE），中止部署"
  exit 1
fi
echo "live == maintainer，且修复分支已包含最新 mainline"

LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动"

echo
echo "--- 本次改动将落到 live 的文件 ---"
CHANGED=$(git diff --name-only "$MAINTAINER_BEFORE" "$BRANCH")
echo "$CHANGED" | sed 's/^/  /'

# 【安全闸】**绝不能碰 web/** —— 与并发中的那条任务零冲突。
BAD=$(echo "$CHANGED" | grep -E '^web/' || true)
if [ -n "$BAD" ]; then
  echo "!! 本次改动包含 web/ 文件，本脚本拒绝部署（会与并发任务冲突）："
  echo "$BAD"
  exit 1
fi
echo "安全闸通过：本次不碰 web/"

git branch "backup/live-before-wd8apict-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-wd8apict-${TS}" local/maintainer
echo "回滚点：backup/live-before-wd8apict-${TS} / backup/maintainer-before-wd8apict-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/wd8apict_mergetree.txt
if grep -q '<<<<<<<' /tmp/wd8apict_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/wd8apict_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(watchdog8-apict): /api/jobs 校验 Content-Type —— 错请求头不再回 500

看门狗第 8 轮巡检（2026-11-21）实测发现并修复的自包含缺陷。

api_create 直接 await request.multipart()，而 aiohttp 的 MultipartReader
要求请求头里有 Content-Type：无 Content-Type -> KeyError；非 multipart/* ->
AssertionError；multipart/form-data 无 boundary -> ValueError。三者都**抛
异常**而不是返回 4xx，aiohttp 默认处理把它变成 HTTP 500。

线上 24 小时日志里的 10 条异常（7 AssertionError + 2 boundary + 1 KeyError）
全部来自这一处。500 会把排查方向带去 ComfyUI/GPU/队列，而真实原因只是
「请求头没带对」—— 调用方自己能纠正的问题。

修法：在 multipart() 之前校验，缺头/错类型 -> 415，无 boundary -> 400。
测试：tests/test_apicreate_content_type.py 9 例（真实 aiohttp 客户端）；
变异测试 5/5 咬住。

不碰 web/ 与 h3studio/，与并发的「收藏/媒体库/移动端导航」任务零冲突。
MSG
)
echo "合并提交 = $MERGE_COMMIT"

echo
echo "=== 3) 推进 live + maintainer ==="
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" \
  "$LIVE_BEFORE"
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
echo "live       -> $(git rev-parse local/deploy/qwen-ref-assets-live)"
echo "maintainer -> $(git rev-parse local/maintainer)"

echo
echo "=== 4) 精确 checkout 到 live worktree ==="
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- $(echo "$CHANGED" | tr '\n' ' ')
echo "已 checkout:"
# ⚠️ `grep -v` 无匹配时返回 1 —— 在 `set -e` 下会**中止整个脚本**。
# 所以这里必须加 `|| true`。
git status --short | grep -v '^??' || true

echo
echo "=== 5) 重启服务 ==="
systemctl restart h3-studio.service
for i in $(seq 1 30); do
  if systemctl is-active --quiet h3-studio.service; then
    CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/" || true)
    if [ "$CODE" = "200" ]; then
      echo "服务就绪（探测 / = 200），等待 ${i}s"
      break
    fi
  fi
  sleep 1
done
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER"

echo
echo "=== 6) 部署后完整性自检 ==="
cd "$LIVE"
for f in server.py tests/test_apicreate_content_type.py scripts/mutation_check_apicreate_ct.py; do
  if git diff --quiet "$MERGE_COMMIT" -- "$f"; then
    echo "  OK  $f 与 HEAD 一致"
  else
    echo "  !!  $f 与 HEAD 不一致"; exit 1
  fi
done
if [ "$(git rev-parse HEAD)" != "$MERGE_COMMIT" ]; then
  echo "  !! live HEAD 不是合并提交"; exit 1
fi
echo "  OK  live HEAD = $MERGE_COMMIT"

echo
echo "=== 7) 探活 + 缺陷复验（修复后） ==="
for p in / /api/jobs /api/agent/settings /api/system /api/queue /api/generation-types; do
  curl -s -o /dev/null -w "  $p  code=%{http_code} time=%{time_total}s size=%{size_download}\n" \
    "http://127.0.0.1:${PORT}${p}"
done

echo
echo "  -- 修复前是 500 的 5 种错 Content-Type，现在应该是 4xx --"
printf "  no content-type      : "
curl -s -o /tmp/wd8_v1.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -d "mode=t2v" --max-time 20; echo "  $(head -c 160 /tmp/wd8_v1.txt)"
printf "  application/json     : "
curl -s -o /tmp/wd8_v2.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -H "Content-Type: application/json" -d '{"mode":"t2v"}' --max-time 20; echo "  $(head -c 160 /tmp/wd8_v2.txt)"
printf "  text/plain           : "
curl -s -o /tmp/wd8_v3.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -H "Content-Type: text/plain" -d "mode=t2v" --max-time 20; echo "  $(head -c 160 /tmp/wd8_v3.txt)"
printf "  form-urlencoded      : "
curl -s -o /tmp/wd8_v4.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -H "Content-Type: application/x-www-form-urlencoded" -d "mode=t2v" --max-time 20; echo "  $(head -c 160 /tmp/wd8_v4.txt)"
printf "  multipart no boundary: "
curl -s -o /tmp/wd8_v5.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -H "Content-Type: multipart/form-data" -d "mode=t2v" --max-time 20; echo "  $(head -c 160 /tmp/wd8_v5.txt)"
printf "  valid multipart      : "
curl -s -o /tmp/wd8_v6.txt -w "%{http_code}" -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -F "mode=t2v" -F "params={}" --max-time 20; echo "  $(head -c 160 /tmp/wd8_v6.txt)"

echo
echo "=== 8) 部署后日志（应无新增 Traceback） ==="
journalctl -u h3-studio.service --since "2 minutes ago" --no-pager 2>&1 \
  | grep -cE "Traceback|AssertionError|KeyError" || true

echo
echo "=== 部署完成 ==="
echo "合并提交 = $MERGE_COMMIT"
echo "回滚：git update-ref refs/heads/local/deploy/qwen-ref-assets-live $LIVE_BEFORE"
echo "        git update-ref refs/heads/local/maintainer $MAINTAINER_BEFORE"
