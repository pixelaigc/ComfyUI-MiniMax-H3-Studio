#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明「接续开关可见性」的断言**不是恒真断言**。

【为什么需要】
浏览器 E2E 测试最容易出的问题是**断言写虚了** —— 实现回退时测试仍然绿。
本脚本对实现做定向变异，每个变异都必须让对应断言变红。

【被测的修复】
`web/continuation.js` 的 `rerenderVideoPanel()`：
接续上下文就绪后**必定**重渲染一次视频面板，让「接续段也用二采」
开关的可见性不再依赖「有没有素材」这个副作用。

【方法】
每个变异：
  1. 把实现改成「缺陷版」
  2. 跑对应测试选择器 —— 必须**失败**
  3. 还原实现，重跑必须**通过**（确认还原干净）

【行尾纪律】
web/continuation.js 是 **LF** 文件（web/app.js 才是 CRLF —— 那个文件由
.gitattributes 的 -text 规则保护，字节原样入库）。
本文件的变异串用 \\n。

【与 tests/mutation_continuation_frontend_params.py 的分工】
那份变异的是「提交体里有没有 continuation」（参数层）；
本份变异的是「开关在界面上显不显示」（可见性层）。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/ubuntu/comfyui/venv/bin/python"

CONT_JS = os.path.join(ROOT, "web", "continuation.js")
TEST = "tests/test_continuation_switch_visibility.py"

#: (编号, 描述, 目标文件, 旧串, 新串, 必须失败的测试选择器)
MUTATIONS = [
    (
        "M1",
        "删掉 start() 里的 rerenderVideoPanel() 调用"
        "（= 缺陷本体：开关的可见性又依赖「有没有素材」这个副作用）",
        CONT_JS,
        "      rerenderVideoPanel();\n"
        "      const total = ctx.total_seconds ?",
        "      const total = ctx.total_seconds ?",
        f"{TEST}::TestContinuationSwitchVisibility::"
        "test_switch_appears_for_all_modes_without_assets",
    ),
    (
        "M2",
        "把 rerenderVideoPanel 变成**空函数**（调用还在，但什么也不做）",
        CONT_JS,
        "    const panel = window.VIDEO_PANEL;\n"
        "    if (!panel || typeof panel.render !== \"function\") return;",
        "    const panel = window.VIDEO_PANEL;\n"
        "    if (panel) return;",
        f"{TEST}::TestContinuationSwitchVisibility::"
        "test_switch_appears_for_all_modes_without_assets",
    ),
    (
        "M3",
        "把 clear() 里的 rerenderVideoPanel() 删掉"
        "（清掉上下文后开关会**留在界面上**）",
        CONT_JS,
        "    rerenderVideoPanel();\n"
        "  }\n"
        "\n"
        "  // ------------------------------------------------------------------\n"
        "  // 「复用参数」与「接续」的互斥",
        "  }\n"
        "\n"
        "  // ------------------------------------------------------------------\n"
        "  // 「复用参数」与「接续」的互斥",
        f"{TEST}::TestContinuationSwitchVisibility::"
        "test_switch_hides_again_after_clear",
    ),
    (
        "M4",
        "把 clearForReuse() 里的 rerenderVideoPanel() 删掉"
        "（「复用参数」清掉上下文后开关会留在界面上）",
        CONT_JS,
        "    // 【2026-11-19 修】同 clear() —— 清掉上下文后重渲染视频面板，\n"
        "    // 否则「接续段也用二采」开关会留在界面上。\n"
        "    rerenderVideoPanel();\n",
        "",
        f"{TEST}::TestContinuationSwitchVisibility::"
        "test_switch_hides_again_after_reuse_clears_context",
    ),
]


def _run_pytest(selector: str):
    proc = subprocess.run(
        [PY, "-m", "pytest", selector, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2500:]


def main() -> int:
    print("=" * 78)
    print("变异测试：接续开关可见性 —— 每个变异都必须让断言变红")
    print("=" * 78)

    ok, out = _run_pytest(TEST)
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2000:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    with open(CONT_JS, "rb") as fh:
        backup = fh.read()

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
                fh.write(backup)
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
