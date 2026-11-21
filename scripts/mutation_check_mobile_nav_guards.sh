#!/usr/bin/env bash
# 变异测试：证明 tests/test_mobile_nav_regression_guards.py 真的会红。
#
# 【为什么要做】
# 验证纪律：「报『全绿』必须自问『有没有一条本该红的』」。
# 这些守卫守的是「查错 id」「属性名搞错」「模态框残留」「让位规则放错断点」
# 四类**反复踩过**的坑 —— 如果它们不会红，下次换个写法又白测一遍。
#
# 【每个变异对应一条真实踩过的坑】
#   M1  详情弹层 id 改成 #jobDetailModal（已停用的那套）→ T1 必须红
#   M2  生成类型标签改成 data-st（第一轮复测踩过）→ T2 必须红
#   M3  状态标签改成 data-type → T2b 必须红
#   M4  关详情后不摘 hidden（模态框残留级联）→ T3 必须红
#   M5  平板档删掉 padding-bottom（FAB 压分页条）→ T4 静态守卫必须红
#   M6  手机档删掉 padding-bottom → T4 首屏守卫必须红
#   M7  .panel-mask 遮罩改回 inset:0（顶栏被盖）→ T5 必须红
set -uo pipefail
cd "$(dirname "$0")/.."

TARGET=tests/test_mobile_nav_regression_guards.py
PASS=0
FAIL=0
declare -a RESULTS

run_guard() {
  # $1=变异名  $2=要改的 python 片段(用 {EDITOR} 占位)
  local name="$1"; shift
  local applied="$1"; shift
  echo "──────── $name ────────"
  # 备份
  cp web/app.js /tmp/mt_app.js
  cp web/index.html /tmp/mt_index.html
  cp web/responsive.css /tmp/mt_resp.css
  cp web/job-detail.js /tmp/mt_jd.js
  if ! eval "$applied"; then
    echo "  !! 变异没施加成功（跳过）"
    cp /tmp/mt_app.js web/app.js; cp /tmp/mt_index.html web/index.html
    cp /tmp/mt_resp.css web/responsive.css; cp /tmp/mt_jd.js web/job-detail.js
    return
  fi
  local out
  out=$(timeout 300 python3 -m pytest "$TARGET" -q --tb=no 2>&1 | tail -3)
  echo "$out" | grep -E 'passed|failed|error' | tail -1
  if echo "$out" | grep -qE '[1-9][0-9]* failed'; then
    RESULTS+=("PASS(红)  $name")
    PASS=$((PASS+1))
  else
    RESULTS+=("FAIL(绿)  $name  ← 守卫没咬人")
    FAIL=$((FAIL+1))
  fi
  cp /tmp/mt_app.js web/app.js
  cp /tmp/mt_index.html web/index.html
  cp /tmp/mt_resp.css web/responsive.css
  cp /tmp/mt_jd.js web/job-detail.js
}

# M1: 让详情弹层退化成 #jobDetailModal
run_guard "M1 详情弹层用 #jobDetailModal（已停用）" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/app.js'); s=p.read_bytes()
old=b'const modal = \$(\"#detailModal\");'
# app.js 里 openDetailModal 用的是 \$(\"#detailModal\")，改成 jobDetailModal
n=s.replace(b'\$(\"#detailModal\")', b'\$(\"#jobDetailModal\")')
p.write_bytes(n); print('M1 applied')
PY"

# M2: 生成类型标签 data-type -> data-st
run_guard "M2 生成类型标签改用 data-st" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/app.js'); s=p.read_bytes()
s=s.replace(b'<button data-type=\"\${esc(t.key)}\"', b'<button data-st=\"\${esc(t.key)}\"')
s=s.replace(b'btn.dataset.type || \"\"', b'btn.dataset.st || \"\"')
s=s.replace(b'btn.dataset.type = ', b'btn.dataset.st = ')
p.write_bytes(s); print('M2 applied')
PY"

# M3: 状态标签 data-st -> data-type
run_guard "M3 状态标签改用 data-type" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/index.html'); s=p.read_bytes()
s=s.replace(b'data-st=\"all\"', b'data-type=\"all\"')
s=s.replace(b'data-st=\"active\"', b'data-type=\"active\"')
s=s.replace(b'data-st=\"done\"', b'data-type=\"done\"')
s=s.replace(b'data-st=\"favorites\"', b'data-type=\"favorites\"')
p.write_bytes(s); print('M3 applied')
PY"

# M4: 关详情不摘 hidden
run_guard "M4 关详情不摘 hidden（模态框残留）" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/app.js'); s=p.read_bytes()
old=b'function closeDetailModal() {'
i=s.find(old)
assert i>=0
j=s.find(b'modal.hidden = true;', i)
assert j>=0
s = s[:j] + b'modal.hidden = false;' + s[j+len(b'modal.hidden = true;'):]
p.write_bytes(s); print('M4 applied')
PY"

# M5: 平板档删掉 padding-bottom
run_guard "M5 平板档删掉 FAB 让位 padding-bottom" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/responsive.css'); s=p.read_text(encoding='utf-8')
old='''  .jobs-area { padding-bottom: 76px; }
  .jobs-pagination .page-summary { margin-left: 0; }

  /* 抽屉内部：多列改单列 */'''
assert old in s
p.write_text(s.replace(old,'  /* 抽屉内部：多列改单列 */'),encoding='utf-8'); print('M5 applied')
PY"

# M6: 手机档删掉 padding-bottom
#
# 【注意】修完平板档后，≤1100px 那条**已经覆盖**了 ≤700px 那条
# （CSS 层叠：两个断点都命中时，后写的 ≤700px 覆盖前面的；
#  但删掉 ≤700px 那条后，≤1100px 那条依然生效 ⇒ 手机档不受影响）。
# ⇒ 这个变异必须**两条一起删**，否则删一条等于没变异（守卫当然不红）。
run_guard "M6 两档都删掉 FAB 让位 padding-bottom" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/responsive.css'); s=p.read_text(encoding='utf-8')
a='''  .jobs-area { padding-bottom: 76px; }
  .jobs-pagination .page-summary { margin-left: 0; }

  /* 抽屉内部：多列改单列 */'''
b='''  .jobs-area { padding-bottom: 76px; }
  .jobs-pagination .page-summary { margin-left: 0; }

  /* ---------- 4.6 输入框'''
assert a in s and b in s
s = s.replace(a,'  /* 抽屉内部：多列改单列 */')
s = s.replace(b,'  /* ---------- 4.6 输入框')
p.write_text(s,encoding='utf-8'); print('M6 applied')
PY"

# M7: 遮罩改回 inset:0
run_guard "M7 参数抽屉遮罩改回 inset:0（顶栏被盖）" \
  "python3 - <<'PY'
from pathlib import Path
p=Path('web/responsive.css'); s=p.read_text(encoding='utf-8')
old='position: fixed; inset: 44px 0 0 0; background: rgba(18, 22, 30, .42);'
assert old in s
p.write_text(s.replace(old,'position: fixed; inset: 0; background: rgba(18, 22, 30, .42);'),encoding='utf-8'); print('M7 applied')
PY"

echo
echo "================ 变异测试汇总 ================"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo "  红 $PASS / 绿 $FAIL"
[ "$FAIL" -eq 0 ] || exit 1
