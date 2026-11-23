#!/usr/bin/env bash
# 部署「媒体上下文一致性（A/B/C/D）」到 8190（2026-11-23）
#
# 与 deploy_enh_assets_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/prompt-enhance-media-ctx
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "待部署分支  = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# 【并发安全】live 必须仍停在本轮开始时看到的状态 —— 别人推进了就中止。
EXPECT_LIVE=f66cb39
LIVE_FULL=$(git rev-parse local/deploy/qwen-ref-assets-live)
if [ "${LIVE_FULL:0:7}" != "$EXPECT_LIVE" ]; then
  echo "!! live 分支已被别人推进（$LIVE_FULL 不是 $EXPECT_LIVE 前缀），中止部署"
  exit 1
fi
if [ "$MAINTAINER_BEFORE" != "$LIVE_BEFORE" ]; then
  echo "!! maintainer 与 live 不一致（$MAINTAINER_BEFORE vs $LIVE_BEFORE），中止部署"
  exit 1
fi

# 【live 工作区干净性】只允许未跟踪文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

git branch "backup/live-before-mediactx-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-mediactx-${TS}" local/maintainer
echo "回滚点：backup/live-before-mediactx-${TS} / backup/maintainer-before-mediactx-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/mediactx_mergetree.txt
if grep -q '<<<<<<<' /tmp/mediactx_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/mediactx_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(media-ctx): 补齐与 nonebot 咏唱h3 的媒体上下文一致性（A/B/C/D）

【背景】上一轮只读核查确认：本仓库比 nonebot「咏唱h3」弱在四处媒体
上下文 —— nonebot 把素材的**本地绝对路径**写进 hermes query（让它自己
vision_analyze / ffprobe 读），本仓库只给「标签语义文字」。

【A】素材绝对路径进 hermes query
  * h3studio/hermes_enhance.py：build_query() 新增 media_paths 参数 +
    「# 素材位置」块，格式与 nonebot h3_interactive.py:4346-4361 逐字对齐
  * 合成阶段仍保留「不要用 vision_analyze 读参考图」省时指令（nonebot 4370-4377）

【B】音频 librosa 结构化数值
  * 新增 h3studio/audio_features.py：忠实移植 nonebot h3_audio_features.py
    （BPM / 调式 / 段落 / 能量 / 频谱 / 质感 / 人声代理 / 风格族）
  * h3studio/enhance_assets.py：数值分析 + 失败降级 + 如实告知

【C】Agent 路径带媒体上下文
  * h3studio/agent_enhance.py：_resolve_agent_media() 复用服务端素材解析
  * h3studio/agent.py：建任务时把 asset_refs 传给增强管线
  * 素材解析放在开关检查之后 —— hermes 关闭时零额外开销

【D】视频回传整段 mp4 路径（不是只给抽帧）

【安全口径】与 nonebot 一致 —— 只给路径，不传 base64 / 二进制；
路径必须真实存在（collect_media_paths 逐条校验），前端只能给 asset_id。

【实测】图像 200/synth/1492字；音频 200/synth/3371字；视频超时但 hermes
确实去逐帧读了 mp4（20 处工具调用痕迹）—— 均证明路径生效。
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
# 【部署脚本把自己也带上】
if [ -f "$LIVE/scripts/deploy_media_ctx_8190.sh" ]; then
  git checkout "$MERGE_COMMIT" -- scripts/deploy_media_ctx_8190.sh 2>/dev/null || true
fi
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 git checkout "$MERGE_COMMIT" --)
echo

echo "=== 5) 部署后完整性自检 ==="
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/mediactx_missing.txt
MISSING=$(cat /tmp/mediactx_missing.txt)
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
for p in "/" "/api/jobs" "/api/image-options" "/api/generation-types" "/api/enhancer"; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 线上关键行为复验 ==="
echo "--- hermes 配置下发（含 A/B/C/D 相关字段）---"
curl -s "http://127.0.0.1:${PORT}/api/enhancer" | python3 -c "
import json,sys
d=json.load(sys.stdin)
h=d.get('hermes') or {}
print('  hermes.enabled      =', h.get('enabled'))
print('  hermes.agent_enabled=', h.get('agent_enabled'))
print('  hermes.profile      =', h.get('profile'))
print('  hermes.binary       =', h.get('binary'))
"
echo "--- 新增模块在位 ---"
for f in h3studio/hermes_enhance.py h3studio/enhance_assets.py h3studio/item_scan.py h3studio/audio_features.py; do
  if [ -f "$LIVE/$f" ]; then echo "  OK  $f"; else echo "  MISSING $f"; exit 1; fi
done
echo "--- A/D：素材位置块在代码里（在线上文件上直接 grep）---"
grep -c "参考图{i}\|参考视频{i}\|参考音频{i}" "$LIVE/h3studio/hermes_enhance.py" || true
echo "--- B：音频数值模块在位 ---"
python3 - <<'PY'
import sys
sys.path.insert(0, "/root/projects/h3studio-live")
from h3studio import audio_features as AF
need = ["analyze_audio_numeric", "features_to_prompt_block", "detect_tempo",
        "detect_key", "detect_structure"]
missing = [n for n in need if not hasattr(AF, n)]
print("  audio_features 关键函数缺失:", missing or "无")
PY
echo "--- hermes 增强端点：禁用时应如实报错（不能静默）---"
curl -s -X POST "http://127.0.0.1:${PORT}/api/hermes-enhance" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"测试","media":"image"}' | head -c 300
echo

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-mediactx-${TS} / backup/maintainer-before-mediactx-${TS}"
