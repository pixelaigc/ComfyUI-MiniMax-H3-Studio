#!/usr/bin/env bash
# 部署「增强必须带 H3 官方参考标签」修复到 8190（2026-11-21）
#
# 与前几轮部署脚本同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/enh-ref-labels-20261121
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
EXPECT_LIVE=dac226c43fd88dd9b825694f59e4df668996cd49
# 【2026-11-21】并发任务已经把 flf-panel 改动推上线，live 基线前移。
EXPECT_LIVE_AFTER_CONCURRENT=65b373b04c8d0cf4a102e155bc4e0b36f5c59c12
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ] && \
   [ "$LIVE_BEFORE" != "$EXPECT_LIVE_AFTER_CONCURRENT" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE 既不是 $EXPECT_LIVE 也不是 $EXPECT_LIVE_AFTER_CONCURRENT），中止部署"
  exit 1
fi
if [ "$MAINTAINER_BEFORE" != "$LIVE_BEFORE" ]; then
  # maintainer 与 live 不一致时**不中止** —— 只要 live 在允许的基线集合里
  # 就继续（maintainer 可能被另一个任务推进过）。
  echo "注意：maintainer($MAINTAINER_BEFORE) 与 live($LIVE_BEFORE) 不一致，继续（live 在允许基线内）"
fi

# 【live 工作区干净性】只允许**未跟踪**文件存在；有 tracked 改动就中止
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止部署："
  echo "$LIVE_DIRTY"
  exit 1
fi
echo "live 工作区：无 tracked 改动（只有未跟踪的证据文件）"

git branch "backup/live-before-reflabel-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-reflabel-${TS}" local/maintainer
echo "回滚点：backup/live-before-reflabel-${TS} / backup/maintainer-before-reflabel-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/reflabel_mergetree.txt
if grep -q '<<<<<<<' /tmp/reflabel_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/reflabel_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(reflabel): 增强结果必须带 H3 官方参考标签（<Picture N>/<Video N>/<Audio N>）

【缺陷】上一轮把素材传进了增强请求，但**增强后的提示词里仍然没有参考标签**
—— 2026-11-21 线上实测：两张图都真传了（used_multimodal=true、image_count=2），
模型也描述了图片内容，但输出里 `<Picture N>` / `<Subject N>` **一个都没有**。
它只是「描述了图片」却没「标注哪张图」-> 下游 H3 工作流无法把文字引用和素材
对上，素材等于白引用。

【官方依据】MiniMax H3 官方 skill h3-prompt-writing/references/ref-en.txt：
四类标签 <Subject N>/<Picture N>/<Video N>/<Audio N>，且**各类别独立编号**
（"numbered independently"），编号与面板素材顺序一一对应。

【改动】
  * h3studio/enhance_assets.py：官方标签分配（各类别独立编号）；MAX_IMAGES
    8 -> 9（对齐节点 ref_images max=9，取 8 会静默丢弃第 9 张）；
    新增视觉总预算（图片优先，分不到额度的视频如实记进 skipped）
  * h3studio/skills/prompt-enhancer/assets/h3-ref-director.txt（新增）：
    官方 Ref2VA 六段式模板
  * h3studio/enhancer.py：注册模板 + reference_mode 标记 + 参考模式不叠加通用话术
  * server.py：带素材 -> 路由到参考模式模板；**输出缺标签 -> 502 拒绝写回**
    （逐个标签查）；响应新增 reference_labels 回显
  * web/app.js：新增 panelSlotOrder() 唯一真源，**提交侧**也按面板顺序排素材
  * web/enhance-menu.js：素材收集改为按面板顺序（原来用对象插入顺序，
    会让 <Picture 1> 指到错的图）
  * web/providers.js：meta 行显示参考标签（修掉双重转义）

【测试】tests/test_enhance_ref_labels.py（28 用例，含 5 条真实浏览器用例）
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
if [ -f "$LIVE/scripts/deploy_ref_labels_8190.sh" ]; then
  git checkout "$MERGE_COMMIT" -- scripts/deploy_ref_labels_8190.sh 2>/dev/null || true
fi
# 【中文文件名必须用 NUL 分隔传给 git checkout】
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 git checkout "$MERGE_COMMIT" --)
echo

echo "=== 5) 部署后完整性自检 ==="
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/reflabel_missing.txt
MISSING=$(cat /tmp/reflabel_missing.txt)
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
echo "=== 8) 线上关键行为复验 ==="
echo "--- 静态资源版本号已升 ---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(app|providers|enhance-menu)\.js\?v=[^"]+' | sort -u

echo "--- 增强接口带素材：必须回显参考标签 ---"
REAL_ASSET=$(python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/mnt/usb2t/asset_library/asset_library.db')
c.row_factory = sqlite3.Row
row = c.execute("SELECT id FROM assets WHERE category='image' AND deleted_at IS NULL LIMIT 1").fetchone()
print(row["id"] if row else "")
PY
)
if [ -n "$REAL_ASSET" ]; then
  echo "  用素材：$REAL_ASSET"
  curl -s -m 400 -X POST "http://127.0.0.1:${PORT}/api/enhance" \
    -H 'Content-Type: application/json' \
    -d "{\"prompt\":\"镜头从人物慢慢推向远景\",\"mode_ctx\":\"flf\",\"asset_refs\":[{\"asset_id\":\"$REAL_ASSET\",\"slot\":\"first_frame\",\"ordinal\":0}]}" \
    | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
print('  reference_labels =', json.dumps(d.get('reference_labels'), ensure_ascii=False))
print('  assets          =', json.dumps(d.get('assets'), ensure_ascii=False))
e=d.get('enhanced') or ''
print('  输出里的标签      =', sorted(set(re.findall(r'<(?:Subject|Picture|Video|Audio)\s*\d+>', e))))
print('  enhanced[:200]  =', e[:200].replace(chr(10),' | '))
"
else
  echo "  （素材库里没有图片素材，跳过）"
fi

echo "--- 增强接口带素材：素材缺失时必须如实报错 ---"
curl -s -m 60 -X POST "http://127.0.0.1:${PORT}/api/enhance" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"测试","asset_refs":[{"asset_id":"asset_NOTEXIST000","slot":"ref_image"}]}'
echo

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-reflabel-${TS} / backup/maintainer-before-reflabel-${TS}"
