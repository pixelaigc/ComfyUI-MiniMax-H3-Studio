#!/usr/bin/env bash
# 第 6 轮代码审查部署 —— 上线 8190（2026-11-18）
#
# 与 deploy_cr5_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/code-review-20261118
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
# ⚠ 这个 SHA 是**上一轮部署**留下的（本轮第一次部署成功了，合并提交 = f1dcb3f），
#   本轮第二次部署要把「补传 job id」的修复推上去 —— 所以基线是 f1dcb3f，
#   不是最初的 aa0556f。
#   判据仍然是「live/maintainer 与我上次部署后的状态一致」，
#   别人推进了就会中止。
EXPECT_LIVE=aa0556f7d6ba38a359c1479f360e66f43d384a9d
EXPECT_LIVE_AFTER_FIRST_DEPLOY=f1dcb3f1112eb0b66464242de70c974568a1402e
EXPECT_LIVE_AFTER_SECOND_DEPLOY=de9ce19182ec9da17dc43fb2f9b4dfb2479b689e
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ] && \
   [ "$LIVE_BEFORE" != "$EXPECT_LIVE_AFTER_FIRST_DEPLOY" ] && \
   [ "$LIVE_BEFORE" != "$EXPECT_LIVE_AFTER_SECOND_DEPLOY" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE 既不是 $EXPECT_LIVE 也不是 $EXPECT_LIVE_AFTER_FIRST_DEPLOY），中止部署"
  exit 1
fi
if [ "$MAINTAINER_BEFORE" != "$LIVE_BEFORE" ]; then
  echo "!! maintainer 与 live 不一致（$MAINTAINER_BEFORE vs $LIVE_BEFORE），中止部署"
  exit 1
fi

# 【live 工作区干净性】只允许**未跟踪**文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

git branch "backup/live-before-cr6-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-cr6-${TS}" local/maintainer
echo "回滚点：backup/live-before-cr6-${TS} / backup/maintainer-before-cr6-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cr6_mergetree.txt
if grep -q '<<<<<<<' /tmp/cr6_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/cr6_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(audit-cr6): 第6轮代码审查 —— 尾裁跨接缝 / 显示值≠产物 / 可见文案污染 / 缓存破除

基线：aa0556f（第5轮终稿 + 接续成片时长收口 + flf/r2v 2.0MP）
合并：local/feature/code-review-20261118

【P1-1】成片尾裁跨过接缝（h3studio/continuation.py）
  trim 无条件全扣到最后一段尾部，但 trim 可能超过最后一段自己的交付帧数
  -> 尾裁一路吃进上一段，接缝从成片里消失（4 段 x 2 秒：keep_frames[-1]=-4）。
  修法：裁不到目标就一帧都不裁（保住内容与接缝），残差如实上报。

【P1-2】显示的累计时长 != 真实成片（server.py + continuation_service.py）
  成片拼接做了尾裁，但 continuation_state 的 total_frames 是「累计交付帧数」
  （不含尾裁）-> 显示比真实成片长一个尾裁帧数（线上实测 9.417s vs 216 帧=9.000s）。
  修法：协议层口径不动（它是音频偏移 C_n 的来源，动了会音画错位），
        只在展示层补尾裁口径。

【P1-3】用户可见的 markdown 强调残留
  真实浏览器取证（Chromium + Playwright，12 面板）：可见文本 39 处 + 属性值 71 处。
  修掉全部；官方提示词模板原文一字未动。
  新增 acorn AST 扫描器（scripts/cr6_scan_markdown_leaks.cjs）作护栏。

【P1-4】静态资源 ?v= 未升（老用户拿不到修复）
  app.js / video-panel.js / continuation.js / qwen-refs.js / providers.js /
  media-manage.js / agent-manage.js 升到 cr20261118a / vparams6。

【回归】新增 tests/test_cr6_p1_fixes.py（10 用例，含变异自证）
  定向 350 passed；全量对拍新增实质失败 0（13 条并发 flake 已串行复验归因，
  两边各 73/73 全绿）。
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
CHANGED=$(git diff --name-only "$LIVE_BEFORE" "$MERGE_COMMIT")
echo "$CHANGED" | sed 's/^/  /'
echo "共 $(echo "$CHANGED" | wc -l) 个文件"
# 【部署脚本把自己也带上】—— 否则 git checkout <commit> -- <清单>
# 会把「清单外的 tracked 路径」当成「新 commit 里不存在」而删除
if [ -f "$LIVE/scripts/deploy_cr6_8190.sh" ]; then
  git checkout "$MERGE_COMMIT" -- scripts/deploy_cr6_8190.sh 2>/dev/null || true
fi
# 【中文文件名必须用 NUL 分隔传给 git checkout】
# 用 `$(... tr '\n' ' ')` 会把 "docs/代码审查报告-....md" 这种**带引号**的
# git 路径原样传下去 -> git 报「路径规格未匹配任何 git 已知文件」。
# 正确做法：git diff -z + xargs -0。
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 git checkout "$MERGE_COMMIT" --)
echo
echo "=== 5) 部署后完整性自检 ==="
# 【同样要用 NUL 分隔】`git diff --name-only` 对非 ASCII 路径会加引号，
# 逐行读回来再 `[ -e "$LIVE/$f" ]` 会因为引号而误判「缺失」。
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/cr6_missing.txt
MISSING=$(cat /tmp/cr6_missing.txt)
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
for p in "/" "/api/jobs" "/api/queue" "/api/video-options" "/api/image-options" "/api/generation-types" "/api/continuation/state"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 线上关键行为复验 ==="
echo "--- 静态资源版本号已升 ---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(app|video-panel|continuation|qwen-refs|providers|media-manage|agent-manage)\.js\?v=[^"]+' | sort -u
echo "--- 可见文案已无 markdown 残留 ---"
python3 /root/projects/h3studio-review-r6/scripts/cr6_scan_markdown_leaks.cjs "$LIVE/web" 2>&1 | tail -2
echo "--- 接续显示值（应与真实成片一致）---"
curl -s "http://127.0.0.1:${PORT}/api/jobs/e451d93897e6/continuation?duration=5" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('  total_seconds =', d.get('total_seconds'), ' total_frames =', d.get('total_frames'))"
echo "  （真实成片 v-h3-studio-000531.mp4 = 216 帧 = 9.000s）"

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-cr6-${TS} / backup/maintainer-before-cr6-${TS}"
