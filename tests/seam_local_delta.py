#!/usr/bin/env python
"""接缝局部亮度诊断：对每条链算「接缝处真实亮度差」。

关键区分：
  · lum_drop_3frames = lum[seg2 首帧] - lum[seg2 第4帧]
      —— 这是「段2 内部前 3 帧的亮度演化」，会被段内光照事件（闪光/开灯）主导，
         并非接缝本身的指标。
  · seam_lum_delta = lum[seg2 首帧] - lum[seg1 末帧]
      —— 这才是「接缝两端的亮度差」，直接反映接续是否造成亮度台阶。

用法： seam_local_delta.py <job2_id> [<job2_id> ...] [--out <json>]
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


def one(job2: str) -> dict:
    info = seam.chain_segments(job2)
    if not info or len(info.get("segs") or []) < 2:
        return {"job2": job2, "error": "段数不足"}
    s1rel, s2rel = info["segs"][0], info["segs"][1]
    S1 = seam.frames(str(OUT / s1rel))
    S2 = seam.frames(str(OUT / s2rel))
    if S1 is None or S2 is None:
        return {"job2": job2, "error": "读不到帧", "segs": info["segs"]}
    l1 = S1.mean(axis=(1, 2, 3))
    l2 = S2.mean(axis=(1, 2, 3))
    d2 = [seam.l1(S2[i], S2[i + 1]) for i in range(len(S2) - 1)]
    med = float(sorted(d2[1:])[len(d2[1:]) // 2]) if len(d2) > 2 else None
    head_ratio = (d2[0] / med) if (med and med > 0) else None
    return {
        "job2": job2,
        "seg1_rel": s1rel, "seg2_rel": s2rel,
        "seg1_frames": len(S1), "seg2_frames": len(S2),
        "mode": info.get("mode"), "twopass": info.get("twopass"),
        "seg1_last_lum": float(l1[-1]),
        "seg2_first_lum": float(l2[0]),
        "seam_lum_delta": float(l2[0] - l1[-1]),
        "seg2_lum_drop_3frames": float(l2[0] - l2[3]) if len(l2) > 3 else None,
        "seg2_slope_first3_per_frame": (
            float((l2[3] - l2[0]) / 3) if len(l2) > 3 else None),
        "seg2_head_break_ratio": head_ratio,
        "seg2_head_lum12": [round(float(x), 3) for x in l2[:12]],
        "seg1_tail_lum6": [round(float(x), 3) for x in l1[-6:]],
        "seam_L1": seam.l1(S1[-1], S2[0]),
    }


def main(argv: list[str]) -> int:
    out_path = None
    ids: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--out":
            out_path = argv[i + 1]
            i += 2
            continue
        ids.append(argv[i])
        i += 1

    rows = []
    for jid in ids:
        r = one(jid)
        rows.append(r)
        if "error" in r:
            print(f"  {jid}: {r['error']}")
            continue
        print(f"  {jid}: 接缝亮度差={r['seam_lum_delta']:+.4f}  "
              f"段2三帧降={r['seg2_lum_drop_3frames']:+.4f}  "
              f"段2前3帧斜率={r['seg2_slope_first3_per_frame']:+.4f}/帧  "
              f"头断点比={r['seg2_head_break_ratio']:.4f}  "
              f"拼接L1={r['seam_L1']:.4f}")
    if out_path:
        Path(out_path).write_text(json.dumps(rows, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\n  落盘: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
