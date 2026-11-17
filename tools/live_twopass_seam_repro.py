#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""二采接缝的**受控复现矩阵**（2026-11-17 接缝色档残缺陷 · 第二轮取证）。

【为什么需要这一轮】
第一轮矩阵（tools/live_twopass_seam_matrix.py）跑出来的 baseline 是 **2.065x**，
但历史链是 **15.37x**。两者**提示词和种子都不同**，所以那个对比是**混淆的**
（content-dependent），不能直接拿来下结论。

本脚本用**历史链的原始提示词 + 原始种子**跑受控对照，
把「内容差异」和「代码/参数差异」拆开：

  A  old_baseline : 老提示词+老种子，steps=4 trans=3 rho=0  → 复现 15.37x 吗？
  B  old_trans2   : 同上，只把 transition_step 改 2（NFE 2/2 而不是 3/1）
  C  old_steps7   : 同上，steps=7 trans=5
  D  old_off      : 同上，二采关闭（对照）
  E  old_lowres1  : 同上，lowres_scale=1.0（**完全不做降采样**，诊断用）
  F  old_rho06    : 同上，rho=0.6（开像素锚点）

【唯一变量原则】
每档只改**一个**参数，其余与 A 完全一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import live_twopass_seam_matrix as M  # noqa: E402

# ★ 历史链的原始提示词与种子（job 9bd34ada0036 / 6f477813754c）
OLD_PROMPT = "一只橘猫在草地上奔跑，镜头跟拍"
OLD_SEED = 20261117

BASE_TP = {
    "enabled": True, "transition_step": 3, "lowres_scale": 0.75,
    "rho": 0.0, "w_min": 0.8, "w_max": 1.0,
    "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
    "cfg": 1.0,
}

OFF_TP = {"enabled": False}


def tp(**over):
    out = dict(BASE_TP)
    out.update(over)
    return out


REPRO = [
    ("A_old_baseline", BASE_TP, 4),
    ("B_old_trans2",   tp(transition_step=2), 4),
    ("C_old_steps7",   tp(transition_step=5), 7),
    ("D_old_off",      OFF_TP, 4),
    ("E_old_lowres1",  tp(lowres_scale=1.0), 4),
    ("F_old_rho06",    tp(rho=0.6), 4),
]


def main():
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = M.OUT_ROOT / f"twopass-seam-repro-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"started_at": stamp, "prompt": OLD_PROMPT, "seed": OLD_SEED,
              "runs": []}

    # 覆盖提示词/种子（module 级，让 run_chain 用老参数）
    orig_base = M.base_params

    def patched(twopass, steps, seed):
        p = orig_base(twopass, steps, seed)
        p["prompt"] = OLD_PROMPT
        return p

    M.base_params = patched

    for tag, tpv, steps in REPRO:
        if a.only and tag not in a.only:
            continue
        # run_chain 里用 seed+1 作为段2 的种子
        try:
            report["runs"].append(M.run_chain(tag, tpv, steps, OLD_SEED))
        except Exception as exc:
            report["runs"].append({"tag": tag, "error": str(exc)})
            print(f"[{tag}] 异常: {exc}", flush=True)
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n════════ 受控复现汇总 ════════")
    print(f"{'tag':18} {'拼接跳变':>9} {'拼接比值':>9} {'段2断点':>9} {'段内比值':>9} {'高频比':>8}")
    for r in report["runs"]:
        m = (r.get("metric") or {}).get("attribution")
        if not m:
            print(f"{r.get('tag'):18} {r.get('error') or r.get('measure_error') or 'N/A'}")
            continue
        mm = r["metric"]
        print(f"{r['tag']:18} {mm['seam_rgb_jump']:>9} {mm['seam_ratio_vs_all_median']:>9} "
              f"{m['seg2_head_break_l1']:>9} {m['seg2_head_break_ratio']:>9} "
              f"{mm['hf_energy_ratio_after_over_before']:>8}")

    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
