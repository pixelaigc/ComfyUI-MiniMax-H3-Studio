#!/usr/bin/env bash
# 部署到 8190 线上：合并 -> local/maintainer -> 回灌 live -> 重启 -> 探活
#
# ⚠️ 纪律：
#   * 不碰主工作树 /root/projects/comfyui-minimax-h3-studio（那里有 29 个未提交 WIP）
#   * 只在 /tmp/h3-base（local/maintainer 的 worktree）与 /root/projects/h3studio-live 操作
#   * 重启只重启 h3-studio.service（线上本来的服务），不碰别的 unit
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
MAINTAINER_WT=/tmp/h3-base
LIVE=/root/projects/h3studio-live
FIX_BRANCH=local/feature/agent-enh-merge
PORT=8190

cd "$REPO"

echo "=== 0) 现状 ==="
git -C "$MAINTAINER_WT" log --oneline -1
git -C "$LIVE" log --oneline -1
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

echo
echo "=== 1) 合并到 local/maintainer ==="
git -C "$MAINTAINER_WT" merge --no-ff "$FIX_BRANCH" -m "merge(agent-enh-fix): 合入内置 Agent 增强 + MOSS LoRA 键形态修复 + 批量步数修复 + 遗留项修复" || {
  echo "!! 合并冲突 —— 手工处理后再跑本脚本的剩余部分"
  exit 1
}
git -C "$MAINTAINER_WT" log --oneline -1

echo
echo "=== 2) 回灌 live ==="
git -C "$LIVE" merge --no-ff "$FIX_BRANCH" -m "merge(deploy): 回灌 local/maintainer 集成结果到线上分支" || {
  echo "!! live 合并冲突"
  exit 1
}
git -C "$LIVE" log --oneline -1
git -C "$LIVE" status --porcelain | head

echo
echo "=== 3) 重启 h3-studio.service ==="
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

echo
echo "=== 4) 探活 ==="
echo "--- 首页"
curl -s -o /dev/null -w "  / -> %{http_code}\n" "http://127.0.0.1:${PORT}/"
echo "--- API"
for api in /api/system /api/jobs /api/generation-types /api/image-options \
           /api/image-loras /api/providers /api/enhancer /api/moss-layout /api/assets; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${api}")
  printf "  %-28s -> %s\n" "$api" "$CODE"
done
echo "--- 静态资源"
for f in app.js style.css index.html music.js video-panel.js qwen-refs.js \
         interaction-guard.js providers.js assets.js moss-layout.js \
         enhance-menu.js enhance-menu-shared.js music-enhance.js \
         agent.js agent-media.js responsive.css; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/static/${f}")
  printf "  %-28s -> %s\n" "$f" "$CODE"
done

echo
echo "=== 5) 增强注册表项数 ==="
curl -s "http://127.0.0.1:${PORT}/api/enhancer" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  keys:', list(d.keys())[:8])
for k in ('methods','items','entries','enhancements'):
    if k in d and isinstance(d[k], list):
        print(f'  {k} 项数:', len(d[k]))
        for it in d[k][:12]:
            print('     -', it.get('id') or it.get('label'))
"

echo
echo "=== 6) 完成 ==="
echo "maintainer HEAD: $(git -C "$MAINTAINER_WT" log --oneline -1)"
echo "live HEAD:       $(git -C "$LIVE" log --oneline -1)"
