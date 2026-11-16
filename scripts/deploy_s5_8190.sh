#!/usr/bin/env bash
# S5 收尾部署 —— 上线 8190（2026-11-16）
#
# 与 deploy_cr5_8190.sh 同纪律，并带上第 5 轮实测踩坑后的两条加固：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 【加固 1】checkout 清单里**包含脚本自己** —— 否则
#     `git checkout <commit> -- <files>` 会把清单外的 tracked 缺失路径删掉
#     （第 5 轮实测：脚本把自己删了，live 出现 D scripts/deploy_cr5_8190.sh）
#   * 【加固 2】部署后做完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-cr5
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

# ---- 本轮（S5）改动的文件清单 ----
# 【注意】必须包含脚本自己（见文件头「加固 1」）。
DEPLOY_FILES=(
  web/enhance-menu.js
  web/index.html
  tests/test_video_sampling_params.py
  tests/test_yue2_flow_review_fixes.py
  tests/test_agent_oneshot_e2e.py
  tests/test_frontend_assets_hygiene.py
  scripts/deploy_s5_8190.sh
)

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "审查分支    = $(git rev-parse "$BRANCH")"
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

git branch "backup/live-before-s5-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-s5-${TS}" local/maintainer
echo "回滚点：backup/live-before-s5-${TS} / backup/maintainer-before-s5-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/s5_mergetree.txt
if grep -q '<<<<<<<' /tmp/s5_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/s5_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-s5): S5 收尾 —— 清掉第 5 轮列出的 7 条基线既有失败测试

基线：live b6f53ac（第5轮终稿）
合并：local/feature/code-review-cr5

【Case 1 · test_video_sampling_params.py 5 条 —— 断言过期，非产品缺陷】
- 3 条：夹具缺必填字段「首帧/尾帧」（flf 自 a2c3622 起要求至少一帧，
  h3studio/h3_video_modes.py:429-433）-> 补 "first_frame": "a.png"。
- 2 条：断言读已删除的 legacy 面板代码（withLora / effectiveLegacyLora）——
  第4轮 B-11 真源搬到共享面板时随旧面板删除 -> 断言改读新真源的等价语义
  （web/video-panel.js + 服务端档位常量 + resolve_accel_lora_slot）。
- 期望档位值一个都没动（4/beta/euler、20/simple/res_multistep）。

【Case 2 · test_yue2_flow_review_fixes.py 1 条 —— 真实缺陷（声明缺失）】
web/enhance-menu.js 的 director 条目补 write: { kind: "text" }。
行为未变（methodWriteContract 本就按 text 兜底），只是把契约写出来。

【Case 3 · test_agent_oneshot_e2e.py 1 条 —— 测试隔离缺陷】
测试漏打桩「提示词增强管线」的 llm.call_provider（另一个模块/另一个函数），
导致增强管线真的去调本机 LLM（实测占总耗时 62%：10.50s / 17.06s），
本机 LLM 一忙就击穿 90s 等待预算。
补上打桩 -> 46.25s -> 14.42s，负载下连跑 3 次全通过。
产品代码一行没动；没有 skip / xfail / 放宽断言。

【D-2 联动】web/enhance-menu.js 改了内容 -> ?v= cr20261115b -> cr20261116b。
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
# 【2026-11-16 S5 修 —— set -e 下 grep 无匹配会静默退出整个脚本】
# 原来这行是 `git status --porcelain | grep -v '^??' | head -20`：
#   当**没有 tracked 改动**时 grep 无输出、退出码 1
#   -> 在 `set -euo pipefail` 下整个脚本**静默退出**（第 4 步之后就断了，
#      完整性自检与重启都没跑，但 exit code 仍是 0）。
# 本轮实测就踩到了（输出停在第 4 步标题，服务 PID 没变）。
# 修法：`|| true` 兜住 grep 的「无匹配」，并把展示与判定分开。
CHANGED=$(git -C "$LIVE" status --porcelain | grep -v '^??' || true)
if [ -n "$CHANGED" ]; then
  echo "$CHANGED" | head -20
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
echo "回滚点     = backup/live-before-s5-${TS}"
