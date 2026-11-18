#!/usr/bin/env python
"""最终判定统计：把「接缝亮度台阶」与「段2 内部光照事件」两个量分开。

指标 A（判定用）：seam_lum_delta = lum[段2 首帧] - lum[段1 末帧]
      —— 接缝两端的亮度台阶，直接回答「接续是否造成亮度跳变」。
指标 B（诊断用）：seg2_lum_drop_3frames = lum[段2 首帧] - lum[段2 第4帧]
      —— 段2 内部前三帧的亮度演化，受段内光照事件主导。
指标 C（对照）：twopass ON 对照链（513383284a58）

判定规则（对应任务口径）：
  (a) 单样本波动 = A 的 |均值| 明显 < 门槛 0.3 且方向不一致（正负都有）
  (b) 系统性偏暗 = A 的均值显著偏离 0 且方向一致
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

EV = Path("/root/projects/h3studio-flf-lum-20261118/evidence/"
          "evidence-20261118-flf-lum-closure")
CONTROL = "513383284a58"

# 样本来源标注（与 seam_local_delta_all.json 的 job2 顺序一致）
SOURCES = {
    # 本轮 · 素材B(e2e-sample-3603235-87321.png) · seed 20261121-20261125
    "b9019c6084d0": ("本轮-素材B", 20261121, "B"),
    "212ca12ffa6e": ("本轮-素材B", 20261122, "B"),
    "7b7b36d7ed79": ("本轮-素材B", 20261123, "B"),
    "75bf29d7906b": ("本轮-素材B", 20261124, "B"),
    "8cb1303c0c64": ("本轮-素材B", 20261125, "B"),
    # 本轮 · 素材A(i-zimage-000301.png) 受控组 · seed 20261126-20261130
    "0c95b4b4f137": ("本轮-素材A受控", 20261126, "A"),
    "216522708331": ("本轮-素材A受控", 20261127, "A"),
    "c1620f430e19": ("本轮-素材A受控", 20261128, "A"),
    "a5bcf9f9b409": ("本轮-素材A受控", 20261129, "A"),
    "965b6e0506db": ("本轮-素材A受控", 20261130, "A"),
    # 上一轮 R1 · 素材A
    "c3264ca57a71": ("上一轮R1-素材A", 20261118, "A"),
    "41b7cc9dde0c": ("上一轮R1-素材A", 20261119, "A"),
    "5059526c4075": ("上一轮R1-素材A", 20261120, "A"),
    # 更早历史链 · 素材A
    "dcd2581e3482": ("更早历史链-素材A", None, "A"),
}


def st(vals: list[float]) -> dict:
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    return {
        "n": len(vals),
        "values_raw": [round(v, 4) for v in vals],
        "mean": round(statistics.fmean(vals), 4),
        "median": round(statistics.median(vals), 4),
        "std_dev_s": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
        "range": round(max(vals) - min(vals), 4),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "sign_pos": pos, "sign_neg": neg,
        "over_0.3": sum(1 for v in vals if v > 0.3),
        "under_-0.3": sum(1 for v in vals if v < -0.3),
    }


def main() -> int:
    rows = json.loads((EV / "seam_local_delta_all.json").read_text(encoding="utf-8"))
    pool = [r for r in rows if r["job2"] in SOURCES]
    ctrl = [r for r in rows if r["job2"] == CONTROL]
    assert len(pool) == 14, f"样本池应 14 条，实际 {len(pool)}"

    A = [r["seam_lum_delta"] for r in pool]
    B = [r["seg2_lum_drop_3frames"] for r in pool]
    C_ratio = [r["seg2_head_break_ratio"] for r in pool]
    D = [r["seam_L1"] for r in pool]

    out = {
        "metric_A_seam_lum_delta": {
            "definition": "lum[段2 首帧] - lum[段1 末帧]（接缝两端亮度台阶）",
            "stats": st(A),
        },
        "metric_B_seg2_lum_drop_3frames": {
            "definition": "lum[段2 首帧] - lum[段2 第4帧]（段2 内部前三帧亮度演化）",
            "stats": st(B),
        },
        "metric_C_seg2_head_break_ratio": {"stats": st(C_ratio)},
        "metric_D_seam_L1": {"stats": st(D)},
        "sample_rows": [
            {**{k: r[k] for k in ("job2", "seg1_rel", "seg2_rel", "mode",
                                  "twopass", "seg1_last_lum", "seg2_first_lum",
                                  "seam_lum_delta", "seg2_lum_drop_3frames",
                                  "seg2_slope_first3_per_frame",
                                  "seg2_head_break_ratio", "seam_L1")},
             "origin": SOURCES[r["job2"]][0],
             "seed": SOURCES[r["job2"]][1],
             "material": SOURCES[r["job2"]][2]}
            for r in pool
        ],
        "twopass_on_control": ctrl,
    }

    # 分素材组统计（素材是本轮发现的主要混淆因子）
    groups = {}
    for r in pool:
        mat = SOURCES[r["job2"]][2]
        groups.setdefault(mat, []).append(r)
    out["by_material"] = {}
    for mat, grp in sorted(groups.items()):
        ga = [r["seam_lum_delta"] for r in grp]
        gb = [r["seg2_lum_drop_3frames"] for r in grp]
        out["by_material"][mat] = {
            "n": len(grp),
            "seam_lum_delta": st(ga),
            "seg2_lum_drop_3frames": st(gb),
        }

    # 判定
    sa = out["metric_A_seam_lum_delta"]["stats"]
    direction_mixed = sa["sign_pos"] > 0 and sa["sign_neg"] > 0
    mean_far_below_thr = abs(sa["mean"]) < 0.3
    verdict_a = mean_far_below_thr and direction_mixed
    out["verdict"] = {
        "criterion": "(a) 单样本波动：|均值| < 0.3 且方向不一致",
        "metric_A_mean": sa["mean"],
        "metric_A_abs_mean": round(abs(sa["mean"]), 4),
        "threshold": 0.3,
        "mean_far_below_threshold": mean_far_below_thr,
        "direction_mixed": direction_mixed,
        "sign_pos": sa["sign_pos"], "sign_neg": sa["sign_neg"],
        "verdict_a_single_sample_noise": verdict_a,
        "systematic_dark_bias": (not verdict_a) and (
            abs(sa["mean"]) >= 0.3 and
            (sa["sign_pos"] == 0 or sa["sign_neg"] == 0)),
    }
    (EV / "final_verdict_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 92)
    print(f"最终判定统计（样本池 n={len(pool)}）")
    print("=" * 92)
    print(f"{'来源':<14}{'seed':<10}{'job2':<14}{'素材':<6}"
          f"{'接缝亮度差(A)':>14}{'段2三帧降(B)':>14}{'头断点比':>10}{'拼接L1':>10}")
    for r in out["sample_rows"]:
        print(f"{r['origin']:<14}{str(r['seed']):<10}{r['job2']:<14}"
              f"{r['material']:<6}{r['seam_lum_delta']:>+14.4f}"
              f"{r['seg2_lum_drop_3frames']:>+14.4f}"
              f"{r['seg2_head_break_ratio']:>10.4f}{r['seam_L1']:>10.4f}")
    print("-" * 92)
    for key, name in (("metric_A_seam_lum_delta", "A 接缝亮度台阶"),
                      ("metric_B_seg2_lum_drop_3frames", "B 段2 三帧亮度降"),
                      ("metric_C_seg2_head_break_ratio", "C 段2 头断点比"),
                      ("metric_D_seam_L1", "D 拼接点 L1")):
        s = out[key]["stats"]
        print(f"\n{name}: n={s['n']}")
        print(f"  原始值 = {s['values_raw']}")
        print(f"  均值={s['mean']:+.4f} 中位数={s['median']:+.4f} "
              f"标准差={s['std_dev_s']:.4f} 极差={s['range']:.4f} "
              f"min={s['min']:+.4f} max={s['max']:+.4f}")
        print(f"  符号计数: 正={s['sign_pos']} 负={s['sign_neg']} | "
              f"超+0.3: {s['over_0.3']}/{s['n']} 低于-0.3: {s['under_-0.3']}/{s['n']}")
    print("\n--- 二采 ON 对照（测量器鉴别力自证）---")
    for c in ctrl:
        print(f"  job={c['job2']} mode={c['mode']} twopass={c['twopass']}")
        print(f"    A 接缝亮度差={c['seam_lum_delta']:+.4f}  "
              f"B 段2三帧降={c['seg2_lum_drop_3frames']:+.4f}  "
              f"C 头断点比={c['seg2_head_break_ratio']:.4f}  "
              f"D 拼接L1={c['seam_L1']:.4f}")
    print("\n--- 分素材组（素材 = 主要混淆因子）---")
    for mat, g in out["by_material"].items():
        sa_g, sb_g = g["seam_lum_delta"], g["seg2_lum_drop_3frames"]
        print(f"  素材{mat}: n={g['n']}")
        print(f"    A 接缝亮度差 原始值={sa_g['values_raw']}")
        print(f"      均值={sa_g['mean']:+.4f} 中位数={sa_g['median']:+.4f} "
              f"标准差={sa_g['std_dev_s']:.4f} 极差={sa_g['range']:.4f} "
              f"符号 正{sa_g['sign_pos']}/负{sa_g['sign_neg']}")
        print(f"    B 段2三帧降  原始值={sb_g['values_raw']}")
        print(f"      均值={sb_g['mean']:+.4f} 中位数={sb_g['median']:+.4f} "
              f"标准差={sb_g['std_dev_s']:.4f} 极差={sb_g['range']:.4f} "
              f"符号 正{sb_g['sign_pos']}/负{sb_g['sign_neg']}")
    print("\n--- 判定 ---")
    for k, v in out["verdict"].items():
        print(f"  {k}: {v}")
    print(f"\n  落盘: {EV / 'final_verdict_stats.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
