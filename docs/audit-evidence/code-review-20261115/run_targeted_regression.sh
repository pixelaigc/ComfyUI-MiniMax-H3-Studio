#!/usr/bin/env bash
# 针对性回归：基线（8bcc3d0）vs 候选（我的分支）逐用例对比
#
# 【为什么用「逐用例结果序列」而不是「passed/failed 总数」】
# 全量套件有 flaky 用例（test_grid_flash 等），总数对比会被噪声干扰。
# pytest -q 每个用例吐一个字符（. F E s x），逐位对齐能直接回答
# 「我这版有没有引入基线里没有的失败」。
#
# 【范围】只跑**引用了我改动文件**的测试文件（web/app.js、web/index.html、
# web/providers.js、h3studio/loras.py）—— 这才是「受影响文件」的准确口径。
set -uo pipefail

BASE=/tmp/h3s-cr-baseline
CAND=/root/projects/h3s-cr-20261115
OUT=/root/.hermes-web-ui/.ekko/workspace/default/person-main-505e35f9-2826-4d4c-af53-d3dc5de6f957/audit
PY=/home/ubuntu/comfyui/venv/bin/python
mkdir -p "$OUT"

# 测试文件清单（由引用扫描得出）
FILES=$(cd "$CAND" && python3 - <<'PYEOF'
import os
changed = ["web/app.js","web/index.html","web/providers.js","h3studio/loras.py","h3studio/lora_text_sanitize.py"]
# 【为什么排除这两个】它们是**本轮新增**的测试文件，基线树里根本不存在 ——
# 拿它们去跑基线会直接 "file or directory not found"。
# 它们在候选侧的通过情况单独验证（见变异测试 26/26）。
NEW_ONLY = {"test_visible_text_pollution.py", "test_flf_r2v_size_not_overridden.py"}
out=[]
for f in sorted(os.listdir("tests")):
    if not f.endswith(".py") or f.startswith("_"): continue
    if f in NEW_ONLY: continue
    try: s=open(os.path.join("tests",f),encoding="utf-8",errors="replace").read()
    except: continue
    if any(os.path.basename(c) in s for c in changed): out.append("tests/"+f)
print(" ".join(out))
PYEOF
)
COUNT=$(echo "$FILES" | wc -w)
echo "受影响测试文件：$COUNT 个"

echo "=== 1) 基线跑（8bcc3d0） ==="
cd "$BASE" && $PY -m pytest $FILES -q -p no:cacheprovider > "$OUT/pytest_baseline.txt" 2>&1
echo "baseline rc=$?"

echo "=== 2) 候选跑（我的分支） ==="
cd "$CAND" && $PY -m pytest $FILES -q -p no:cacheprovider > "$OUT/pytest_candidate.txt" 2>&1
echo "candidate rc=$?"

echo
echo "=== 3) 逐用例对比 ==="
cd "$CAND" && $PY scripts/compare_pytest_runs.py \
    --baseline "$OUT/pytest_baseline.txt" \
    --candidate "$OUT/pytest_candidate.txt"
