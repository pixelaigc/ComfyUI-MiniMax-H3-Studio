#!/usr/bin/env python
"""判定用最终分析：接缝台阶 vs 段首自然收敛基线。

核心检验：
  接缝亮度台阶(A) = lum[段2 首帧] - lum[段1 末帧]
  段首自然收敛(N) = lum[段1 首帧] - lum[段1 第4帧]
                     （同一条链里，段1 是「不涉及接续」的独立生成，
                       它开头的 3 帧收敛幅度就是模型在首帧锚点上的固有行为）

若 A ≈ N ⇒ 接缝没有引入额外亮度台阶（差值只是模型首帧收敛的固有量）。
"""
from __future__ import annotations

import importlib.util
import json
import statistics
from pathlib import Path

SEAM = Path(__file__).with_name("live_flf_seam_repeat.py")
spec = importlib.util.spec_from_file_location("seam_mod", SEAM)
assert spec is not None and spec.loader is not None
seam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seam)

OUT = seam.REPO_DATA_OUT

# job2 -> (来源, seed, 素材, 采样路径说明)
POOL = {
    "b9019c6084d0": ("本轮-素材B", 20261121, "B"),
    "212ca12ffa6e": ("本轮-素材B", 20261122, "B"),
    "7b7b36d7ed79": ("本轮-素材B", 20261123, "B"),
    "75bf29d7906b": ("本轮-素材B", 20261124, "B"),
    "8cb1303c0c64": ("本轮-素材B", 20261125, "B"),
    "0c95b4b4f137": ("本轮-素材A受控", 20261126, "A"),
    "216522708331": ("本轮-素材A受控", 20261127, "A"),
    "c1620f430e19": ("本轮-素材A受控", 20261128, "A"),
    "a5bcf9f9b409": ("本轮-素材A受控", 20261129, "A"),
    "965b6e0506db": ("本轮-素材A受控", 20261130, "A"),
    "c3264ca57a71": ("上一轮R1-素材A", 20261118, "A"),
    "41b7cc9dde0c": ("上一轮R1-素材A", 20261119, "A"),
    "5059526c4075": ("上一轮R1-素材A", 20261120, "A"),
    "dcd2581e3482": ("更早历史链-素材A", None, "A"),
}


def one(job2: str) -> dict:
    info = seam.chain_segments(job2)
    s1rel, s2rel = info["segs"][0], info["segs"][1]
    S1 = seam.frames(str(OUT / s1rel))
    S2 = seam.frames(str(OUT / s2rel))
    l1 = S1.mean(axis=(1, 2, 3))
    l2 = S2.mean(axis=(1, 2, 3))
    return {
        "job2": job2,
        "seg1_rel": s1rel, "seg2_rel": s2rel,
        "seam_lum_delta_A": float(l2[0] - l1[-1]),
        "seg1_head_settling_N": float(l1[0] - l1[3]),
        "seg2_head_settling_B": float(l2[0] - l2[3]),
        "seam_excess_over_N": float((l2[0] - l1[-1]) - (l1[0] - l1[3])),
        "seam_L1": seam.l1(S1[-1], S2[0]),
    }


def st(vals):
    return {
        "n": len(vals),
        "values_raw": [round(v, 4) for v in vals],
        "mean": round(statistics.fmean(vals), 4),
        "median": round(statistics.median(vals), 4),
        "std_dev_s": round(statistics.stdev(vals), 4) if len(vals) > 1 else 0.0,
        "range": round(max(vals) - min(vals), 4),
        "sign_pos": sum(1 for v in vals if v > 0),
        "sign_neg": sum(1 for v in vals if v < 0),
    }


def main() -> int:
    rows = [one(j) for j in POOL]
    for r in rows:
        o, s, m = POOL[r["job2"]]
        r["origin"], r["seed"], r["material"] = o, s, m

    A = [r["seam_lum_delta_A"] for r in rows]
    N = [r["seg1_head_settling_N"] for r in rows]
    E = [r["seam_excess_over_N"] for r in rows]

    # 配对检验（同一条链内 A 与 N 的差）
    diffs = E
    paired_t_like = (statistics.fmean(diffs) /
                     (statistics.stdev(diffs) / len(diffs) ** 0.5)) if len(diffs) > 1 else 0.0

    out = {
        "n_total": len(rows),
        "rows": rows,
        "A_seam_lum_delta": st(A),
        "N_seg1_head_settling": st(N),
        "E_seam_excess_over_N": st(E),
        "paired_mean_diff": round(statistics.fmean(diffs), 4),
        "paired_t_stat": round(paired_t_like, 3),
        "by_material": {},
    }
    for mat in ("A", "B"):
        grp = [r for r in rows if r["material"] == mat]
        out["by_material"][mat] = {
            "n": len(grp),
            "A": st([r["seam_lum_delta_A"] for r in grp]),
            "N": st([r["seg1_head_settling_N"] for r in grp]),
            "E": st([r["seam_excess_over_N"] for r in grp]),
        }

    ev = Path("/root/projects/h3studio-flf-lum-20261118/evidence/"
              "evidence-20261118-flf-lum-closure/seam_vs_natural_settling.json")
    ev.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 100)
    print(f"接缝台阶(A) vs 段首自然收敛(N) —— n={len(rows)}")
    print("=" * 100)
    print(f"{'来源':<18}{'seed':<10}{'job2':<14}{'素材':<5}"
          f"{'A接缝台阶':>12}{'N段1自然收敛':>14}{'E=A-N超额':>12}{'B段2三帧降':>12}")
    for r in rows:
        print(f"{r['origin']:<18}{str(r['seed']):<10}{r['job2']:<14}"
              f"{r['material']:<5}{r['seam_lum_delta_A']:>+12.4f}"
              f"{r['seg1_head_settling_N']:>+14.4f}{r['seam_excess_over_N']:>+12.4f}"
              f"{r['seg2_head_settling_B']:>+12.4f}")
    for name, key in (("A 接缝亮度台阶", "A_seam_lum_delta"),
                      ("N 段1 首帧自然收敛(不涉及接续)", "N_seg1_head_settling"),
                      ("E 超额 = A - N", "E_seam_excess_over_N")):
        s = out[key]
        print(f"\n{name}: n={s['n']}")
        print(f"  原始值 = {s['values_raw']}")
        print(f"  均值={s['mean']:+.4f} 中位数={s['median']:+.4f} "
              f"标准差={s['std_dev_s']:.4f} 极差={s['range']:.4f} "
              f"符号 正{s['sign_pos']}/负{s['sign_neg']}")
    print(f"\n  配对差均值 = {out['paired_mean_diff']:+.4f}  "
          f"配对 t 统计量 = {out['paired_t_stat']}（|t| < 2.16 ⇒ p > 0.05，"
          f"不能拒绝 A 与 N 无差异）")
    print("\n--- 分素材 ---")
    for mat, g in out["by_material"].items():
        print(f"  素材{mat} n={g['n']}")
        print(f"    A 均值={g['A']['mean']:+.4f} 符号 正{g['A']['sign_pos']}/负{g['A']['sign_neg']}")
        print(f"    N 均值={g['N']['mean']:+.4f} 符号 正{g['N']['sign_pos']}/负{g['N']['sign_neg']}")
        print(f"    E 均值={g['E']['mean']:+.4f} 符号 正{g['E']['sign_pos']}/负{g['E']['sign_neg']}")
    print(f"\n  落盘: {ev}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
