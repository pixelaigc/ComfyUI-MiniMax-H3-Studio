#!/usr/bin/env bash
# 部署「Agent 能力自述真实化 + 媒体后处理补齐 + 多步请求一气呵成」到 8190
# （2026-11-20）
#
# 与既有 deploy_*.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/agent-defaults-truth
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

# 【并发安全】live 必须仍停在本轮开始时看到的状态。
EXPECT_LIVE=9f74dd79640eb880bd75bd149fcb67d43897c9f3
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE != $EXPECT_LIVE），中止部署"
  exit 1
fi
# 【maintainer 与 live 的关系】live 是「含 maintainer 的合并提交」，
# 所以两者**不是**同一个 commit（live 的父提交才是 maintainer）。
# 判据用**祖先关系**：maintainer 必须是 live 的祖先，否则说明
# maintainer 被别人推进过而 live 没回灌 —— 那时合并会覆盖别人的工作。
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$LIVE_BEFORE"; then
  echo "!! maintainer 不是 live 的祖先（$MAINTAINER_BEFORE vs $LIVE_BEFORE），中止部署"
  exit 1
fi
echo "maintainer 是 live 的祖先（live 是含 maintainer 的合并提交）"

# 【live 工作区干净性】只允许**未跟踪**文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

git branch "backup/live-before-agentcap-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-agentcap-${TS}" local/maintainer
echo "回滚点：backup/live-before-agentcap-${TS} / backup/maintainer-before-agentcap-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/deftruth_mergetree.txt
if grep -q '<<<<<<<' /tmp/deftruth_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/deftruth_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
fix(agent): 管理页默认参数改读真实配置 + 分辨率统一为 MP 预算 x 百分比

【用户实测反馈，确实还没修】「内置 Agent 管理」面板里首尾帧(flf) /
参考生(r2v) 的默认参数仍是老配置（旧档位 544p/768p、旧时长、旧模型清单）。

【定位事实】
- 存储：SQLite model_provider_prefs / key=agent_settings / gen_defaults
- 下发：GET/PUT /api/agent/settings -> gen_defaults + gen_defaults_spec
- 渲染：web/agent-manage.js::renderGenDefaults() 完全由 spec 驱动
- 两条路径：面板提交 -> prepare_job_params -> h3_video_size.apply_size
             Agent 自建 -> merge_into_params -> prepare_agent_t2v_params

【核心修正 1｜默认参数不再写死旧档位】
_video_legacy_fields() 原来用 wf.RES_MP.keys()（544p/768p 两档），
默认写死 "768p"，note 写死「544p = 0.4MP / 768p = 0.8MP」。那不是真实
能力，是旧界面（#resSeg）的两档遗留。真实约束：flf/r2v 面积上限与 t2v
同为 2.0 MP（2026-11-17 放宽），custom_size=True。现在按各 mode 真实
上限派生（28 档）。

【核心修正 2｜统一为「MP 预算 x 百分比」（用户 2026-11-20 原话）】
新增 h3studio/size_spec.py —— 分辨率的唯一换算入口：
MP 预算 = 该 mode 真实上限（实时派生）-> 实际 MP = 预算 x 百分比 ->
宽高 = video_dimensions(aspect, mp)（与 ComfyUI ResolutionSelector 同源，
天然对齐 32）-> 超上限按比例缩放 + 给出原因 ->
调整原因写进既有 params["size_adjust_reasons"]。
覆盖 t2v/flf/r2v + 六个图片 mode；音乐无尺寸概念，不加字段。

【核心修正 3｜存量迁移】
migrate_size_expression() 在**读取时**迁移（幂等）。按旧口径
（workflows.RES_MP）反查 —— 历史上「768p」有两个不同含义
（旧 0.8MP=1216x672 / 新短边 1344x768），按旧口径保证用户已配好的像素不变。

【顺带修掉的两个真缺陷（实测复现）】
1. max_pixels_for_mode 读导入期快照，而 size_policy_for_mode 已实时
   派生 —— 同一真源两套行为。改为复用后者。
2. 显式宽高不吸附到 32 的倍数（1900x1000 原样跑）。H3 CANVAS_MULTIPLE=32
   要求对齐。size_spec.plan_size 与 h3_video_size.resolve_size 都修。

【能力自述同步】
agent_capability / list_modes 的 video_resolution_note / agent_schema
全部改读真实配置，并暴露 megapixels + resolution_percent。

【测试】新增 tests/test_agent_defaults_truth.py 31 项；相关既有 143 全绿。
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
#
# 【为什么必须过滤删除（2026-11-20 实测差点踩坑）】
# `git checkout <commit> -- <path>` 对一个在 <commit> 里**不存在**的路径
# 会**真的删掉** live 工作区里的那个文件。
# 而 `git diff --name-only LIVE_BEFORE MERGE_COMMIT` 会把
# 「live 有、合并后没有」的路径也列进来 —— 那是**删除类**变更。
#
# 【本轮的实际情形】live 分支比 maintainer 多了一个交付文档
# （docs/参考素材右键移除-交付说明-20261120.md）。它在合并提交里
# **仍然存在**（不是删除），但如果用「live_before vs 合并提交」的
# 差异清单去 checkout，一旦清单里混进真正的删除项就会误删。
#
# 修法：把清单拆成「新增/修改」与「删除」两类，只 checkout 前者；
# 删除类**不 checkout**（live 上保留原文件，由人工确认后再处理），
# 并在日志里明确列出来。
CHANGED_ALL=$(git diff --name-only "$LIVE_BEFORE" "$MERGE_COMMIT")
echo "$CHANGED_ALL" | sed 's/^/  /'
echo "共 $(echo "$CHANGED_ALL" | wc -l) 个差异文件"

# 只保留「在合并提交里存在」的路径（= 新增或修改）
CHANGED_KEEP=$(git diff --name-only --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT")
# 被删除的路径（合并提交里已不存在）—— 不 checkout，只报告
DELETED=$(git diff --name-only --diff-filter=D "$LIVE_BEFORE" "$MERGE_COMMIT")
if [ -n "$DELETED" ]; then
  echo
  echo "--- 以下路径在合并提交里已删除：**不 checkout**（避免误删 live 文件）---"
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
# 只核对「新增/修改」的那批（删除类不在 checkout 范围内）
git diff --name-only -z --diff-filter=d "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/deftruth_missing.txt
MISSING=$(cat /tmp/deftruth_missing.txt)
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
for p in "/" "/api/jobs" "/api/image-options" "/api/generation-types" "/api/enhancer" "/api/agent/settings"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-agentcap-${TS} / backup/maintainer-before-agentcap-${TS}"
