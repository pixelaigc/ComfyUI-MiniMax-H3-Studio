#!/usr/bin/env python
"""接缝逐帧亮度轨迹诊断：区分「接缝处跳变」与「段2 内部渐变」。

对每条链输出：
  · 段1 末 6 帧逐帧亮度
  · 段2 首 12 帧逐帧亮度
  · 接缝差（seg1 末帧 vs seg2 首帧）
  · 段2 内部 3 帧斜率（lum[3]-lum[0]）/3
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

SEAM = Path(__file__).with_name("live_flf_seam_repeat.py")
spec = importlib.util.spec_from_file_location("seam_mod", SEAM)
assert spec is not None and spec.loader is not None
seam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seam)

OUT = seam.REPO_DATA_OUT


def trace(rel: str, tail: int = 6, head: int = 12) -> dict:
    S = seam.frames(str(OUT / rel))
    if S is None:
        return {"error": "读不到", "rel": rel}
    lum = S.mean(axis=(1, 2, 3))
    n = len(lum)
    return {
        "rel": rel, "frames": n,
        "tail_lum": [round(float(x), 3) for x in lum[max(0, n - tail):]],
        "head_lum": [round(float(x), 3) for x in lum[:head]],
        "lum_first": float(lum[0]),
        "lum_frame3": float(lum[3]) if n > 3 else None,
        "lum_last": float(lum[-1]),
        "seam_delta": float(lum[0] - lum[-1]),
        "slope_first3_per_frame": (
            float((lum[3] - lum[0]) / 3) if n > 3 else None),
        "monotone_head": bool(np_all_monotone(lum[:min(head, n)])),
    }


def np_all_monotone(v) -> bool:
    import numpy as np
    v = np.asarray(v)
    if len(v) < 3:
        return False
    inc = bool(np.all(np.diff(v) >= 0))
    dec = bool(np.all(np.diff(v) <= 0))
    return inc or dec


def main(tj: str) -> int:
    data = json.loads(Path(tj).read_text(encoding="utf-8"))
    for ch in data.get("chains") or []:
        if ch.get("job2_status") != "done":
            continue
        sm = ch.get("seam") or {}
        segs = sm.get("segments") or []
        if len(segs) < 2:
            continue
        s1, s2 = segs[0], segs[1]
        t1 = trace(s1["rel"])
        t2 = trace(s2["rel"])
        print(f"\n  === {ch.get('tag')} seed={ch.get('seed')} job2={ch.get('job2')} ===")
        print(f"    段1 {t1['rel']} ({t1['frames']}帧)")
        print(f"      末6帧亮度: {t1['tail_lum']}")
        print(f"      首12帧亮度: {t1['head_lum']}")
        print(f"    段2 {t2['rel']} ({t2['frames']}帧)")
        print(f"      首12帧亮度: {t2['head_lum']}")
        print(f"      末6帧亮度:  {t2['tail_lum']}")
        print(f"    接缝: 段1末帧亮度={t1['lum_last']:.3f} -> 段2首帧亮度={t2['lum_first']:.3f} "
              f"(差 {t2['seam_delta']:+.3f})")
        print(f"    段2 前3帧斜率 = {t2['slope_first3_per_frame']:+.3f} 灰阶/帧 "
              f"(单调={t2['monotone_head']})")
        print(f"    段2 三帧亮度降(lum[0]-lum[3]) = {s2.get('lum_drop_3frames'):+.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
