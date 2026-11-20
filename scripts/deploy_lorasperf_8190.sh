#!/usr/bin/env bash
# 看门狗第 7 轮 —— /api/image-loras 载荷/扫描性能修复部署到 8190（2026-11-21）
#
# 【改动内容】
#   h3studio/loras.py     —— metadata 超长字段截断 + 目录指纹扫描缓存
#   h3studio/workflows.py  —— _lookup_lora_meta 改走缓存入口（提交路径 2.2s -> 0.075s）
#   tests/test_loras_payload_perf.py —— 16 例回归（变异测试 5/5 咬住）
#   scripts/watchdog7_*.py —— 本轮 A–F 巡检取证脚本
#
# 【与并发任务的关系 —— 不冲突】
# 本次**不碰 web/**（不动 app.js / index.html / style.css / favorites.js /
# media_cleanup 相关逻辑），所以与「生成媒体库：提交不跳转标签 + 收藏 +
# 移动端导航修复」那条在跑任务**零文件冲突**。
#
# 【并发安全纪律（同 deploy_mediaperm_8190.sh）】
#   * 关系式判据（live == maintainer，且分支包含最新 maintainer）
#   * 回滚点（backup/live-before-* + backup/maintainer-before-*）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/fix/loras-payload-perf-20261121
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

git branch "backup/live-before-lorasperf-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-lorasperf-${TS}" local/maintainer
echo "回滚点：backup/live-before-lorasperf-${TS} / backup/maintainer-before-lorasperf-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/lorasperf_mergetree.txt
if grep -q '<<<<<<<' /tmp/lorasperf_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/lorasperf_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(watchdog7-perf): /api/image-loras 载荷 6.1MB -> 314KB，扫描 0.8s -> 0.014s

看门狗第 7 轮巡检（2026-11-21）实测发现并修复的自包含性能缺陷。

根因 1：/api/image-loras 响应体 6,149,820 字节 —— 93% 是一个 LoRA 的
  safetensors __metadata__["_"]（5,668,456 字节随机噪声）。该字段全仓
  零消费者。=> 按单字段上限截断，短字段原样保留，截断带可观测标记。

根因 2：每次请求都重新全盘扫 712 个 safetensors（0.7~1.0 秒）。
  => 目录指纹缓存（指纹成本 0.032 秒，变了自动作废）。

根因 3：workflows._lookup_lora_meta 在提交路径上每次 2.225 秒。
  => 改走缓存入口，并保留「效果类 + 加速类」并集（闸门不能失效）。

测试：tests/test_loras_payload_perf.py 16 例；变异测试 5/5 咬住。
不碰 web/，与并发的「生成媒体库/收藏/移动端导航」任务零冲突。
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
for f in h3studio/loras.py h3studio/workflows.py tests/test_loras_payload_perf.py; do
  if git diff --quiet "$MERGE_COMMIT" -- "$f"; then
    echo "  OK  $f 与 HEAD 一致"
  else
    echo "  !!  $f 与 HEAD 不一致"; exit 1
  fi
done
# live HEAD 必须等于合并提交
if [ "$(git rev-parse HEAD)" != "$MERGE_COMMIT" ]; then
  echo "  !! live HEAD 不是合并提交"; exit 1
fi
echo "  OK  live HEAD = $MERGE_COMMIT"

echo
echo "=== 7) 性能复验（修复后计时） ==="
for i in 1 2 3; do
  curl -s -o /dev/null -w "  /api/image-loras  try$i  code=%{http_code} time=%{time_total}s size=%{size_download}\n" \
    "http://127.0.0.1:${PORT}/api/image-loras"
done
for p in / /api/jobs /api/agent/settings; do
  curl -s -o /dev/null -w "  $p  code=%{http_code} time=%{time_total}s\n" "http://127.0.0.1:${PORT}${p}"
done

echo
echo "=== 部署完成 ==="
echo "合并提交 = $MERGE_COMMIT"
echo "回滚：git update-ref refs/heads/local/deploy/qwen-ref-assets-live $LIVE_BEFORE"
echo "        git update-ref refs/heads/local/maintainer $MAINTAINER_BEFORE"
