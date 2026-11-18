#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明 R2 前端开关的断言**不是恒真断言**。

【为什么需要】
R2 新增了「接续段也用二采」的前端显式开关 + 一组新断言。
新断言最容易出的问题是写得太空 —— 实现回退（比如开关默认开、
取消确认后不回滚、标签不写风险）时测试仍然绿。

⇒ 本脚本对前端实现做 6 个定向变异，每个变异都必须让对应断言变红。

【方法】
每个变异：
  1. 把实现改成「缺陷版」
  2. 跑对应测试子集 —— 必须**失败**
  3. 还原实现，重跑必须**通过**（确认还原干净）
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/ubuntu/comfyui/venv/bin/python"

PANEL_JS = os.path.join(ROOT, "web", "video-panel.js")
APP_JS = os.path.join(ROOT, "web", "app.js")
INDEX_HTML = os.path.join(ROOT, "web", "index.html")

TEST = "tests/test_twopass_allow_frontend.py"

#: (编号, 描述, 目标文件, 旧串, 新串, 必须失败的测试选择器)
MUTATIONS = [
    (
        "F1",
        "把面板默认值改成 true（开关默认开 —— 默认路径就不再等价 seg2）",
        PANEL_JS,
        "      allow_continuation: false,",
        "      allow_continuation: true,",
        f"{TEST}::TestFrontendAllowTwopassDefaultOff::"
        "test_panel_state_default_is_false",
    ),
    (
        "F2",
        "把 app.js 里写 allow_twopass 的条件去掉（无条件放行）",
        APP_JS,
        # 【注意】web/app.js 是 CRLF 文件（.gitattributes 的 -text 规则），
        # 变异串必须用 \r\n，否则匹配不到 —— 本仓行尾纪律的坑。
        "      const _allowTp = !!(_tpState.twopass\r\n"
        "        && _tpState.twopass.allow_continuation);\r\n",
        "      const _allowTp = true;\r\n",
        f"{TEST}::TestFrontendAllowTwopassDefaultOff::"
        "test_app_js_never_unconditionally_writes_allow_twopass",
    ),
    (
        "F3",
        "把「关掉二采时同时关掉 allow」这条删掉（留下矛盾状态）",
        PANEL_JS,
        "        if (!tpBox.checked) videoState.twopass.allow_continuation = false;\n",
        "",
        f"{TEST}::TestFrontendAllowTwopassDefaultOff::"
        "test_disabling_twopass_also_clears_allow",
    ),
    (
        "F4",
        "把中文标签里的「色档跳变」删掉（风险提示被抹掉）",
        INDEX_HTML,
        "接续段用二采会强制走段头色档跳变",
        "接续段用二采",
        f"{TEST}::TestFrontendAllowTwopassChineseLabel::"
        "test_label_says_colour_jump",
    ),
    (
        "F5",
        "把二次确认里的实测数字删掉（用户无从判断风险大小）",
        PANEL_JS,
        '            + "· 实测段头断点比 2.58x–4.54x（二采关闭时 ≤1.58x）\\n"',
        '            + "· 接续段用二采有风险\\n"',
        f"{TEST}::TestFrontendAllowTwopassChineseLabel::"
        "test_confirm_dialog_states_the_numbers",
    ),
    (
        "F6",
        "把「用户取消时回滚复选框」删掉（取消后状态仍然放行）",
        PANEL_JS,
        "            tpAllowBox.checked = false;\n",
        "",
        f"{TEST}::TestFrontendAllowTwopassConfirmation::"
        "test_cancel_rolls_back_checkbox_and_state",
    ),
]


def _run_pytest(selector: str):
    proc = subprocess.run(
        [PY, "-m", "pytest", selector, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=600)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2500:]


def main() -> int:
    print("=" * 78)
    print("变异测试：R2 前端「接续段也用二采」开关 —— 每个变异都必须让断言变红")
    print("=" * 78)

    ok, out = _run_pytest(TEST)
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2000:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    backups = {}
    for p in (PANEL_JS, APP_JS, INDEX_HTML):
        with open(p, "rb") as fh:
            backups[p] = fh.read()

    failures = []
    for mid, desc, target, old, new, selector in MUTATIONS:
        with open(target, "rb") as fh:
            data = fh.read()
        old_b = old.encode("utf-8")
        new_b = new.encode("utf-8")
        cnt = data.count(old_b)
        if cnt != 1:
            print(f"[FAIL] {mid}: 目标片段在 {os.path.basename(target)} 里出现 "
                  f"{cnt} 次（应恰好 1 次）—— 变异体已失效")
            failures.append(mid)
            continue
        with open(target, "wb") as fh:
            fh.write(data.replace(old_b, new_b))
        try:
            passed, out = _run_pytest(selector)
        finally:
            with open(target, "wb") as fh:
                fh.write(backups[target])
        if passed:
            print(f"[FAIL] {mid}: {desc}\n        -> 断言**仍然绿**（变异没被抓住）")
            failures.append(mid)
        else:
            red = [ln for ln in out.splitlines()
                   if ln.startswith("E ") or "AssertionError" in ln]
            detail = red[-1][:170] if red else "(见 pytest 输出)"
            print(f"[ OK ] {mid}: {desc}")
            print(f"        -> 如期变红：{detail}")
        passed_again, _ = _run_pytest(selector)
        if not passed_again:
            print(f"[FAIL] {mid}: 还原后仍然红 —— 还原不干净")
            failures.append(mid + "-restore")

    print()
    if failures:
        print(f"变异测试结果：{len(failures)} 条有问题 -> {failures}")
        return 1
    print(f"变异测试结果：全部 {len(MUTATIONS)} 条变异均让对应断言变红 ✅")
    print("（证明这些断言不是恒真断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
