#!/usr/bin/env bash
# 「创意内容设计」面板上线 8190（2026-11-21）
#
# 与既有部署脚本（deploy_cr6_8190.sh / deploy_enh_assets_8190.sh）同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全闸门）
#   * 部署后完整性自检（逐个核文件在位 + 工作区与 HEAD 一致）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
WORK=/root/projects/h3studio-creative-prompt
BRANCH=local/feature/creative-prompt-design
PORT=8190
TS=$(date +%Y%m%d-%H%M%S)

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "本轮分支    = $(git rev-parse "$BRANCH")"
systemctl is-active h3-studio.service
PID_BEFORE=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启前 PID=$PID_BEFORE"

# 【并发安全】live 必须仍停在本轮开始时看到的状态。
# 别人推进了就中止 —— 不能覆盖别人的部署。
# 2026-11-21 复核：本轮合并前 maintainer 与 live 都在 97e7294
# （移动端导航第三轮真机全矩阵复测报告）。本分支已把 maintainer 并进来了
# （merge commit a430683），所以这里对齐 97e7294。
EXPECT_LIVE="97e729486b0b925bfb85d4cc5efb038ad1895c60"
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ]; then
  echo "!! live 分支已被别人推进（$LIVE_BEFORE != $EXPECT_LIVE），中止部署"
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

git branch "backup/live-before-cpd-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-cpd-${TS}" local/maintainer
echo "回滚点：backup/live-before-cpd-${TS} / backup/maintainer-before-cpd-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cpd_mergetree.txt
CONFLICTS=$(grep -c '<<<<<<<' /tmp/cpd_mergetree.txt || true)
if [ "$CONFLICTS" != "0" ]; then
  echo "!! 冲突，中止"; cat /tmp/cpd_mergetree.txt; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(head -1 /tmp/cpd_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(creative-prompt): 「创意内容设计」面板 —— 选项目录 + 原始提示词组装

基线：97e7294（移动端导航第三轮真机全矩阵复测，= 合并前 local/maintainer
       与 local/deploy/qwen-ref-assets-live 的共同 HEAD）
合并：local/feature/creative-prompt-design

【A. 选项目录数据源】
  h3studio/prompt_catalog.py：9 维度 / 48 子类 / 891 项
  （调研原文 871 项逐条保留 + 补 20 个显式「不指定」项）；
  三级依据标注（通用 625 / 需实测 70 / 标签体系词 176）全部保留；
  标签体系词保留 Danbooru 官方 post_count 实测值（166 项 > 0）；
  D9 冲突矩阵元数据（48 条规则原文）随目录下发；
  GET /api/prompt-catalog（版本号 = 内容 sha256 前 12 位）。

【B. 面板】web/prompt-catalog.js + web/prompt-catalog.css（独立文件）
  45 个可选分组，可折叠 + 搜索；人物从头到脚逐级可选可跳过；
  前景 / 中景 / 远景独立可选；实时「原始提示词」预览；
  每个分组都有「不指定」默认项 + 自由文本入口（中文示例占位）。

【C. 组装与写回】骨架 主体→造型→风格→场景→构图→光→色→氛围→质量；
  **中文优先**：默认组装/预览/写回都走中文片段（zh_fragment），
  英文片段只作辅助（「复制含英文片段版本」），不参与默认组装；
  自定义输入按所属子类的骨架槽位参与组装，不做任何校验拦截；
  逗号分隔，不含 (x:1.5) 权重语法；「不指定」真的不输出；
  输入框已有文本时给「覆盖 / 追加」二选一（内联，不用 fixed 遮罩）；
  写回记为 level 0，不打「已增强」徽标 ⇒ 不绕过未增强拦截。

【D. 冲突】硬互斥 22 条阻止（禁用 + 说明原因）；软冲突 17 条黄字提示；
  数量上限 3 条（D1 风格族 ≤2 / D8.4 画质 ≤2 / D5 构图三族互斥）。

【测试】tests/test_prompt_catalog.py（35 用例）
        tests/e2e/test_prompt_catalog_flow.py（21 用例，真实浏览器）
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
for f in $CHANGED; do
  mkdir -p "$LIVE/$(dirname "$f")"
  git show "$MERGE_COMMIT:$f" > "$LIVE/$f"
  echo "  -> $f"
done

echo
echo "=== 5) 重启服务 ==="
systemctl restart h3-studio.service
for i in $(seq 1 40); do
  if curl -fsS -m 3 "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    echo "服务已就绪（${i}s）"; break
  fi
  sleep 1
done
systemctl is-active h3-studio.service
PID_AFTER=$(systemctl show h3-studio.service -p MainPID --value)
echo "重启后 PID=$PID_AFTER（原 $PID_BEFORE）"

echo
echo "=== 6) 部署后自检 ==="
for f in $CHANGED; do
  if ! git -C "$LIVE" diff --quiet -- "$f" 2>/dev/null; then
    echo "  [OK] $f 与 HEAD 一致"
  else
    echo "  [!!] $f 与 HEAD 不一致"
  fi
done
# live 工作区必须与 HEAD 完全一致（tracked）
LIVE_DIRTY2=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY2" ]; then
  echo "!! live 工作区仍有差异："; echo "$LIVE_DIRTY2"; exit 1
fi
echo "live 工作区 == HEAD（无 tracked 差异）"

echo
echo "=== 7) 线上探活 ==="
echo -n "  GET /                -> "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${PORT}/"
echo -n "  GET /api/prompt-catalog -> "; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:${PORT}/api/prompt-catalog"
echo -n "  目录条目数            -> "
curl -s "http://127.0.0.1:${PORT}/api/prompt-catalog" | python3 -c "import json,sys; d=json.load(sys.stdin); c=d['counts']; print(f\"{c['items']} 项（调研 {c['research_items']} + 补跳过 {c['added_skips']}），{c['groups']} 组，{c['dimensions']} 维度，version={d['version']}\")"
echo -n "  新静态资源已上线      -> "
curl -s "http://127.0.0.1:${PORT}/" | grep -o 'prompt-catalog[^\"]*' | tr '\n' ' '; echo
echo -n "  app.js 版本号         -> "
curl -s "http://127.0.0.1:${PORT}/" | grep -o 'app.js?v=[^\"]*' | head -1

echo
echo "=== 完成 ==="
echo "回滚命令："
echo "  git update-ref refs/heads/local/maintainer $MAINTAINER_BEFORE"
echo "  git update-ref refs/heads/local/deploy/qwen-ref-assets-live $LIVE_BEFORE"
echo "  cd $LIVE && git checkout -- . && git clean -fd web/ h3studio/  # 按需"
echo "  systemctl restart h3-studio.service"
