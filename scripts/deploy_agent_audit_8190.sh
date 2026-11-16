#!/usr/bin/env bash
# 内置 Agent 审查收口部署 —— 上线 8190（2026-11-16）
#
# 沿用 deploy_s5_8190.sh 的全部纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 【加固 1】checkout 清单里**包含脚本自己**
#   * 【加固 2】部署后做完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
#   * 【加固 3】set -e 下 grep 无匹配会静默中断 —— 一律 `|| true` 兜住
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/agent-audit-20261116
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

# ---- 本轮改动的文件清单 ----
# 【注意】必须包含脚本自己（见文件头「加固 1」）。
DEPLOY_FILES=(
  server.py
  web/agent.js
  web/index.html
  scripts/gen_flow.py
  scripts/templates/flow-agent.mmd
  scripts/deploy_agent_audit_8190.sh
  docs/交互流程图-总览.mmd
  docs/交互流程图-总览.svg
  docs/交互流程图-总览.png
  docs/交互流程图-创作链路.mmd
  docs/交互流程图-创作链路.svg
  docs/交互流程图-创作链路.png
  docs/交互流程图-素材库.mmd
  docs/交互流程图-素材库.svg
  docs/交互流程图-素材库.png
  docs/交互流程图-内置Agent.mmd
  docs/交互流程图-内置Agent.svg
  docs/交互流程图-内置Agent.png
  docs/交互流程图-系统配置.mmd
  docs/交互流程图-系统配置.svg
  docs/交互流程图-系统配置.png
  docs/交互流程图-派生清单.md
  tests/test_flow_diagram.py
  tests/test_agent_frontend_enh.py
  tests/test_agent_contract.py
  tests/test_frontend_assets_hygiene.py
  docs/内置Agent审查报告-20261116.md
  # 【2026-11-16 修：必须把新增的证据文件也列进来】
  # 上一轮漏列的后果：live 工作区里这些文件是「已删除」态（索引有、工作区没有），
  # 部署完整性自检正确拦住了部署（live 工作区与 HEAD 不一致）。
  # 根因：`git checkout <commit> -- <清单>` 只更新清单内的路径，
  # 清单外的新增文件不会被带过去。
  audits/agent_attach_browser.py
  audits/agent_audit_api_probe.json
  audits/agent_audit_api_probe.py
  audits/agent_audit_browser.py
  audits/agent_retry_probe.py
  audits/agent_settings_roundtrip.json
  audits/agent_settings_roundtrip.py
  audits/agent_tools_live_probe.json
  audits/agent_tools_live_probe.py
  audits/agent_tools_live_probe_full.json
  audits/agent_tools_run_code.json
  audits/attach_browser2/attach_browser.json
  audits/attach_browser2/attach_uploaded.png
  audits/browser_audit3/01_agent_page.png
  audits/browser_audit3/02_drawer.png
  audits/browser_audit3/05_stream.png
  audits/browser_audit3/05b_token.png
  audits/browser_audit3/08_clarify.png
  audits/browser_audit3/09_error_retry.png
  audits/browser_audit3/10_manage.png
  audits/browser_audit3/15_artifacts.png
  audits/browser_audit3/browser_audit.json
  audits/retry_probe3/A_http_500.png
  audits/retry_probe3/B_sse_error.png
  audits/retry_probe3/C_sse_done_failed.png
  audits/retry_probe3/retry_probe.json
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

git branch "backup/live-before-agentaudit-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-agentaudit-${TS}" local/maintainer
echo "回滚点：backup/live-before-agentaudit-${TS} / backup/maintainer-before-agentaudit-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/agent_audit_mergetree.txt
if grep -q '<<<<<<<' /tmp/agent_audit_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/agent_audit_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(agent-audit): 内置 Agent 审查收口 —— 3 个真实缺陷修复 + 流程图同步

【缺陷 1 · 流程图与代码脱同步（线上遗留失败）】
2026-11-14 有 10 个 Agent 工具（媒体库管理 / 外部能力 / ffmpeg 音视频）
加入 agent.TOOLS，但 scripts/gen_flow.py 的 TOOL_GROUP_RULES 没同步更新，
docs/ 里的流程图也没重跑 —— tests/test_flow_diagram.py 的
「每个 Agent 工具都必须有节点」断言转红，一直没人发现。
修：补 3 个正式分栏（媒体库管理类 / 外部能力类 / ffmpeg 音视频类）+
    对应落地侧节点（ML / EX / FF）+ make flow 重生成 5 张图与派生清单。
新回归：test_every_agent_tool_has_a_named_group
        test_agent_tool_side_nodes_exist
        test_new_agent_tool_groups_have_side_nodes

【缺陷 2 · SSE 失败路径丢失「重试」入口】
服务端把 Agent 执行失败发成 SSE error 事件（HTTP 200）或 done(status=failed)，
两者都不抛错 -> 前端 send() 走进「成功」分支 -> 无条件 msg.retryRequest = null
-> 红色错误条正常显示，但「重试」按钮消失，用户只能手打一遍。
修：只有「真成功」（无 error 且 status 非 failed/error/aborted）才清 retryRequest。
新回归：test_sse_error_event_keeps_retry_entry
        test_sse_done_failed_keeps_retry_entry
        test_successful_message_has_no_retry_entry（反向判据）
缓存：web/agent.js 内容变了 -> ?v= cr20261115b -> cr20261116b

【缺陷 3 · 三个 Agent 接口缺「请求体必须是对象」检查】
POST /api/agent/chat、/api/agent/chat/stream、/api/agent/memory
传合法 JSON 数组时 payload.get(...) 抛 AttributeError -> 500 +
aiohttp 默认错误页（不是本项目 JSON 契约）+ Python 栈信息泄漏。
修：三处补 isinstance(payload, dict) 检查 -> 400 + {error, code}。
新回归：TestNonObjectPayloadContract（6 条）

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
# 【加固 3】set -e 下 grep 无匹配会静默退出整个脚本 —— 一律 `|| true`
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
         /api/agent/tools /api/agent/settings /api/agent/sessions /api/agent/memory; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "回滚点     = backup/live-before-agentaudit-${TS}"
