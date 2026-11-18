#!/usr/bin/env bash
# 部署「增强必须带上已引用素材」修复到 8190（2026-11-18）
#
# 与 deploy_cr6_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/fix-enhance-assets
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
EXPECT_LIVE=b2c382d3c389edfaa9880e0d838e6803d535c169
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE != $EXPECT_LIVE），中止部署"
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

git branch "backup/live-before-enhassets-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-enhassets-${TS}" local/maintainer
echo "回滚点：backup/live-before-enhassets-${TS} / backup/maintainer-before-enhassets-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/enhassets_mergetree.txt
if grep -q '<<<<<<<' /tmp/enhassets_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/enhassets_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(enh-assets): 修复「增强不带素材」—— 提示词增强必须把已引用素材传给模型

【缺陷】参考生图 / 首尾帧 / 参考生视频 / 图片编辑面板里，用户已从素材库
引用了图片并输入提示词，点「增强」得到的提示词**完全没有基于素材**。
根因：前端调 /api/enhance 时从不带素材；服务端的 images 参数是「浏览器
本地路径列表」，服务端根本读不到 —— 这条参数实际是死的。

【修法】
  * 新增 h3studio/enhance_assets.py：把素材引用（asset_id）解析成
    **服务端可读**的图片 —— 图片直给 / 视频抽帧 / 音频明确降级为文字。
  * /api/enhance 接收 asset_refs，解析后喂给多模态模型，并把图序语义
    （<imageN> = 首帧「xxx」…）写进提示词。
  * web/enhance-menu.js：普通增强 / 二次增强 / 视频导演稿三条路径都把
    **当前面板已引用的素材**一并提交（按当前模式过滤槽位，不带幽灵引用）。
  * web/providers.js：增强结果 meta 行显示素材上下文 —— 用户能确认
    素材到底参没参与。
  * 显式指定了非多模态 Provider 时**提前**报错，不静默丢素材。

【不回归】无素材面板（文生图 / 文生视频）asset_refs 为空数组 ->
提示词与改动前逐字一致（有用例锁住）。

【测试】新增 tests/test_enhance_assets.py（21 用例，含 4 个真实浏览器
用例 + 2 轮变异自证）。
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
if [ -f "$LIVE/scripts/deploy_enh_assets_8190.sh" ]; then
  git checkout "$MERGE_COMMIT" -- scripts/deploy_enh_assets_8190.sh 2>/dev/null || true
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
     done) > /tmp/enhassets_missing.txt
MISSING=$(cat /tmp/enhassets_missing.txt)
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
echo "--- 静态资源版本号已升 ---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(enhance-menu|providers)\.js\?v=[^"]+' | sort -u
echo "--- 增强接口带素材：素材缺失时必须如实报错（不能静默忽略）---"
curl -s -X POST "http://127.0.0.1:${PORT}/api/enhance" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"测试","asset_refs":[{"asset_id":"asset_NOTEXIST000","slot":"ref_image"}]}'
echo
echo "--- 增强接口带素材：正常素材应返回 assets 回显 ---"
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
  curl -s -X POST "http://127.0.0.1:${PORT}/api/enhance" \
    -H 'Content-Type: application/json' \
    -d "{\"prompt\":\"一个女孩站在雪地里\",\"asset_refs\":[{\"asset_id\":\"$REAL_ASSET\",\"slot\":\"ref_image\"}]}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('  assets =', json.dumps(d.get('assets'), ensure_ascii=False)); print('  used_multimodal =', d.get('used_multimodal')); print('  enhanced[:120] =', (d.get('enhanced') or '')[:120])"
else
  echo "  （素材库里没有图片素材，跳过）"
fi

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-enhassets-${TS} / backup/maintainer-before-enhassets-${TS}"
