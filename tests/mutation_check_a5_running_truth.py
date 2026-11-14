#!/usr/bin/env python3
"""A5 的变异检查：逐个破坏判定逻辑，确认测试会失败（断言「测试会咬人」）。

【为什么需要】新写的测试最常见的失败模式是「测了个寂寞」——
断言写得不对，代码怎么改它都绿。这里对四条核心判定各做一次变异，
要求：变异后**必须**出现测试失败。

运行：python3 tests/mutation_check_a5_running_truth.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "h3studio" / "running_truth.py"
BACKUP = TARGET.with_suffix(".py.bak")

MUTATIONS = [
    (
        "M1 去掉「队列里也没有 -> 已丢失」判定",
        "    if elapsed < grace:\n        return STATE_UNKNOWN\n    return STATE_LOST",
        "    if elapsed < grace:\n        return STATE_UNKNOWN\n    return STATE_ACTIVE",
    "tests/test_a5_running_truth.py::ClassifyTest::"
        "test_prompt_not_in_queue_and_no_history_is_lost",
    ),
    (
        "M2 把「ComfyUI 读不到」当成「队列为空」",
        "    if queue_ids is None:\n        return STATE_UNKNOWN",
        "    if queue_ids is None:\n        queue_ids = set()",
        "tests/test_a5_running_truth.py::ClassifyTest::"
        "test_unreachable_comfy_is_unknown_not_lost",
    ),
    (
        "M3 去掉超时判定",
        "    if limit > 0 and elapsed > limit:\n        return STATE_TIMEOUT",
        "    if limit > 0 and elapsed > limit:\n        return STATE_ACTIVE",
        "tests/test_a5_running_truth.py::ClassifyTest::"
        "test_timeout_overrides_queue_presence",
    ),
    (
        "M4 自愈时不再把「丢失的残留」判成失败",
        '    return {\n        "action": "fail",\n'
        '        "reason": RESTART_STALE_REASON,\n'
        '        "state": STATE_LOST,\n    }\n',
        '    return {\n        "action": "adopt",\n'
        '        "reason": "",\n'
        '        "state": STATE_ACTIVE,\n    }\n',
        "tests/test_a5_running_truth.py::ResidualRunningPlanTest::"
        "test_missing_from_queue_and_history_fails",
    ),
    (
        "M5 前端判定不再把超限判成 timeout",
        "    if limit > 0 and elapsed > limit:\n        local_state = STATE_TIMEOUT",
        "    if limit > 0 and elapsed > limit:\n        local_state = STATE_ACTIVE",
        "tests/test_a5_running_truth.py::JobRunningTruthTest::"
        "test_running_over_limit_is_timeout",
    ),
]


def run_tests(timeout: int = 120):
    """跑 A5 测试文件，返回 (是否通过, 输出最后几行)。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_a5_running_truth.py",
             "-q", "--tb=no", "-x"],
            cwd=str(REPO), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT（测试挂住 —— 说明变异让任务永远跑不完，也算咬到了）"
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    return proc.returncode == 0, tail[0]


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    shutil.copy2(TARGET, BACKUP)
    failures = []
    try:
        print("=== 基线（未变异）===")
        ok, tail = run_tests()
        print(f"  基线通过 = {ok}  {tail}")
        if not ok:
            print("!! 基线就不绿 —— 先修测试再谈变异")
            return 1

        for name, old, new, expect in MUTATIONS:
            count = original.count(old)
            if count < 1:
                failures.append(f"{name}: 锚点没找到（源码变了？）")
                print(f"[SKIP] {name}: 锚点未命中")
                continue
            # 只替换最后一次出现（plan_residual_running 里有两处相同的 fail 块）
            idx = original.rfind(old)
            mutated = original[:idx] + new + original[idx + len(old):]
            TARGET.write_text(mutated, encoding="utf-8")
            ok, tail = run_tests()
            if ok:
                failures.append(f"{name}: 变异后测试**仍然全绿** —— 断言没咬到")
                print(f"[MISS] {name}: 测试仍然全绿 ❌  {tail}")
            else:
                print(f"[BITE] {name}: 测试失败 ✅  {tail}")
                print(f"        期望咬到的用例：{expect}")
    finally:
        TARGET.write_text(original, encoding="utf-8")
        BACKUP.unlink(missing_ok=True)
    print()
    if failures:
        print("=== 变异检查未通过 ===")
        for item in failures:
            print(" -", item)
        return 1
    print("=== 变异检查通过：每条判定都有测试咬住 ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
