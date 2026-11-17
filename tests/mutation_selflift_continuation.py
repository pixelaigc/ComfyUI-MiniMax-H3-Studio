# -*- coding: utf-8 -*-
"""P2 selflift 接续的**变异测试**：证明那些断言真的能抓住回归。

【为什么需要这个】新增的测试如果只是「写完就绿」，那它可能什么都没防。
变异测试的做法：**故意把生产代码改坏**，看**命名该行为的那条断言**
会不会红。红了 = 那条断言真的在守这个行为。

每个变异必须：
  1. 改一处生产代码（最小改动）；
  2. 跑出**具体**的失败断言名（不是「某个测试失败」）；
  3. 立刻还原，确认还原后全绿。

【纪律】变异测试**不能**留在仓库里当常驻测试 —— 它会故意弄坏代码。
所以本文件是**一次性脚本**，不是 pytest 用例。

用法：python3 tests/mutation_selflift_continuation.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 变异表：(说明, 文件, 原文, 替换文, 必须失败的测试名片段)
MUTATIONS = [
    (
        "把「二采不走接续分支」的条件加回来（not use_twopass）",
        "h3studio/video_workflow.py",
        '    if cont_state["enabled"] and cont_state["has_source"]:',
        '    if cont_state["enabled"] and not use_twopass and cont_state["has_source"]:',
        "test_selflift_continuation_has_bridge_nodes",
    ),
    (
        "把 SelfLift 的 positive 从桥第 4 路改回条件节点",
        "h3studio/video_workflow.py",
        '            "positive": pos_ref,',
        '            "positive": [cond_node_id, 0],',
        "test_selflift_positive_uses_bridge_fourth_output",
    ),
    (
        "把落盘从采样器输出改成桥输出（nonebot v1 的直通场景）",
        "h3studio/video_workflow.py",
        '                "latent": samples_ref,\n                "run_id": cont_state["own_run_id"],',
        '                "latent": ["300:bridge", 0],\n                "run_id": cont_state["own_run_id"],',
        "test_selflift_save_takes_sampler_output_not_bridge",
    ),
    (
        "把二采闸门放回去（一律拒绝）",
        "h3studio/continuation.py",
        '    if not mode_supported(mode):',
        '    if selflift_enabled(params):\n'
        '        return False, "二采产物不支持接续。"\n'
        '    if not mode_supported(mode):',
        "test_selflift_mode_is_not_blanket_rejected",
    ),
    (
        "把 flf/r2v 的二采接续分支也挡回去",
        "h3studio/h3_video_modes.py",
        '    if cont_state["enabled"] and cont_state["has_source"]:',
        '    if cont_state["enabled"] and not use_twopass and cont_state["has_source"]:',
        "test_flf_selflift_continuation_has_bridge",
    ),
    (
        "把拷贝桥的钉住掩码从 hard 改成 taper",
        "h3studio/video_workflow.py",
        '                "mask_mode": "hard",',
        '                "mask_mode": "taper",',
        "test_selflift_bridge_mask_is_hard",
    ),
]


def run_tests(test_file: str) -> tuple[int, str]:
    """跑一个测试文件，返回 (returncode, 输出)。"""
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_file, "-q"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def run_one(test_id: str) -> tuple[int, str]:
    """只跑**一条**目标断言，返回 (returncode, 输出)。

    【为什么必须只跑目标断言】不能用 `-x` 跑整个文件 ——
    别的断言先红的话会提前中止，目标断言根本没跑到，
    就会误判成「变异没被抓住」。
    """
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_id, "-q"],
        cwd=str(REPO), capture_output=True, text=True, timeout=300, env=env)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("=" * 74)
    print("P2 selflift 接续 —— 变异测试（证明断言真能抓住回归）")
    print("=" * 74)

    # 先确认基线是绿的
    rc, out = run_tests("tests/test_cont_selflift.py")
    if rc != 0:
        print("❌ 基线就不绿，先修基线：\n", out[-3000:])
        return 1
    print("\n[基线] tests/test_cont_selflift.py 全绿 ✅\n")

    failures = []
    for idx, (desc, rel_path, old, new, expect_test) in enumerate(MUTATIONS, 1):
        path = REPO / rel_path
        backup = path.read_text(encoding="utf-8")
        try:
            if old not in backup:
                print(f"[{idx}] ⚠  跳过：找不到原文 → {desc}")
                failures.append((desc, "SKIPPED: 原文未找到"))
                continue
            # 只替换**第一处**出现（保证改动最小、可控）
            path.write_text(backup.replace(old, new, 1), encoding="utf-8")
            # 只跑**目标断言**（不能 -x，否则别的断言先红会提前中止）
            rc, out = run_one("tests/test_cont_selflift.py::%s" % expect_test)
            caught = rc != 0
            named = expect_test in out
            if caught and named:
                print(f"[{idx}] ✅ 变异被抓到：{desc}")
                print(f"      失败断言：{expect_test}")
            else:
                print(f"[{idx}] ❌ 变异**没被抓到**：{desc}")
                print(f"      rc={rc} 目标断言名出现={named}")
                print(out[-2500:])
                failures.append((desc, "未被目标断言抓住"))
        finally:
            path.write_text(backup, encoding="utf-8")
            # 还原后必须仍然全绿
            rc2, out2 = run_tests("tests/test_cont_selflift.py")
            if rc2 != 0:
                print(f"      ⚠ 还原后**不绿**！变异残留：\n{out2[-2000:]}")
                failures.append((desc, "还原后不绿"))

    print("\n" + "=" * 74)
    if failures:
        print(f"❌ 变异测试失败 {len(failures)} 项：")
        for desc, why in failures:
            print(f"   - {desc}: {why}")
        return 1
    print(f"✅ 全部 {len(MUTATIONS)} 个变异都被对应断言抓住，且还原后全绿")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
