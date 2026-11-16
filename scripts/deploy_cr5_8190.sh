#!/usr/bin/env bash
# 第 5 轮代码审查部署 —— 上线 8190（2026-11-16）
#
# 与 deploy_cr4b_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-cr5
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "审查分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

git branch "backup/live-before-cr5-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-cr5-${TS}" local/maintainer
echo "回滚点：backup/live-before-cr5-${TS} / backup/maintainer-before-cr5-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cr5_mergetree.txt
if grep -q '<<<<<<<' /tmp/cr5_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/cr5_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-cr5): 第5轮代码审查 —— C 段 12 条收口 + A 段文案泄漏修复

基线：live 4eb09e7（第4轮终稿，已含他人补漏的 B-14 bindSeg 删除）
合并：local/feature/code-review-cr5

【C 段 · 后端归一化契约（跨越「面板 / Agent / 老任务重放」三条链路）】
- C-5/C-6 图像模式（t2i/t2i_qwen/i2i_qwen/t2i_moss/t2i_h3image）的
  steps/cfg/shift/sampler/scheduler 完全不归一 -> 新增 h3studio/gen_normalize.py
  （唯一归一实现，白名单来自各模型表自己的 samplers/schedulers 字段）。
  契约 R1 合法值不动（老任务重放语义安全）；R2 非法值回落 + 记 normalize_notes。
- C-9   prepare_agent_t2v_params 硬写死 mode=t2v -> Agent 建 flf/r2v 时
  按 t2v 口径判（面积上限 / Ref2VA 通道）。现在加 mode 形参并由 agent.py 传参。
- C-10  build_t2v_panel 缺宽高产出 0×0 latent（实测确认）-> 明确报错 +
  worker 侧用同一套公式从 resolution/aspect 补齐，无依据时宁可报错不编造。
- C-11/C-12 死常量清理（VIDEO_MAX_PIXELS_LITERAL / VIDEO_MAX_PIXELS /
  VIDEO_DURATION_MIN_OLD / VIDEO_DURATION_MAX_OLD）。
- C-13  _frame_expr_node 三份重复实现合一 -> 新建零依赖 h3studio/frame_expr.py。
- C-15  analysis_jobs.progress_cb 的 c.close() 在 try 内 -> 连接泄漏。改 try/finally。
- C-16  agent_settings.normalize_t2v_defaults / default_t2v_defaults 零引用 -> 删。
- C-17  assets.atomic_move 跨盘回退 copy2 直写 target 最终路径（复制中可见半成品）
  -> 改「同盘临时文件 + 原子替换」。

【A 段 · 前端用户可见文案污染】
- 骨架卡 prompt 原为「……此处的临时文字仅用于对齐布局。」—— 该文案**真的进 DOM**
  （<b> 文本节点），CSS 只是 color:transparent 涂掉，但「复制页面内容」/读屏软件
  照样能拿到 = 开发者措辞泄漏。换成纯用户向加载态文案（33 字，等长，布局不变）。
- 补齐 tests/test_visible_text_pollution.py 的漏网措辞（临时/对齐/布局/仅用于…）
  + 新增一条「DOM 路径」断言。

【D-2 联动】web/app.js 改了内容 -> ?v= 升到 cr20261116a。

【测试】新增 tests/test_cr5_c_segment.py（30 用例）
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
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- \
  h3studio/agent.py \
  h3studio/agent_settings.py \
  h3studio/analysis_jobs.py \
  h3studio/assets.py \
  h3studio/frame_expr.py \
  h3studio/gen_normalize.py \
  h3studio/h3_video_modes.py \
  h3studio/video_models.py \
  h3studio/video_workflow.py \
  h3studio/workflows.py \
  server.py \
  web/app.js \
  web/index.html \
  scripts/verify_cr5_8190_browser.py \
  scripts/compare_suite_cr5.sh \
  scripts/cr5_scan_text_candidates.py \
  scripts/serial_recheck_cr5.sh \
  scripts/deploy_cr5_8190.sh \
  tests/test_cr5_c_segment.py \
  tests/test_cr5_text_scanner.py \
  tests/test_visible_text_pollution.py \
  tests/test_frontend_assets_hygiene.py
git -C "$LIVE" status --porcelain | grep -v '^??' | head -20
echo "(上面应只列出本轮改的文件 = 部署成功)"
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

# 【部署后的完整性自检 —— 2026-11-16 第5轮实测踩坑】
# `git checkout <commit> -- <files>` 只更新**清单里列的路径**；
# 清单外的路径若在 live 的 index 里是「tracked 且工作区缺失」的状态，
# 会被 git 当成「该路径在新 commit 里不存在」而**删除**。
# 本轮就踩到了：脚本把自己（deploy_cr5_8190.sh）漏在清单外，
# 结果脚本在运行途中把自己删了（live 工作区出现 D scripts/deploy_cr5_8190.sh）。
# 修法：① 清单里包含自己；② 部署后逐个核「本轮文件都在工作区」。
echo
echo "=== 部署完整性自检 ==="
MISSING=0
for f in h3studio/agent.py h3studio/agent_settings.py h3studio/analysis_jobs.py \
         h3studio/assets.py h3studio/frame_expr.py h3studio/gen_normalize.py \
         h3studio/h3_video_modes.py h3studio/video_models.py \
         h3studio/video_workflow.py h3studio/workflows.py server.py \
         web/app.js web/index.html \
         scripts/verify_cr5_8190_browser.py scripts/compare_suite_cr5.sh \
         scripts/cr5_scan_text_candidates.py scripts/serial_recheck_cr5.sh \
         scripts/deploy_cr5_8190.sh \
         tests/test_cr5_c_segment.py tests/test_cr5_text_scanner.py \
         tests/test_visible_text_pollution.py tests/test_frontend_assets_hygiene.py; do
  if [ ! -f "$LIVE/$f" ]; then
    echo "  缺失: $f"
    MISSING=$((MISSING + 1))
  fi
done
if [ "$MISSING" -eq 0 ]; then
  echo "  本轮 23 个文件全部就位"
else
  echo "  !! 缺失 $MISSING 个文件 —— 需要补 checkout"
  exit 1
fi
# 工作区与 HEAD 必须完全一致（tracked）
if [ -n "$(git -C "$LIVE" diff --name-only HEAD)" ]; then
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
for p in / /api/jobs /api/queue /api/video-options /api/image-options /api/generation-types; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "回滚点     = backup/live-before-cr5-${TS}"
