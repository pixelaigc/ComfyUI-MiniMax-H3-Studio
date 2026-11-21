#!/usr/bin/env bash
# 增量合并：把部署脚本修正 + 线上探活证据并入 local/maintainer 与 live。
# 纪律同 deploy_creative_prompt_8190.sh：commit-tree + update-ref、
# 只精确 checkout 本次改动的文件、只刷新 index（不动文件内容）、
# 并发闸门（live 必须在预期 HEAD，否则中止）。
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/creative-prompt-design
PORT=8190

cd "$REPO"

echo "=== 0) 现状 ==="
MAINTAINER_BEFORE=$(git rev-parse local/maintainer)
LIVE_BEFORE=$(git rev-parse local/deploy/qwen-ref-assets-live)
echo "maintainer = $MAINTAINER_BEFORE"
echo "live       = $LIVE_BEFORE"
echo "本轮分支    = $(git rev-parse "$BRANCH")"

EXPECT_LIVE="46632454a63988f98eb4054e4556bfeecc87e716"
if [ "$LIVE_BEFORE" != "$EXPECT_LIVE" ]; then
  echo "!! live 已被别人推进（$LIVE_BEFORE != $EXPECT_LIVE），中止"
  exit 1
fi
if [ "$MAINTAINER_BEFORE" != "$LIVE_BEFORE" ]; then
  echo "!! maintainer 与 live 不一致，中止"
  exit 1
fi
LIVE_DIRTY=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY" ]; then
  echo "!! live 工作区有 tracked 改动，中止："; echo "$LIVE_DIRTY"; exit 1
fi

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/cpd_mt2.txt
CONFLICTS=$(grep -c '<<<<<<<' /tmp/cpd_mt2.txt || true)
if [ "$CONFLICTS" != "0" ]; then
  echo "!! 冲突，中止"; cat /tmp/cpd_mt2.txt; exit 1
fi
echo "无冲突"

MERGE_TREE=$(head -1 /tmp/cpd_mt2.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
merge(creative-prompt): 部署脚本两处真错修正 + 线上 8190 探活证据

【部署脚本真错（实测踩到）】
1) 自检判据写反：`git diff --quiet` 返回 0 = 无差异 = 一致，
   原脚本写成「不一致时报 OK」⇒ 判据完全颠倒。已翻转。
2) 用 `git show > 文件` 写 live 文件**没刷新 live 的 git index**，
   ⇒ git status 把每个文件报成 D，自检误判部署失败。
   已补 `git reset --mixed HEAD`（只刷 index、不动工作区内容）。

【线上 8190 探活（部署后实测）】
GET / -> 200；GET /api/prompt-catalog -> 200
counts: 891 项（调研 871 + 补跳过 20）/ 48 组 / 9 维度 / tag_verified=166
version=f6dcce209bfa；index.html 已引用 prompt-catalog ?v=cpd20261121c
真实 Chromium 打线上：45 组、组装 ['人物（单人）','赛璐璐动画','远景是雾中的高压电塔']、
硬互斥可见说明存在且可见、375px 无溢出、页面 JS 错误 []
MSG
)
echo "合并提交 = $MERGE_COMMIT"

echo
echo "=== 2) 更新分支指针 ==="
git update-ref refs/heads/local/maintainer "$MERGE_COMMIT" "$MAINTAINER_BEFORE"
git update-ref refs/heads/local/deploy/qwen-ref-assets-live "$MERGE_COMMIT" "$LIVE_BEFORE"

echo
echo "=== 3) 精确 checkout 本次改动的文件 ==="
CHANGED=$(git diff --name-only "$LIVE_BEFORE" "$MERGE_COMMIT")
echo "$CHANGED" | sed 's/^/  /'
for f in $CHANGED; do
  mkdir -p "$LIVE/$(dirname "$f")"
  git show "$MERGE_COMMIT:$f" > "$LIVE/$f"
  echo "  -> $f"
done
cd "$LIVE" && git reset --mixed HEAD >/dev/null && cd "$REPO"

echo
echo "=== 4) 自检 ==="
for f in $CHANGED; do
  if git -C "$LIVE" diff --quiet -- "$f" 2>/dev/null; then
    echo "  [OK] $f 与 HEAD 一致"
  else
    echo "  [!!] $f 与 HEAD 不一致"
  fi
done
LIVE_DIRTY2=$(cd "$LIVE" && git status --porcelain | grep -v '^??' || true)
if [ -n "$LIVE_DIRTY2" ]; then
  echo "!! live 工作区仍有差异："; echo "$LIVE_DIRTY2"; exit 1
fi
echo "live 工作区 == HEAD"

echo
echo "=== 5) 探活 ==="
curl -s -o /dev/null -w "GET / -> %{http_code}\n" "http://127.0.0.1:${PORT}/"
curl -s -o /dev/null -w "GET /api/prompt-catalog -> %{http_code}\n" \
  "http://127.0.0.1:${PORT}/api/prompt-catalog"
echo
echo "回滚命令："
echo "  git update-ref refs/heads/local/maintainer $MAINTAINER_BEFORE"
echo "  git update-ref refs/heads/local/deploy/qwen-ref-assets-live $LIVE_BEFORE"
