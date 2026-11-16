#!/usr/bin/env bash
# MOSS-Image / 图像族采样参数量程修复 —— 部署到 8190（2026-11-16）
#
# 与 deploy_cr5_8190.sh 同纪律：
#   * git commit-tree + update-ref（不切分支、不碰 live 的未跟踪文件）
#   * 只精确 checkout 本次改动的文件
#   * 只重启 h3-studio.service
#   * 合并前重新核实 live 现状（并发安全）
#   * 部署后逐个核「本轮文件都在工作区」（漏一个会被 git 当成删除）
set -euo pipefail

REPO=/root/projects/comfyui-minimax-h3-studio
LIVE=/root/projects/h3studio-live
BRANCH=local/feature/image-param-ranges-20261116
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

git branch "backup/live-before-imgparams-${TS}" local/deploy/qwen-ref-assets-live
git branch "backup/maintainer-before-imgparams-${TS}" local/maintainer
echo "回滚点：backup/live-before-imgparams-${TS} / backup/maintainer-before-imgparams-${TS}"

echo
echo "=== 1) 预演合并 ==="
git merge-tree --write-tree local/deploy/qwen-ref-assets-live "$BRANCH" > /tmp/imgparams_mergetree.txt
if grep -q '<<<<<<<' /tmp/imgparams_mergetree.txt; then
  echo "!! 冲突，中止"; exit 1
fi
echo "无冲突"

echo
echo "=== 2) 造合并提交 ==="
MERGE_TREE=$(head -1 /tmp/imgparams_mergetree.txt)
MERGE_COMMIT=$(git commit-tree "$MERGE_TREE" \
  -p local/deploy/qwen-ref-assets-live \
  -p "$BRANCH" \
  -F - <<'MSG'
fix(MOSS-Image): 采样参数量程前后端同源 —— 面板 steps=100 不再被夹回 50

【根因 —— 「前端允许、后端不允许」】
前端滑杆量程按模型族分别硬编码（MOSS steps 1~100 / cfg 0~10 / shift 0~10），
后端归一层 h3studio/gen_normalize.py 却用一套**扁平的全局区间**
（IMAGE_STEPS_MAX=60 / IMAGE_CFG_MIN=1.0 / IMAGE_SHIFT_MIN=1.0 /
IMAGE_SHIFT_MAX=7.0）去卡所有族。于是：
  * MOSS-Image steps=100 -> 后端判越界 -> 静默回落模型推荐 50
    （线上实测复现：落库 steps=50 + normalize_notes 记录了改动）；
  * Qwen-Image shift=0.69（官方内置值）-> 被判越界 -> 回落 1.0；
  * Qwen cfg=0 / H3-Image shift=0 -> 同样被回落。
两套量程各写一份，必然漂移 —— 这就是本次用户报的 BUG。

【修复点 1 · 量程单一真源】
h3studio/gen_normalize.py 新增 IMAGE_PARAM_RANGES（按 zimage/qwen/moss/
h3image 四族），前后端共用：
  * 后端 normalize_image_sampling() 按族取量程判越界；
  * 服务端 /api/image-options 下发 param_ranges；
  * 前端 web/app.js 删掉四套硬编码，改读服务端下发。
前端**不再有任何第二份量程表**（没有兜底表 —— 两份量程必然漂移）。
服务端没下发时保持 index.html 里 range 标签自带的量程不动
（那套恰好等于旧后端的全局区间，版本不一致期间也不会错配）。

【修复点 2 · Z-Image 放大分支不再写死采样参数】
h3studio/workflows.py 的 _z_image_upscale_nodes() 把第一段 KSampler 写死
steps=8 / cfg=1.0 / res_multistep / simple -> >=3MP 档位上用户改的
步数/CFG/采样器/调度器/shift 全部无效。现在第一段用用户参数；
第二段（57:75）是官方放大链的固定工艺参数，保持不变。

【修复点 3 · 归一说明不再静默】
h3studio/job_meta.py 的 gen_params() 下发 normalize_notes，
web/app.js 的详情弹层新增「参数已调整」一行。
此前该说明只落库、前端从不读 -> 「参数被服务端改过」这件事完全看不出来。

【测试】
新增 tests/test_image_param_ranges.py（27 用例）
新增 scripts/mutation_image_param_ranges.py（10 个变异全部被抓住）
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
echo "=== 4) live worktree 精确 checkout ==="
cd "$LIVE"
git checkout "$MERGE_COMMIT" -- \
  h3studio/gen_normalize.py \
  h3studio/h3image.py \
  h3studio/job_meta.py \
  h3studio/workflows.py \
  server.py \
  web/app.js \
  web/index.html \
  tests/test_image_param_ranges.py \
  scripts/mutation_image_param_ranges.py \
  scripts/verify_moss_steps_live.py \
  scripts/deploy_imgparams_8190.sh
CHANGED=$(git -C "$LIVE" status --porcelain | grep -v '^??' || true)
if [ -n "$CHANGED" ]; then
  echo "$CHANGED" | head -20
  echo "(上面应只列出本轮改的文件 = 部署成功)"
else
  echo "(无 tracked 改动 —— 文件内容与 HEAD 已一致)"
fi
echo "live HEAD = $(git -C "$LIVE" rev-parse HEAD)"

echo
echo "=== 部署完整性自检 ==="
MISSING=0
for f in h3studio/gen_normalize.py h3studio/h3image.py h3studio/job_meta.py \
         h3studio/workflows.py server.py web/app.js web/index.html \
         tests/test_image_param_ranges.py \
         scripts/mutation_image_param_ranges.py \
         scripts/verify_moss_steps_live.py \
         scripts/deploy_imgparams_8190.sh; do
  if [ ! -f "$LIVE/$f" ]; then
    echo "  缺失: $f"
    MISSING=$((MISSING + 1))
  fi
done
if [ "$MISSING" -eq 0 ]; then
  echo "  本轮 10 个文件全部就位"
else
  echo "  !! 缺失 $MISSING 个文件 —— 需要补 checkout"
  exit 1
fi
if [ -n "$(git -C "$LIVE" diff --name-only HEAD)" ]; then
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
for p in / /api/jobs /api/queue /api/video-options /api/image-options /api/generation-types; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}${p}" || echo 000)
  echo "  ${p} -> ${CODE}"
done

echo
echo "=== 6) 关键断言：param_ranges 已下发 ==="
curl -s "http://127.0.0.1:${PORT}/api/image-options" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); r=d.get('param_ranges') or {}; print('families:', sorted(r.keys())); print('moss.steps:', (r.get('moss') or {}).get('steps'))"

echo
echo "=== 7) 汇总 ==="
echo "maintainer = $(git -C "$REPO" rev-parse local/maintainer)"
echo "live       = $(git -C "$REPO" rev-parse local/deploy/qwen-ref-assets-live)"
echo "回滚点     = backup/live-before-imgparams-${TS}"
