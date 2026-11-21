#!/usr/bin/env bash
# 部署「参考标签方言」修复到 8190（2026-11-21）
#
# 与 deploy_ref_labels_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全 —— 别人推进了就中止）
#   * 部署后完整性自检 + 探活
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/fix/reflabel-syntax-20261121
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
# 别人推进了就中止 —— 绝不覆盖别人的工作。
EXPECT_LIVE=ff37ee729cd6b4bf845ab135304b243c0c918d20
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE != $EXPECT_LIVE），中止部署"
  exit 1
fi
echo "live 起始状态核对通过（$EXPECT_LIVE）"
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

git branch "backup/live-before-reflabelsyntax-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-reflabelsyntax-${TS}" local/maintainer
echo "回滚点：backup/live-before-reflabelsyntax-${TS} / backup/maintainer-before-reflabelsyntax-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/reflabelsyntax_mergetree.txt
if grep -q '<<<<<<<' /tmp/reflabelsyntax_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/reflabelsyntax_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(reflabel-syntax): 参考标签语法跟目标模型族走（修 Qwen 图片编辑「增强完提交被拒」）

【现场复验发现的跨层缺陷】
上一轮把参考标签一律写成 H3 官方的 <Picture N>，结果**参考生图
（Qwen-Image 2.1 图片编辑）被自己修坏了**：
  * 增强输出 <Picture 1> / <Picture 2>；
  * 但 Qwen 官方 PE-I2I 的占位语法是 <image1> / <image2>
    （ref_images.PLACEHOLDER_RE = /<image(\d+)>/）；
  * 提交侧 validate_refs() 判「有 2 张参考图，提示词里必须有
    <image1>..<image2> 占位标记」-> HTTP 400 ref_invalid；
    用户的表现是「我点了增强，增强出来的提示词提交不了」。

线上实测复现（8190，i2i_qwen，2 张参考图）：
  POST /api/enhance -> 200，reference_labels.found=["<Picture 1>","<Picture 2>"]
  POST /api/jobs    -> 400 ref_invalid「提示词里必须有 <image1>..<image2>」

【根因】不同模型族的官方参考标记语法不是同一套：
  * H3 视频（t2v/flf/r2v/longfilm）-> <Picture N>/<Video N>/<Audio N>
  * Qwen-Image 2.1 图片编辑（i2i_qwen）-> <imageN>（小写、无空格）
  * MiniMax-H3-Image 图片编辑（i2i_h3image）-> <Picture N>（与 Qwen 隔离）

【改动】
  * h3studio/enhance_assets.py：引入标签方言（LABEL_DIALECT_H3/_QWEN），
    label_for / label_pattern / has_reference_label /
    extract_reference_labels 按方言收紧；新增 normalize_label /
    missing_labels（归一化比较）；describe() 在 Qwen 方言下讲清写法
  * server.py：_label_dialect_for_mode() 按面板选方言；解析素材与
    校验输出标签都传方言
  * tests/test_reflabel_dialect.py（新增 21 条）：含「用提交侧真正的
    校验函数做端到端口径」断言
  * scripts/mutate_reflabel_dialect.py（新增）：9 个变异全部被杀死

【无素材面板不回归】describe({}) 仍返回空串；dialect 默认 H3。
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
git checkout "$MERGE_COMMIT" -- scripts/deploy_reflabel_syntax_8190.sh 2>/dev/null || true
# 【中文文件名必须用 NUL 分隔传给 git checkout】
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 git checkout "$MERGE_COMMIT" --)
echo

echo "=== 5) 部署后完整性自检 ==="
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/reflabelsyntax_missing.txt
MISSING=$(cat /tmp/reflabelsyntax_missing.txt)
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
sleep 10
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER（原 $PID_BEFORE）"

echo
echo "=== 7) 探活 ==="
for p in "/" "/api/jobs" "/api/image-options" "/api/generation-types" "/api/enhancer"; do
  code=$(curl -s -m 60 -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}")
  echo "  $p -> $code"
  [ "$code" = "200" ] || { echo "!! $p 返回 $code"; exit 1; }
done

echo
echo "=== 8) 部署后的方言实测（i2i_qwen 必须出 <imageN>）==="
AID=$(curl -s -m 60 "http://127.0.0.1:${PORT}/api/assets?limit=50&category=image" \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print((d.get('items') or d.get('assets') or [{}])[0].get('id',''))")
if [ -n "$AID" ]; then
  curl -s -m 600 -X POST "http://127.0.0.1:${PORT}/api/enhance" \
    -H 'Content-Type: application/json' \
    -d "{\"prompt\":\"把背景换成雪山\",\"mode_ctx\":\"i2i_qwen\",\"asset_refs\":[{\"asset_id\":\"$AID\",\"slot\":\"ref_image\",\"ordinal\":0}]}" \
    | python3 -c "
import json,sys
d=json.load(sys.stdin)
rl=d.get('reference_labels') or {}
print('  reference_labels =', json.dumps(rl, ensure_ascii=False))
print('  assets.labels    =', json.dumps((d.get('assets') or {}).get('labels'), ensure_ascii=False))
"
fi

echo
echo "=== 部署完成 ==="
echo "合并提交 = $MERGE_COMMIT"
echo "回滚点   = backup/live-before-reflabelsyntax-${TS} / backup/maintainer-before-reflabelsyntax-${TS}"
