#!/usr/bin/env bash
# 素材库强制引用 + 种子正整数校验 —— 部署到 8190（2026-11-16）
#
# 沿用 deploy_agent_audit_8190.sh 的全部纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 【加固 1】checkout 清单里包含脚本自己
#   * 【加固 2】部署后做完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
#   * 【加固 3】set -e 下 grep 无匹配会静默中断 —— 一律 `|| true` 兜住
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/assetlib-seed-guard
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

# ---- 本轮改动的文件清单 ----
# 【注意】必须包含脚本自己（见文件头「加固 1」）。
DEPLOY_FILES=(
  server.py
  h3studio/agent.py
  h3studio/music_yue2.py
  h3studio/asset_ref_guard.py
  h3studio/seed_guard.py
  web/app.js
  web/index.html
  web/music.js
  web/video-panel.js
  web/seed-guard.js
  scripts/templates/flow-create.mmd
  scripts/deploy_assetlib_seed_8190.sh
  docs/交互流程图-创作链路.mmd
  docs/交互流程图-创作链路.svg
  docs/交互流程图-创作链路.png
  docs/优化-素材库强制引用与种子正整数校验-20261116.md
  tests/assetlib_fixture.py
  tests/test_assetlib_and_seed_guard.py
  tests/test_b1b2_video_modes.py
  tests/test_flf_reference_media.py
  tests/test_h3_video_modes_ui.py
  tests/test_slot_state_reset.py
  tests/test_video_sampling_params.py
)

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "本轮分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# live worktree 的 tracked 必须干净 —— 不干净就说明有人在改，先停手
DIRTY=$(git -C "$LIVE" status --porcelain | grep -v '^??' || true)
if [ -n "$DIRTY" ]; then
  echo "!! live worktree 有 tracked 改动，中止部署："
  echo "$DIRTY"
  exit 1
fi
echo "live worktree tracked 干净"

git branch "backup/live-before-assetlibseed-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-assetlibseed-${TS}" local/maintainer
echo "回滚点：backup/live-before-assetlibseed-${TS} / backup/maintainer-before-assetlibseed-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/assetlib_seed_mergetree.txt
if grep -q '<<<<<<<' /tmp/assetlib_seed_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/assetlib_seed_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(素材库+种子): 媒体引用强制走素材库 + 种子只允许正整数

【目标 1：所有媒体引用必须先进入素材库】
外部素材必须先进入素材库，界面只能从素材库挑选并引用，
不允许功能直接引用外部文件路径（与「图片编辑」保持一致）。
- 前端：槽位不再有 <input type="file">，点空槽位直接开素材库选择器；
  提交体不再 append file_*；拖放只收素材库引用；state.files /
  slotPreviewUrls 整条本地直传链路移除。
- 后端：flf / r2v 拒绝任何 multipart 直传媒体，给中文原因；
  素材库引用的 asset_id 必须真的在库、未删除、类型与槽位匹配，
  否则报「该媒体尚未入库素材库，请先上传到素材库后再引用」。
- 参考素材按 slot 分组的上限校验（≤9 / ≤3 / ≤3）移到素材库引用这一侧，
  保住「不得静默截断」的既有契约。

【目标 2：种子仅允许正整数】
- 后端：prepare_job_params（面板 / Agent / 重试三条路径唯一公共点）
  统一校验 seed，非法时给可读中文原因，不再落到
  「生成失败（服务端未能给出具体原因）」的兜底文案。
  Agent create_job 同步改用同一口径（原 int() 会放过 0 / 负数 / 小数）。
- 前端：提交前先校验 #seed 与 #musicSeed，非法即时报中文提示
  并**不发起请求**；合法值归一成数字再提交。

产品行为无削弱：无 skip / xfail / 放宽断言。
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
echo "=== 4) live worktree 精确 checkout（只部署本轮改的文件）==="
git -C "$LIVE" checkout "$MERGE_COMMIT" -- "${DEPLOY_FILES[@]}"
CHANGED=$(git -C "$LIVE" status --porcelain | grep -v '^??' || true)
if [ -n "$CHANGED" ]; then
  echo "$CHANGED" | head -40
  echo "(上面应只列出本轮改的文件 = 部署成功)"
else
  echo "(无 tracked 改动 —— 文件内容与 HEAD 已一致)"
fi
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

# 【加固 2】部署完整性自检
echo
echo "=== 部署完整性自检 ==="
MISSING=0
for f in "${DEPLOY_FILES[@]}"; do
  if [ ! -f "$LIVE/$f" ]; then
    echo "  缺失: $f"
    MISSING=$((MISSING + 1))
  fi
done
if [ "$MISSING" -eq 0 ]; then
  echo "  本轮 ${#DEPLOY_FILES[@]} 个文件全部就位"
else
  echo "  !! 缺失 $MISSING 个文件 —— 需要补 checkout"
  exit 1
fi
if [ -n "$(git -C "$LIVE" diff --name-only HEAD || true)" ]; then
  echo "  !! live 工作区与 HEAD 不一致："
  git -C "$LIVE" diff --name-only HEAD
  exit 1
fi
echo "  live 工作区与 HEAD 完全一致"

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
for p in / /api/jobs /api/queue /api/video-options /api/image-options /api/generation-types \
         /api/assets /api/assets/picker; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done
echo "--- 静态资源 ---"
for f in app.js index.html music.js video-panel.js seed-guard.js; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/static/${f}" || echo 000)
  echo "  ${f} -> ${CODE}"
done

echo
echo "=== 6) 线上功能探针（真实请求）==="
echo "--- 非法种子必须被拒（400 + 中文原因）---"
SEED_PROBE=$(curl -s -o /tmp/seed_probe.json -w "%{http_code}" \
  -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -F "mode=t2v" \
  -F 'params={"prompt":"x","width":864,"height":480,"duration":5,"seed":"abc"}' || echo 000)
echo "  POST /api/jobs (seed=abc) -> ${SEED_PROBE}"
cat /tmp/seed_probe.json 2>/dev/null | head -c 400; echo
echo "--- 外部媒体直传必须被拒（400 + 中文原因）---"
MEDIA_PROBE=$(curl -s -o /tmp/media_probe.json -w "%{http_code}" \
  -X POST "http://127.0.0.1:${PORT}/api/jobs" \
  -F "mode=flf" \
  -F 'params={"prompt":"x","width":864,"height":480,"duration":5}' \
  -F "file_last=@/root/projects/h3studio-live/web/favicon.svg" || echo 000)
echo "  POST /api/jobs (file_last=外部文件) -> ${MEDIA_PROBE}"
cat /tmp/media_probe.json 2>/dev/null | head -c 400; echo

echo
echo "=== 7) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "live HEAD  = $(git -C "$LIVE" rev-parse HEAD)"
echo "回滚点     = backup/live-before-assetlibseed-${TS}"
