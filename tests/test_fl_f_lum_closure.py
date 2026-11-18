# -*- coding: utf-8 -*-
"""回归测试：接缝亮度台阶 vs 段首自然收敛 的分离口径。

背景（2026-11-18 实测）：
  「段2 三帧亮度降」= lum[段2 首帧] - lum[段2 第4帧]
  这个量测的是「段2 内部前三帧的亮度演化」，会被段内光照事件主导
  （实测 seed=20261125 的链段2 第4帧亮度跃升 +26.9 灰阶 → 该指标 -28.06），
  也随采样路径改变符号（素材A 组段1 SelfLift 4/4 为负、段2 标准 3/4 为正）。

  真正的「接缝亮度跳变」应测：
    A = lum[段2 首帧] - lum[段1 末帧]   （接缝两端亮度台阶）
    N = lum[段1 首帧] - lum[段1 第4帧]   （段1 自身首帧收敛，与接续无关）
    E = A - N                           （接缝相对自然收敛的超额）
  若 E ≈ 0 ⇒ 接缝没有引入额外亮度台阶。

本测试锁定「两个口径必须分开」这一事实，防止后续有人把 B 口径当作接缝指标。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SEAM = HERE / "live_flf_seam_repeat.py"

spec = importlib.util.spec_from_file_location("seam_mod", SEAM)
assert spec is not None and spec.loader is not None
seam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seam)

EV = Path("/root/projects/h3studio-flf-lum-20261118/evidence/"
          "evidence-20261118-flf-lum-closure")
SETTLING = EV / "seam_vs_natural_settling.json"
DELTA = EV / "seam_local_delta_all.json"


def _load(path: Path):
    if not path.exists():
        pytest.skip(f"证据文件不存在（需先跑线上采样）：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


class TestMetricSeparation:
    """A / N / E 三个口径必须存在且互不相同。"""

    def test_settling_evidence_exists(self):
        data = _load(SETTLING)
        assert data["n_total"] == 14, f"样本池应为 14，实际 {data['n_total']}"

    def test_three_metrics_present(self):
        data = _load(SETTLING)
        for key in ("A_seam_lum_delta", "N_seg1_head_settling",
                    "E_seam_excess_over_N"):
            assert key in data, f"缺口径 {key}"
            assert data[key]["n"] == 14

    def test_a_and_n_are_not_the_same_measurement(self):
        """A 与 N 不能是同一个量（否则「接缝台阶」就退化成「自然收敛」）。"""
        data = _load(SETTLING)
        for r in data["rows"]:
            assert r["seam_lum_delta_A"] != r["seg1_head_settling_N"], (
                f"{r['job2']}: A 与 N 不应恒等")
            assert abs(r["seam_excess_over_N"] -
                       (r["seam_lum_delta_A"] -
                        r["seg1_head_settling_N"])) < 1e-6

    def test_outlier_row_has_large_b_but_small_a(self):
        """seed=20261125：B 口径 = -28.06（段内光照事件），但 A 口径仅 +0.85。

        这条断言是「B 口径被段内事件污染」的直接证据；
        若它变红，说明测量口径被改动，B 又能代表接缝了。
        """
        data = _load(SETTLING)
        row = next(r for r in data["rows"] if r["job2"] == "8cb1303c0c64")
        assert row["seg2_head_settling_B"] < -20.0, (
            f"预期 B 口径为极端负值，实测 {row['seg2_head_settling_B']}")
        assert abs(row["seam_lum_delta_A"]) < 2.0, (
            f"预期 A 口径仍很小，实测 {row['seam_lum_delta_A']}")

    def test_material_A_controlled_group_seam_matches_natural(self):
        """素材A 受控组 n=9：接缝台阶 ≈ 段首自然收敛 ⇒ 接缝无额外亮度台阶。

        判据：|A 均值 - N 均值| < 0.1 且 E 均值 |·| < 0.1 且 E 方向不一致。
        """
        data = _load(SETTLING)
        grp = data["by_material"]["A"]
        assert grp["n"] == 9
        a_mean = grp["A"]["mean"]
        n_mean = grp["N"]["mean"]
        e_mean = grp["E"]["mean"]
        assert abs(a_mean - n_mean) < 0.1, (
            f"素材A 组 A({a_mean}) 应与 N({n_mean}) 接近")
        assert abs(e_mean) < 0.1, f"素材A 组超额均值 {e_mean} 应接近 0"
        assert grp["E"]["sign_pos"] > 0 and grp["E"]["sign_neg"] > 0, (
            "超额量方向应不一致（若一致说明存在系统性台阶）")

    def test_control_chain_is_detected_by_measurement(self):
        """二采 ON 对照链：同一测量器必须能测出真突变（B 口径 +7.21）。"""
        data = _load(DELTA)
        ctrl = next(r for r in data if r["job2"] == "513383284a58")
        assert ctrl["seg2_lum_drop_3frames"] > 5.0, (
            "测量器对二采 ON 的真突变失效")
        assert ctrl["seg2_head_break_ratio"] > 2.0


class TestProductionCodeUntouched:
    """判定 (a) ⇒ 本轮零生产代码改动、零 comfyui 源码改动。"""

    def test_no_production_code_change_in_branch(self):
        """本轮零生产代码改动。

        比较基准 = 本分支的起点（与 local/maintainer 的 merge-base），
        不能用 local/maintainer 本身：并行任务会在本轮期间往 mainline 合新提交，
        那不是「本轮改的」。
        """
        repo = Path("/root/projects/h3studio-flf-lum-20261118")
        if not repo.exists():
            pytest.skip("worktree 不存在")
        import subprocess
        base = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "HEAD", "local/maintainer"],
            capture_output=True, text=True).stdout.strip()
        assert base, "拿不到 merge-base"
        out = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", base,
             "--", ".", ":(exclude)tests/", ":(exclude)docs/",
             ":(exclude)evidence/"],
            capture_output=True, text=True)
        changed = [x for x in out.stdout.split() if x.strip()]
        assert changed == [], f"生产代码被改动：{changed}"
