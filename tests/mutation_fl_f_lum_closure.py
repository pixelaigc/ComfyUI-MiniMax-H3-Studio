#!/usr/bin/env python
"""变异测试：证明 tests/test_fl_f_lum_closure.py 的断言真能抓住回归。

做法 = 篡改「被测数据」（证据 JSON），看对应断言是否变红。
只放宽断言不算变异（那测不出任何东西）。

用法： python3 tests/mutation_fl_f_lum_closure.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

EV = Path("/root/projects/h3studio-flf-lum-20261118/evidence/"
          "evidence-20261118-flf-lum-closure")
REPO = Path("/root/projects/h3studio-flf-lum-20261118")
SETTLING = EV / "seam_vs_natural_settling.json"
DELTA = EV / "seam_local_delta_all.json"
TEST = REPO / "tests/test_fl_f_lum_closure.py"


def run_tests() -> tuple[bool, str]:
    out = subprocess.run(
        ["/home/ubuntu/comfyui/venv/bin/python", "-m", "pytest", str(TEST),
         "-q", "-p", "no:randomly"],
        cwd=str(REPO), capture_output=True, text=True)
    return out.returncode == 0, out.stdout[-1500:]


def m1_systematic_bias() -> tuple[str, dict]:
    """M1: 伪造「接缝引入系统性台阶」——素材A 组 A 均值整体抬高 0.6。"""
    d = json.loads(SETTLING.read_text(encoding="utf-8"))
    for r in d["rows"]:
        if r["material"] == "A":
            r["seam_lum_delta_A"] = r["seam_lum_delta_A"] + 0.6
            r["seam_excess_over_N"] = r["seam_excess_over_N"] + 0.6
    g = d["by_material"]["A"]
    vals = [r["seam_lum_delta_A"] for r in d["rows"] if r["material"] == "A"]
    nvals = [r["seg1_head_settling_N"] for r in d["rows"] if r["material"] == "A"]
    evals = [r["seam_excess_over_N"] for r in d["rows"] if r["material"] == "A"]
    g["A"]["mean"] = round(sum(vals) / len(vals), 4)
    g["E"]["mean"] = round(sum(evals) / len(evals), 4)
    g["A"]["sign_pos"] = sum(1 for v in vals if v > 0)
    g["E"]["sign_pos"] = sum(1 for v in evals if v > 0)
    g["E"]["sign_neg"] = sum(1 for v in evals if v < 0)
    SETTLING.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("test_material_A_controlled_group_seam_matches_natural", d)


def m2_a_equals_n() -> tuple[str, dict]:
    """M2: 让 A 恒等于 N（口径退化成「自然收敛」）。"""
    d = json.loads(SETTLING.read_text(encoding="utf-8"))
    for r in d["rows"]:
        r["seam_lum_delta_A"] = r["seg1_head_settling_N"]
        r["seam_excess_over_N"] = 0.0
    SETTLING.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("test_a_and_n_are_not_the_same_measurement", d)


def m3_outlier_b_normalized() -> tuple[str, dict]:
    """M3: 把极端 B 值（-28.06）抹平 —— 掩盖「段内光照事件污染 B 口径」的事实。

    该断言读的是 SETTLING（seam_vs_natural_settling.json），
    所以必须篡改 SETTLING 里的 seg2_head_settling_B。
    """
    d = json.loads(SETTLING.read_text(encoding="utf-8"))
    for r in d["rows"]:
        if r["job2"] == "8cb1303c0c64":
            r["seg2_head_settling_B"] = -0.5
    SETTLING.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("test_outlier_row_has_large_b_but_small_a", d)


def m4_control_blinded() -> tuple[str, dict]:
    """M4: 让测量器对二采 ON 的真突变失效（鉴别力自证被破坏）。"""
    d = json.loads(DELTA.read_text(encoding="utf-8"))
    for r in d:
        if r["job2"] == "513383284a58":
            r["seg2_lum_drop_3frames"] = 0.5
            r["seg2_head_break_ratio"] = 1.0
    DELTA.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("test_control_chain_is_detected_by_measurement", d)


def m5_pool_shrunk() -> tuple[str, dict]:
    """M5: 样本池少于 14（扩样本要求被破坏）。"""
    d = json.loads(SETTLING.read_text(encoding="utf-8"))
    d["rows"] = d["rows"][:3]
    d["n_total"] = 3
    SETTLING.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return ("test_settling_evidence_exists", d)


MUTANTS = [
    ("M1 伪造系统性偏暗", m1_systematic_bias, SETTLING),
    ("M2 A 退化为 N", m2_a_equals_n, SETTLING),
    ("M3 抹平极端 B 值", m3_outlier_b_normalized, SETTLING),
    ("M4 破坏测量器鉴别力", m4_control_blinded, DELTA),
    ("M5 样本池缩水", m5_pool_shrunk, SETTLING),
]


def main() -> int:
    ok, _ = run_tests()
    print(f"  基线（未变异）: {'✅ 全绿' if ok else '❌ 有失败'}")
    if not ok:
        print("  !! 基线就不绿，先修基线再谈变异")
        return 1

    n_red = 0
    for name, fn, target in MUTANTS:
        backup = target.with_suffix(target.suffix + ".mutbak")
        shutil.copy(target, backup)
        try:
            expect_test, _ = fn()
            ok2, out = run_tests()
            red = not ok2
            caught = expect_test in out
            status = "🔴 变红" if red else "🟢 未变红"
            print(f"  {name}: {status} | 期望抓红的是 {expect_test} "
                  f"→ {'✅ 命中' if caught else '❌ 没命中'}")
            if red and caught:
                n_red += 1
        finally:
            shutil.move(str(backup), str(target))

    ok, _ = run_tests()
    print(f"\n  还原后基线: {'✅ 全绿' if ok else '❌ 有失败'}")
    print(f"  变异抓红数 = {n_red}/{len(MUTANTS)}")
    return 0 if n_red == len(MUTANTS) and ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
