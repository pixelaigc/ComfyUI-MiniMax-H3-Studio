#!/usr/bin/env bash
# 部署「生成媒体库载入慢」服务端修复到 8190（2026-11-21）
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
BRANCH=local/fix/mediaperm-perf
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
# 【并发安全】live 必须仍停在本轮开始时看到的状态。
#
# 【为什么用「关系」而不是写死 SHA】并发任务（收藏功能）可能已经
# 推进了 local/maintainer 与 live。写死 SHA 会在对方一提交时就中止；
# 关系判据则能识别「对方又推进了 mainline」并要求先 rebase。
#
# 本轮额外多一条：**收藏分支**必须已经包含当前 maintainer ——
# 否则合并会把对方未合入 mainline 的改动覆盖掉。
# ------------------------------------------------------------------
if [ "$LIVE_BEFORE" != "$MAINTAINER_BEFORE" ]; then
  echo "!! live 与 maintainer 不一致（$LIVE_BEFORE vs $MAINTAINER_BEFORE），中止部署"
  exit 1
fi
if ! git merge-base --is-ancestor "$MAINTAINER_BEFORE" "$BRANCH"; then
  echo "!! 修复分支没有包含当前 maintainer（$MAINTAINER_BEFORE），"
  echo "   说明对方又推进了 mainline —— 先 rebase 修复分支再部署。中止"
  exit 1
fi
echo "live == maintainer，且修复分支已包含最新 mainline"

# 【live 工作区干净性】只允许**未跟踪**文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

# 【本次改动的文件清单】先打印，确认没有踩到并发任务占用的前端文件
echo
echo "--- 本次改动将落到 live 的文件 ---"
git diff --name-only "$MAINTAINER_BEFORE" "$BRANCH" | sed 's/^/  /'

git branch "backup/live-before-perf-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-perf-${TS}" local/maintainer
echo "回滚点：backup/live-before-perf-${TS} / backup/maintainer-before-perf-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/perf_mergetree.txt
if grep -q '<<<<<<<' /tmp/perf_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/perf_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
fix(mediaperm): 生成媒体库载入慢的三条服务端根因（实测定位 + 变异测试）

【用户症状】2026-11-20 实测反馈：生成媒体库卡片「有时候载入很慢很慢」。
「有时候」是关键词 —— 空闲时 41ms 很快，慢的是**特定条件**。

【根因 1（最主要）｜效果 LoRA 清单每卡一次全盘扫盘】
  h3studio/job_meta._effect_lora_display() 每次调用都遍历
  h3_effect_loras.discover_effect_loras() 的完整结果，而那个结果要
  **扫全盘 712 个 safetensors**（实测单次 377~980ms，其中
  read_safetensors_header + lora_modules 占 914ms）。
  列表接口每条任务要问好几次 -> 一次请求里就是好几次全盘扫描。
  discover_effect_loras() 自己的缓存 TTL 只有 60 秒 —— 所以表现是
  **每 60 秒必有一次尖峰**（实测线上长跑采样：p50=23.7ms，
  p99=444.6ms，max=731.1ms）。
  修法：新增 _effect_lora_display_map() —— 一次性拿到
  {权重文件名 -> 显示名} 映射（模块级 + TTL 300s），
  _effect_lora_display() / _in_lora_registry() 改为查这张表。
  一次请求从「N 次扫盘」降到「1 次」。

【根因 2｜全盘扫盘跑在 aiohttp 事件循环里】
  映射的首次构建如果发生在 row_to_job() 里，那次扫盘就把整个 8190
  冻住（与 2026-11-16「整站不可访问」同类故障）。
  修法：api_list 用 asyncio.to_thread(job_meta.warm_effect_lora_display_map)
  **先**预热（在线程里），再进 row_to_job。

【根因 3｜/history 没有缓存】
  /queue 有 3 秒缓存，/history 没有。live_state_for_job() 对每条
  running 任务各查一次 /history —— ComfyUI 忙时（实测本机
  /object_info 满载 8.9s、/queue 5.2s）列表接口被拖到同一量级，
  而且**每次都不命中**。
  修法：新增 comfy_history_present() —— 查到过就**永久**缓存
  （ComfyUI 的 history 不删条目），没查到短缓存 3 秒。
  判定口径不变（缓存的是「history 有没有这条」这个布尔事实）。

【附带修掉的 N+1】
  * batches_of() 每条任务一次 SELECT -> batches_map() 一次 IN (...)。
    实测：page_size=100 时批次查询 100 次 -> 1 次。
  * _lineage_durations() 每段开一次 SQLite 连接 -> 复用一条连接；
    并且复用调用方（row_to_job）已有的本段 params，不再多查一次库。
    实测：row_to_job x20 的 SQL 从 23 条 -> 3 条，x60 从 68 -> 7。

【修前/修后实测对比（隔离实例 + 假 ComfyUI，同一组指标）】
  ComfyUI /history 慢 2 秒 + 有 running 任务：
    修前 /api/jobs 每次 2027.5~2049.9ms -> 修后 13.4~32.3ms
  ComfyUI /queue 慢 2 秒：
    修前 24.8~28.8ms -> 修后 11.0~44.0ms
  ComfyUI 空闲：
    修前 22.8~43.4ms -> 修后 8.5~27.9ms

【测试】tests/test_mediaperm_perf.py：16 例全部通过。
  覆盖：AST 判定显示函数/白名单不再直调扫盘、映射记忆化（调用次数）、
  to_thread 真的换线程、预热在 row_to_job 之前、/history 查过永久缓存 /
  没查短缓存 / 异常不缓存、批次查询恒为 1 次、血缘复用调用方 params。
  【变异测试】10 个变异体逐个改坏 -> **全部被对应断言抓住**
  （0 条「断言没红」），证据 evidence/mediaperm-20261121/mutation-result.txt。

【契约未被破坏】2026-11-12「客户端没传就不写回」的采样类字段契约
  （sampler/scheduler/steps 仅由 Agent 路径套用）**未被本次改动触碰**：
  diff 里没有任何采样字段相关行；tests/test_video_sampling_params.py
  50 例全绿；且实测两版 normalize_t2v_params 对 flf/r2v/t2v 的
  写回行为完全一致（都不写回）。
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
echo "--- 实际 checkout 的文件（新增/修改）--"
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
     done) > /tmp/perf_missing.txt
MISSING=$(cat /tmp/perf_missing.txt)
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
for p in "/" "/api/jobs" "/api/agent/settings" "/api/system" \
         "/api/generation-types" "/api/jobs/favorites"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 修复是否真的上线（线上代码自检）==="
echo "--- 映射记忆化存在？---"
grep -c "_effect_lora_display_map" "$LIVE/h3studio/job_meta.py"
echo "--- to_thread 预热存在？---"
grep -c "asyncio.to_thread(job_meta.warm_effect_lora_display_map)" "$LIVE/server.py"
echo "--- /history 缓存存在？---"
grep -c "_HISTORY_PRESENT_CACHE" "$LIVE/server.py"
echo "--- 批次批量查询存在？---"
grep -c "batches_map" "$LIVE/server.py"

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-perf-${TS} / backup/maintainer-before-perf-${TS}"
