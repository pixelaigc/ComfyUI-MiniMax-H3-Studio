#!/usr/bin/env bash
# 媒体库卡片 LoRA 真值 + 详情模态框 —— 上线到 8190（2026-11-15）
#
# ⚠️ 纪律：
#   * 不碰主工作树 /root/projects/comfyui-minimax-h3-studio（他人 WIP）
#   * 不碰 /root/projects/h3studio-merge（local/maintainer 的 worktree，
#     它的 index + 工作区是一份**旧快照**，已被污染）——
#     改用 `git update-ref` 原子移动分支指针，不动任何 worktree 的文件
#   * 只重启 h3-studio.service
#
# 流程：
#   0) 现状快照 + 建回滚点
#   1) 在 /root/projects/h3s-mc-truth 上提交分支
#   2) 在该 worktree 里造合并提交（基于 local/maintainer 最新 tip）
#   3) update-ref 移动 local/maintainer
#   4) live worktree：update-ref + 精确 checkout 改动的文件
#   5) 重启服务 + 四路径 200 探活 + 浏览器 console/pageError=0
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
DEV=/root/projects/h3s-mc-truth
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/media-card-lora-truth-20261115
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "local/maintainer = $MAINTAINER_BEFORE"
echo "live             = $LIVE_BEFORE"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# ---- 回滚点：备份 live 分支指针 + 记录 maintainer 指针 ----
git branch "backup/live-before-mc-truth-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-mc-truth-${TS}" local/maintainer
echo "回滚点：backup/live-before-mc-truth-${TS} / backup/maintainer-before-mc-truth-${TS}"

echo
echo "=== 1) 提交分支 ==="
cd "$DEV"
git add -A h3studio/artifact_meta.py h3studio/job_meta.py h3studio/video_models.py \
        server.py web/app.js web/job-meta.js \
        scripts/backfill_artifact_meta.py scripts/verify_media_card_truth_browser.py \
        tests/test_artifact_meta.py tests/test_media_card_detail.py
git commit -m "fix(media-card): LoRA 以产物实际为准 + 卡片详情模态框（步数/调度器等）

根因：jobs.params 存的是「任务提交参数」，不是「产物实际元数据」。
工作流构建时才按实际挂载的 LoRA 回落步数/调度器/采样器，但这些值
没落库 -> 显示层只能猜，且 summarize 与 resolve_job_lora 两套取值
路径口径不一致（同一张卡片「LoRA 行」说「无」、详情区说「ckpt850」）。

修法：
- 新增 h3studio/artifact_meta.py：从**构建好的 workflow** 反解产物
  实际参数（步数/调度器/采样器/CFG/种子/尺寸/实际挂载的 LoRA 清单）。
- worker 在 build_workflow 之后回写 jobs.params（单张 + 批次两条路径）。
- job_meta.resolve_job_lora / summarize 统一以 artifact_loras 为最高优先级。
- video_models.resolve_accel_lora_slot 的 legacy/fixed 分支补白名单闸门
  （提示词冒充 LoRA 的脏值不再被当 LoRA 返回）。
- 新增 GET /api/jobs/{id}/artifact-meta：按产物返回完整生成参数。
- 前端：卡片点开详情模态框改读该接口，缺失字段显式标「未知」。
- 文案：没有 LoRA 时显示「未使用 LoRA」（用户要求），不再是「无」。
- 新增 scripts/backfill_artifact_meta.py：历史任务从 ComfyUI history 回填。"

git log --oneline -1

echo
echo "=== 2) 造合并提交（基于 local/maintainer 最新 tip）==="
git checkout -B tmp/merge-mc-truth local/maintainer
# 把分支的改动搬过来（保留合并提交的父：maintainer + 我的分支）
git merge --no-ff "$BRANCH" \
  -m "merge(media-card-truth): 媒体库卡片 LoRA 以产物实际为准 + 卡片详情模态框（步数/调度器等）" \
  || { echo "!! 合并冲突"; exit 1; }
MERGE_COMMIT=$(git rev-parse HEAD)
git log --oneline -1
git checkout "$BRANCH"
git branch -D tmp/merge-mc-truth

echo
echo "=== 3) 原子移动 local/maintainer ==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
echo "local/maintainer -> $(git rev-parse local/maintainer)"

echo
echo "=== 4) live worktree 同步 ==="
cd "$LIVE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"
# 精确 checkout 我改的文件（不动 live 里其它人的未提交内容）
git checkout "$MERGE_COMMIT" -- \
  h3studio/artifact_meta.py h3studio/job_meta.py h3studio/video_models.py \
  server.py web/app.js web/job-meta.js \
  scripts/backfill_artifact_meta.py scripts/verify_media_card_truth_browser.py \
  tests/test_artifact_meta.py tests/test_media_card_detail.py
git -C "$LIVE" status --porcelain | head -20
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

echo
echo "=== 5) 重启服务 + 四路径探活 ==="
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

for p in / /api/jobs /api/options /api/queue; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 浏览器探活（console / warning / pageError = 0）==="
H3_BASE="http://127.0.0.1:${PORT}" \
H3_OUT="/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/evidence-media-card-truth-live" \
  /home/ubuntu/comfyui/venv/bin/python \
  "$DEV/scripts/verify_media_card_truth_browser.py" || echo "!! 浏览器验收有失败项"

echo
echo "=== 7) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "merge      = $MERGE_COMMIT"
echo "PID        = $PID_AFTER"
echo "回滚点     = backup/live-before-mc-truth-${TS}"
