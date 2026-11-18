#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""变异测试：证明「flf/r2v 接续必须提交 continuation」的断言**不是恒真断言**。

【为什么需要】
新增的源码级断言最容易出的问题是写得太空 —— 实现回退时测试仍然绿。
本脚本对实现做 4 个定向变异，每个变异都必须让对应断言变红。

【方法】
每个变异：
  1. 把实现改成「缺陷版」
  2. 跑对应测试选择器 —— 必须**失败**
  3. 还原实现，重跑必须**通过**（确认还原干净）

【行尾纪律】
web/app.js 是 **CRLF** 文件（.gitattributes 的 -text 规则）——
变异串必须用 \\r\\n，否则匹配不到（本仓行尾纪律的坑，
见 tests/mutation_twopass_allow_frontend.py 的 F2 注释）。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = "/home/ubuntu/comfyui/venv/bin/python"

APP_JS = os.path.join(ROOT, "web", "app.js")
TEST = "tests/test_continuation_frontend_params.py"

#: (编号, 描述, 旧串, 新串, 必须失败的测试选择器)
MUTATIONS = [
    (
        "M1",
        "把接续提交退回**只对 t2v 生效**（= 缺陷本体：flf/r2v 退化）",
        APP_JS,
        '  const _isVideoMode = mode === "t2v" || mode === "flf" || mode === "r2v";\r\n',
        '  const _isVideoMode = mode === "t2v";\r\n',
        f"{TEST}::TestContinuationSubmittedForAllVideoModes::"
        "test_video_modes_share_one_continuation_submission",
    ),
    (
        "M2",
        "在 flf/r2v 分支里**覆盖**掉公共作用域写的 continuation"
        "（模拟「分支里又设了一次值」的假修）",
        APP_JS,
        "    params.audio_track_source = state.audioTrackSource || \"\";\r\n",
        "    params.audio_track_source = state.audioTrackSource || \"\";\r\n"
        "    params.continuation = null;\r\n",
        f"{TEST}::TestContinuationSubmittedForAllVideoModes::"
        "test_flf_and_r2v_branches_do_not_lose_continuation",
    ),
    (
        "M3",
        "把公共作用域里的**分辨率一致性闸**删掉"
        "（flf/r2v 接续会失去跨分辨率保护）",
        APP_JS,
        "      if (typeof window.Continuation.checkSubmitConsistency === \"function\") {\r\n"
        "        // 【2026-11-16 审查 B-7 修复】读正式 API，不再读 _debug。\r\n",
        "      if (false) {\r\n"
        "        // 【2026-11-16 审查 B-7 修复】读正式 API，不再读 _debug。\r\n",
        f"{TEST}::TestContinuationSubmittedForAllVideoModes::"
        "test_submit_consistency_gate_still_applies_to_all_modes",
    ),
    (
        "M4",
        "把接续提交块整个**删掉**（回到缺陷本体）",
        APP_JS,
        "  const _isVideoMode = mode === \"t2v\" || mode === \"flf\" || mode === \"r2v\";\r\n",
        "",
        f"{TEST}::TestContinuationSubmittedForAllVideoModes::"
        "test_video_modes_share_one_continuation_submission",
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
    print("变异测试：flf/r2v 接续必须提交 continuation —— 每个变异都必须让断言变红")
    print("=" * 78)

    ok, out = _run_pytest(TEST)
    if not ok:
        print("!! 变异前就有失败，先修测试：")
        print(out[-2000:])
        return 1
    print("\n[ OK ] 变异前全部通过\n")

    with open(APP_JS, "rb") as fh:
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
