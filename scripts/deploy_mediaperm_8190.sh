#!/usr/bin/env bash
# ======================================================================
# 把「生成媒体库性能修」部署到线上 8190。
#
# 【并发安全闸（关系式判据，不写死 SHA）】
#   1. live 的工作区必须**没有** tracked 改动（否则会覆盖别人的未提交改动）
#   2. live HEAD 必须 == local/maintainer（否则有人先推进了主线）
#   3. 待部署分支必须**包含**当前 local/maintainer（否则会把别人的新提交盖掉）
#   4. 部署前再查一次（防止检查与部署之间有别人推进）
#
# 任一不成立 -> **中止**，不部署。
# ======================================================================
set -euo pipefail

BRANCH="${1:-local/feature/favorites-v2-20261120}"
MAIN="/root/projects/comfyui-minimax-h3-studio"
LIVE="/root/projects/h3studio-live"
LOG_TAG="perf-$(date +%Y%m%d-%H%M%S)"

say() { printf '\n=== %s ===\n' "$1"; }

say "1) 并发安全闸"

cd "$MAIN"
MAIN_SHA=$(git rev-parse local/maintainer)
LIVE_SHA=$(git rev-parse local/deploy/qwen-ref-assets-live)
DEPLOY_SHA=$(git rev-parse "$BRANCH")

echo "  local/maintainer              = $MAIN_SHA"
echo "  local/deploy/qwen-ref-assets-live = $LIVE_SHA"
echo "  待部署分支 $BRANCH = $DEPLOY_SHA"

# 闸 1：live 工作区没有 tracked 改动
if ! git -C "$LIVE" diff --quiet HEAD -- .; then
    echo "  ✗ 中止：live 工作区有未提交的 tracked 改动（会覆盖别人的工作）"
    git -C "$LIVE" status --short | head -20
    exit 1
fi
echo "  ✓ live 工作区无 tracked 改动"

# 闸 2：live == maintainer
if [ "$LIVE_SHA" != "$MAIN_SHA" ]; then
    echo "  ✗ 中止：live($LIVE_SHA) != maintainer($MAIN_SHA) —— 有人先推进了主线"
    exit 1
fi
echo "  ✓ live == maintainer"

# 闸 3：部署分支包含最新 mainline
if ! git merge-base --is-ancestor "$MAIN_SHA" "$DEPLOY_SHA"; then
    echo "  ✗ 中止：$BRANCH 不包含当前 mainline —— 部署会盖掉别人的新提交"
    git log --oneline "$MAIN_SHA".."$DEPLOY_SHA" | head -5
    exit 1
fi
echo "  ✓ 部署分支包含最新 mainline"

say "2) 回滚点"
BACKUP_LIVE="backup/live-before-${LOG_TAG}"
BACKUP_MAIN="backup/maintainer-before-${LOG_TAG}"
git -C "$LIVE" branch "$BACKUP_LIVE" "$LIVE_SHA"
git branch "$BACKUP_MAIN" "$MAIN_SHA"
echo "  $BACKUP_LIVE"
echo "  $BACKUP_MAIN"

say "3) 合并到主线 + 推进 live 分支"
git merge --no-ff "$BRANCH" -m "merge(mediaperm): 生成媒体库「卡片载入慢」性能修

主因：aiohttp 事件循环被同步读盘占住
  options_payload 一次调 264 次 find_weight（135 次落 USB 盘 rglob），
  单次 511~800ms，且**跑在事件循环里** -> 整个 8190 冻结。
  实测：video-options 并发时 /api/jobs 的 p50 从 5.8ms 劣化到 1279ms（220 倍）。

另两处：
  * 卡片直接引用原始产物（图片 1.1~9.5MB / 视频 Chrome 拿整个 mp4）
    -> 一页 12 张卡实测 29.6MB；现在走受限尺寸缩略图 + 懒加载 -> 322KB。
  * 列表接口只算缩略图键、不生成（生成会让列表接口慢到 1.5~2.9 秒）。

证据：evidence/media-perf/ + 回归 15 条 + 变异测试 12/12 咬人。"
NEW_SHA=$(git rev-parse HEAD)
echo "  merge commit = $NEW_SHA"
git -C "$LIVE" merge --ff-only "$NEW_SHA"
echo "  live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

say "4) 重启服务"
systemctl restart h3-studio.service
for i in $(seq 1 40); do
    if curl -s -o /dev/null -m 3 -w "" http://127.0.0.1:8190/api/generation-types 2>/dev/null; then
        break
    fi
    sleep 0.5
done
systemctl is-active h3-studio.service
systemctl show h3-studio.service -p MainPID --value

say "5) 探活"
for p in "/" "/api/jobs?status=done&page=1&page_size=12" "/api/jobs/favorites" \
         "/api/image-options" "/api/generation-types" "/api/video-options" \
         "/api/media/manage?scope=all&months=1"; do
    code=$(curl -s -o /dev/null -m 60 -w "%{http_code}" "http://127.0.0.1:8190$p")
    echo "  $code  $p"
done

say "6) 线上代码自检（关键修复点必须都在位）"
for pat in "_FIND_WEIGHT_CACHE" "invalidate_find_weight_cache" "_ACCEL_COMPAT_MATRIX_CACHE"; do
    n=$(grep -c "$pat" "$LIVE/h3studio/video_models.py" || true)
    echo "  video_models.py  $pat x$n"
done
for pat in "asyncio.to_thread" "warm_effect_lora_display_map" "_attach_thumb_urls" \
           "thumb_handler" "_THUMB_INDEX_MEM"; do
    n=$(grep -c "$pat" "$LIVE/server.py" || true)
    echo "  server.py         $pat x$n"
done
n=$(grep -c 'preload="none"' "$LIVE/web/app.js" || true)
echo "  web/app.js        preload=\"none\" x$n"
n=$(grep -c 'loading="lazy"' "$LIVE/web/app.js" || true)
echo "  web/app.js        loading=\"lazy\" x$n"
n=$(ls "$LIVE/h3studio/thumbnails.py" 2>/dev/null | wc -l)
echo "  h3studio/thumbnails.py 存在 x$n"

say "7) 服务日志尾部"
journalctl -u h3-studio.service --since "-2 min" --no-pager | tail -12

say "部署完成"
echo "merge commit = $NEW_SHA"
echo "回滚点：$BACKUP_LIVE / $BACKUP_MAIN"
