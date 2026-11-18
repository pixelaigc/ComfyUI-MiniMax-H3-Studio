#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总 r2v 接续链的段内跳变统计（第 2 / 第 3 项）。

【口径】
等价 L1（与上轮一致）：scaled RGB(1280) -> numpy mean -> 帧间 L1 均值差。
「比值」= 段内最大 L1 / 段内中位 L1 —— 衡量「最坏一跳相对该段正常
运动强度的倍数」，用来区分「整段都在剧烈运动」与「某一帧突变」。

【为什么要有对照段】
每条链都额外跑一条**同 seed、同素材、非接续**的段（ctrl）。
它是「接续」这个变量的**同组对照** —— 同一条素材、同一颗种子，
唯一差别就是有没有接续（有没有拷贝桥钉住区）。
⇒ seg2 与 ctrl 的差值，就是「接续」本身带来的增量。

用法：
  python3 tools/r2v_summary.py /tmp/r2v_expand.json
"""
from __future__ import annotations

import json
import statistics as st
import sys


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/r2v_expand.json"
    rows = json.load(open(path))
    print(f"═══ r2v 接续链段内跳变统计（{len(rows)} 条链，素材受控）═══")
    print()
    hdr = (f"{'seed':>10} {'段1 maxL1':>10} {'段1比值':>8} "
           f"{'段2 maxL1':>10} {'@帧':>5} {'段2比值':>8} "
           f"{'对照 maxL1':>11} {'对照比值':>9} {'差值':>8}")
    print(hdr)
    print("-" * len(hdr))
    s1v, s2v, cv, diffs = [], [], [], []
    for r in rows:
        s1, s2, ct = r["seg1"], r["seg2"], r.get("ctrl")
        if not ct:
            continue
        d = s2["max_l1_ratio"] - ct["max_l1_ratio"]
        diffs.append(d)
        s1v.append(s1["max_l1_ratio"])
        s2v.append(s2["max_l1_ratio"])
        cv.append(ct["max_l1_ratio"])
        print(f"{r['seed']:>10} {s1['max_l1']:>10.2f} {s1['max_l1_ratio']:>7.2f}x "
              f"{s2['max_l1']:>10.2f} {s2['max_l1_at_frame']:>5} "
              f"{s2['max_l1_ratio']:>7.2f}x {ct['max_l1']:>11.2f} "
              f"{ct['max_l1_ratio']:>8.2f}x {d:>+7.2f}x")
    if not s2v:
        print("（没有对照段数据）")
        return 1
    print()

    def stats(v, label):
        print(f"  {label}: 均值={st.mean(v):6.2f}x  中位={st.median(v):6.2f}x  "
              f"σ={st.pstdev(v):5.2f}  极差={max(v)-min(v):6.2f}  "
              f"min={min(v):5.2f}x  max={max(v):5.2f}x")

    stats(s1v, "段1（链首，非接续）")
    stats(cv, "对照段（非接续，同 seed）")
    stats(s2v, "段2（接续）      ")
    print()
    print(f"  接续段 − 对照段 逐条差: "
          f"{[round(d, 2) for d in diffs]}")
    print(f"  差值 均值={st.mean(diffs):+.2f}x  中位={st.median(diffs):+.2f}x  "
          f"σ={st.pstdev(diffs):.2f}")
    print(f"  「接续更差」的条数: {sum(1 for d in diffs if d > 0)}/{len(diffs)}")
    # 配对 t 检验的量级（n 小，只报量级不报 p 值结论）
    if len(diffs) > 1:
        sd = st.stdev(diffs)
        t = (st.mean(diffs) / (sd / (len(diffs) ** 0.5))) if sd else 0.0
        print(f"  配对 t = {t:.2f}（n={len(diffs)}，仅量级参考）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
