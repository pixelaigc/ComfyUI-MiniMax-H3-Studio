#!/usr/bin/env bash
# 部署「参考素材右键移除」到 8190（2026-11-20）
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
BRANCH=local/feature/ref-context-remove
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
EXPECT_LIVE=42b0a0dfc1964f57f9e824262da6462246ecdabd
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

git branch "backup/live-before-refctx-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-refctx-${TS}" local/maintainer
echo "回滚点：backup/live-before-refctx-${TS} / backup/maintainer-before-refctx-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/refctx_mergetree.txt
if grep -q '<<<<<<<' /tmp/refctx_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(cat /tmp/refctx_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
feat(参考素材): 补齐「右键 -> 移除」—— 已添加的参考素材可被删除

【缺陷】
参考素材（参考图 / 参考视频 / 参考音乐 / 首尾帧 / 参考音频）被添加到
面板后**没有任何移除入口**：
  * 视频面板的素材槽位（.up-slot）完全没有移除入口 —— 填满后想换掉
    其中一条，只能去素材库删素材，或者切模式清空整块；
  * Qwen / H3-Image 的参考图只有缩略图右上角那个很小的 × 按钮。

【修法】
  * 新增 web/ref-context-menu.js：一份共享的右键菜单实现（显示 / 定位 /
    关闭），三个模块共用，避免各写一份必然漂移。
  * 新增 web/ref-slots.js：视频面板槽位的「同组前移 + 占位标记重写」
    纯逻辑（可单测）。
  * web/app.js：视频面板素材槽位（首帧 / 尾帧 / 参考图 / 参考视频 /
    参考音乐）已填时右键 -> 菜单 -> 移除。
  * web/qwen-refs.js：参考图缩略图右键 -> 菜单 -> 移除
    （复用已有 removeAt：它已经做了重编号 + 占位标记清理）。
  * web/music.js：参考生音乐的参考音频条目右键 -> 菜单 -> 移除。

【移除后必须同步的三件事】
  1. 参考素材列表本身少一条；
  2. **同组**后面的素材前移（编号保持连续）—— 否则「参考 1 / 参考 3」
     这种空洞编号会让送进模型的图序与界面编号对不上；
  3. 提示词里的占位标记同步重写：被移除那条的标记**删掉**（不留悬空
     <Picture N> / <Video N> / <Audio N>），其余按新序号单遍重写。
     【为什么必须】H3 视频的 T8 条件节点在 strict 模式下对悬空标记是
     **硬失败**：「<Picture 2> is not connected; available picture count
     is 1」。

【不回归】
  * 素材库闸门 / 绝对路径拦截 / 种子校验：一条没碰（本次只动前端）。
  * Qwen 图片编辑面板的 <imageN> 由 qwen-refs.js 单独管理，视频模式的
    重写**只换数字不改写法**（"<image1>" 不会被改成 "<Picture 1>" ——
    那样切回 Qwen 模式后 Qwen 的正则扫不到，合法提交会被误拒）。
  * 槽位状态的清空 / 回填语义没变（沿用 renderModeKeepSlots）。

【测试】新增 tests/test_ref_context_remove.py（19 用例：14 条纯逻辑 +
5 条真实浏览器），并做过 3 轮变异自证（去掉同组前移 / 去掉占位重写 /
去掉 Qwen 面板挂菜单 —— 都被断言抓住）。
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
# 【中文文件名必须用 NUL 分隔传给 git checkout】
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && xargs -0 git checkout "$MERGE_COMMIT" --)
echo

echo "=== 5) 部署后完整性自检 ==="
git diff --name-only -z "$LIVE_BEFORE" "$MERGE_COMMIT" \
  | (cd "$LIVE" && while IFS= read -r -d '' f; do
       [ -z "$f" ] && continue
       [ -e "$f" ] || echo "MISSING:$f"
     done) > /tmp/refctx_missing.txt
MISSING=$(cat /tmp/refctx_missing.txt)
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
echo "--- 右键菜单容器与脚本已在位 ---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE 'id="refContextMenu"' | head -1
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(ref-context-menu|ref-slots)\.js\?v=[^"]+' | sort -u
echo "--- 两个新模块能取到（不能 404 / 不能 JS 语法错）---"
for m in ref-context-menu.js ref-slots.js; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/static/${m}?v=refctx1")
  echo "  /static/$m -> $code"
  [ "$code" = "200" ] || { echo "!! $m 取不到"; exit 1; }
done
echo "--- 静态资源版本号已升 ---"
curl -s "http://127.0.0.1:${PORT}/" | grep -oE '/static/(app|qwen-refs|music)\.js\?v=[^"]+' | sort -u

echo
echo "=== 9) 服务日志尾部 ==="
journalctl -u h3-studio.service --since "-3min" --no-pager | tail -20

echo
echo "=== 部署完成 ==="
echo "merge commit = $MERGE_COMMIT"
echo "回滚点：backup/live-before-refctx-${TS} / backup/maintainer-before-refctx-${TS}"
