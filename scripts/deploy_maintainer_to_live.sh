#!/usr/bin/env bash
# 把主线的改动安全同步到线上 worktree（合并 + 部署 + 探活）。
#
# 【为什么需要这个脚本】
# 反复踩到同一个坑：把文件 cp 进 live 之后，git merge 会因「未跟踪文件
# 将被合并覆盖」而失败；而直接 `rm -f` 又会误删**已跟踪**文件
# （实测误删了 tools/compare_regression.py 与 3 个 evidence JSON）。
#
# 【本脚本的安全规则】
#   1. 合并前：**只**处理「工作区有本地改动」的文件 ——
#      逐个核对 live 版本与主线版本的 md5：
#        * 一致  -> `git checkout --` 恢复（让主线接管）
#        * 不一致 -> **停下报错**（不自动覆盖别人的改动）
#      未跟踪文件：md5 一致才删（否则停下报错）。
#   2. 合并后：`git diff local/maintainer HEAD` 必须为 0 行。
#   3. 重启服务 + 六接口探活。
#
# 用法：bash scripts/deploy_maintainer_to_live.sh [--no-restart]
set -u

LIVE="${LIVE:-/root/projects/h3studio-live}"
MAIN="local/maintainer"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

cd "$LIVE" || { echo "cd $LIVE 失败"; exit 1; }

echo "═══ 1. 核对工作区本地改动 ═══"
BLOCKERS=0
for f in $(git -c core.quotePath=false diff --name-only); do
  LIVE_MD5=$(md5sum "$f" 2>/dev/null | cut -d' ' -f1)
  MAIN_MD5=$(git show "$MAIN:$f" 2>/dev/null | md5sum | cut -d' ' -f1)
  if [ -z "$LIVE_MD5" ] || [ -z "$MAIN_MD5" ]; then
    echo "  [!! 无法计算 md5] $f -> 需人工处理"
    BLOCKERS=1
  elif [ "$LIVE_MD5" = "$MAIN_MD5" ]; then
    echo "  [一致] $f -> 恢复给主线接管"
    git checkout -- "$f"
  else
    echo "  [!! 不一致] $f  live=$LIVE_MD5 main=$MAIN_MD5  -> 不自动覆盖，需人工处理"
    BLOCKERS=1
  fi
done
# 未跟踪文件：只处理「主线也有同名文件」的
for f in $(git -c core.quotePath=false ls-files --others --exclude-standard); do
  if git cat-file -e "$MAIN:$f" 2>/dev/null; then
    LIVE_MD5=$(md5sum "$f" 2>/dev/null | cut -d' ' -f1)
    MAIN_MD5=$(git show "$MAIN:$f" | md5sum | cut -d' ' -f1)
    if [ "$LIVE_MD5" = "$MAIN_MD5" ]; then
      echo "  [未跟踪但一致] $f -> 移除（主线会带回来）"
      rm -f "$f"
    else
      echo "  [!! 未跟踪且不一致] $f  -> 不自动删除"
      BLOCKERS=1
    fi
  fi
done
[ "$BLOCKERS" = "1" ] && { echo "有阻断项，停止。"; exit 2; }

echo "═══ 2. 合并主线 ═══"
git merge --no-ff "$MAIN" -m "merge(deploy): 回灌 $MAIN" || { echo "合并失败"; exit 3; }

echo "═══ 3. 校验 ═══"
# 【口径】**生产代码**必须与主线逐字节一致；docs/ 允许有差异 ——
# live 可能保留了「只属于线上分支」的历史记录（例如外层复核补记），
# 那不是「未同步的改动」，而是 live 自己的历史。
CODE_DIFF=$(git -c core.quotePath=false diff "$MAIN" HEAD --   ':!docs' ':!evidence' ':!audits' | wc -l)
ALL_DIFF=$(git -c core.quotePath=false diff "$MAIN" HEAD | wc -l)
echo "  live vs mainline：全量 diff = $ALL_DIFF 行 / 生产代码 diff = $CODE_DIFF 行"
if [ "$CODE_DIFF" != "0" ]; then
  echo "  !! 生产代码与主线不一致，停止"
  git -c core.quotePath=false diff "$MAIN" HEAD -- ':!docs' ':!evidence' ':!audits' | head -40
  exit 4
fi
if [ "$ALL_DIFF" != "0" ]; then
  echo "  （docs/evidence 有差异 = live 的历史记录，属预期）"
  git -c core.quotePath=false diff --stat "$MAIN" HEAD
fi

if [ "$RESTART" = "1" ]; then
  echo "═══ 4. 重启 + 探活 ═══"
  systemctl restart h3-studio.service
  sleep 12
  echo "  service = $(systemctl is-active h3-studio.service)"
  FAIL=0
  for p in / /api/jobs /api/video-options /api/continuation/state \
           /api/video-config /api/status/stream; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:8190$p")
    printf "  %-26s %s\n" "$p" "$CODE"
    [ "$CODE" != "200" ] && FAIL=1
  done
  [ "$FAIL" = "1" ] && { echo "  !! 有接口非 200"; exit 5; }
fi

echo "═══ 完成 ═══"
echo "  live HEAD = $(git rev-parse --short HEAD)"
