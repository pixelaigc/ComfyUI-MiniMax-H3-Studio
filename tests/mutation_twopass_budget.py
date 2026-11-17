# -*- coding: utf-8 -*-
"""二采 NFE 预算护栏的**变异测试**（2026-11-17 接缝色档残缺陷）。

【原则】
每条变异 = 故意把实现改回缺陷状态，然后验证
「**命名该行为的那条断言**」会失败。
如果变异后测试仍然全绿 ⇒ 那条断言是**空的**（抓不住回归），必须修。

运行：python3 tests/mutation_twopass_budget.py
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUDGET = REPO / "h3studio" / "twopass_budget.py"
WORKFLOW = REPO / "h3studio" / "video_workflow.py"
SERVER = REPO / "server.py"

TEST_FILE = "tests/test_twopass_budget.py"


def _run_tests() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_FILE, "-q", "-p", "no:randomly"],
        cwd=REPO, capture_output=True, text=True, timeout=900)
    return proc.returncode == 0, proc.stdout[-2500:]


def _mutate(path: Path, old: str, new: str) -> str:
    src = path.read_text(encoding="utf-8")
    if old not in src:
        raise SystemExit(f"变异锚点没找到于 {path}:\n{old!r}")
    path.write_text(src.replace(old, new, 1), encoding="utf-8")
    return src


#: (名字, 文件, 旧文本, 新文本, 必须失败的测试名片段)
MUTATIONS = [
    # ① 把夹取整个变成 no-op（回到缺陷状态）
    ("clamp_noop", BUDGET,
     "    return min(ts, max_ts)",
     "    return ts  # MUTANT: 夹取被禁用",
     "test_defect_budget_gets_clamped_to_two"),

    # ② 夹取方向搞反（向上取整 -> 保留缺陷值）
    ("clamp_wrong_direction", BUDGET,
     "    return min(ts, max_ts)",
     "    return max(ts, max_ts)  # MUTANT: 方向反了",
     "test_defect_budget_gets_clamped_to_two"),

    # ③ 比例算错（1/2 -> 1/4，等于放宽到 3 步）
    ("ratio_too_loose", BUDGET,
     "HIGH_RES_NFE_MIN_RATIO = 0.5",
     "HIGH_RES_NFE_MIN_RATIO = 0.25  # MUTANT: 放宽约束",
     "test_seven_steps_transition_five_gets_clamped_to_three"),

    # ④ 节点侧不夹（删掉工作流里的夹取）
    ("workflow_no_clamp", WORKFLOW,
     "    transition_step = max(1, min(_budget_transition, max_transition))",
     "    transition_step = max(1, min(transition_step, max_transition))  # MUTANT",
     "test_node_receives_clamped_transition_step"),

    # ⑤ 服务端不夹
    ("server_no_clamp", SERVER,
     "        if _budget_transition != transition:",
     "        if False:  # MUTANT",
     "test_normalize_clamps_and_records_note"),
]


def main() -> int:
    print("=" * 74)
    print("变异测试：二采 NFE 预算护栏")
    print("=" * 74)

    # 先确认基线全绿
    ok, out = _run_tests()
    print(f"\n[基线] {'全绿' if ok else '有失败'} —— 这必须是绿的，否则变异测试无意义")
    if not ok:
        print(out)
        return 1

    failures = []
    for name, path, old, new, expect in MUTATIONS:
        backup = path.read_text(encoding="utf-8")
        try:
            _mutate(path, old, new)
            ok, out = _run_tests()
            caught = not ok
            named = expect in out
            if caught and named:
                print(f"  ✅ {name:26} 被断言抓住：{expect}")
            elif caught:
                print(f"  ⚠️  {name:26} 测试失败了，但**不是**期望的那条断言")
                print("     " + out.strip().splitlines()[-1][:150])
                failures.append(name)
            else:
                print(f"  ❌ {name:26} **没被抓住** —— 断言是空的！")
                failures.append(name)
        finally:
            path.write_text(backup, encoding="utf-8")

    # 还原后必须恢复全绿
    ok, out = _run_tests()
    print(f"\n[还原] {'全绿' if ok else '有失败'}")
    if not ok:
        failures.append("restore")

    if failures:
        print(f"\n❌ 变异测试失败：{failures}")
        return 1
    print(f"\n✅ 全部 {len(MUTATIONS)} 条变异都被对应断言抓住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
