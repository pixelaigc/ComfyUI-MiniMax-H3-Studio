#!/usr/bin/env python
"""flf 段2 三帧亮度降 —— 扩样本统计与判定（≥9 条样本池）

口径：等价 L1（逐帧 RGB 的 L1 均值差，320x180 缩放解码，numpy 均值），
与 tests/live_flf_seam_repeat.py 内 analyze()/frames()/l1() 完全同一实现。

用法：
  /home/ubuntu/comfyui/venv/bin/python tests/seam_lum_closure_stats.py \
      --new-dir <本轮证据目录> \
      --old-json <上轮 seam_repeat.json> \
      --historical dcd2581e3482 \
      --control 513383284a58 \
      --out <统计结果 json>
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path

SEAM = Path(__file__).with_name("live_flf_seam_repeat.py")
spec = importlib.util.spec_from_file_location("seam_mod", SEAM)
assert spec is not None and spec.loader is not None
seam = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seam)


def stats(values: list[float]) -> dict:
    if not values:
        return {}
    vals = sorted(values)
    pos = sum(1 for v in values if v > 0)
    neg = sum(1 for v in values if v < 0)
    zero = sum(1 for v in values if v == 0)
    return {
        "n": len(values),
        "values_raw": values,
        "values_sorted": vals,
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "std_dev_s": statistics.stdev(values) if len(values) > 1 else 0.0,
        "range": max(values) - min(values),
        "min": min(values),
        "max": max(values),
        "sign_pos": pos,
        "sign_neg": neg,
        "sign_zero": zero,
        "over_threshold_0.3": sum(1 for v in values if v > 0.3),
        "under_threshold_-0.3": sum(1 for v in values if v < -0.3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new-dir", required=True, help="本轮证据目录（含 seam_repeat*.json）")
    ap.add_argument("--old-json", nargs="*", default=[],
                    help="上一轮 seam_repeat.json（可多个）")
    ap.add_argument("--historical", nargs="*", default=[],
                    help="历史链 job id（用同一实现现场复算）")
    ap.add_argument("--control", nargs="*", default=[],
                    help="二采 ON 对照链 job id（测量器鉴别力自证）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows: list[dict] = []

    # ---- 本轮新样本 ----
    new_dir = Path(args.new_dir)
    new_json = sorted(new_dir.glob("seam-repeat*/seam_repeat.json"))
    for jf in new_json:
        data = json.loads(jf.read_text(encoding="utf-8"))
        for r in data.get("stats") or []:
            rows.append({
                "origin": "本轮(20261121起)",
                "tag": r.get("tag"), "seed": r.get("seed"),
                "job2": r.get("job2"),
                "job2_prompt_id": r.get("job2_prompt_id"),
                "job2_sampler_nodes": r.get("job2_sampler_nodes"),
                "job2_selflift_count": r.get("job2_selflift_count"),
                "job2_twopass_in_db": r.get("job2_twopass_in_db"),
                "seg2_ratio": r.get("seg2_ratio"),
                "seg2_lum_drop": r.get("seg2_lum_drop"),
                "seam_L1": r.get("seam_L1"),
            })

    # ---- 上一轮样本（读 json，避免重复生成）----
    for oj in args.old_json:
        data = json.loads(Path(oj).read_text(encoding="utf-8"))
        for r in data.get("stats") or []:
            rows.append({
                "origin": "上一轮(R1)",
                "tag": r.get("tag"), "seed": r.get("seed"),
                "job2": r.get("job2"),
                "job2_prompt_id": r.get("job2_prompt_id"),
                "job2_sampler_nodes": r.get("job2_sampler_nodes"),
                "job2_selflift_count": r.get("job2_selflift_count"),
                "job2_twopass_in_db": r.get("job2_twopass_in_db"),
                "seg2_ratio": r.get("seg2_ratio"),
                "seg2_lum_drop": r.get("seg2_lum_drop"),
                "seam_L1": r.get("seam_L1"),
            })

    # ---- 历史链：现场用同一实现复算 ----
    for jid in args.historical:
        a = seam.analyze(jid)
        segs = a.get("segments") or []
        if len(segs) < 2:
            print(f"  !! 历史链 {jid} 段数不足: {segs}")
            continue
        s2 = segs[1]
        seam_l1 = (a.get("seams") or [{}])[0].get("seam_L1")
        rows.append({
            "origin": "更早历史链",
            "tag": f"hist-{jid}", "seed": None,
            "job2": jid, "job2_prompt_id": None,
            "job2_sampler_nodes": None,
            "job2_selflift_count": None,
            "job2_twopass_in_db": a.get("twopass"),
            "seg2_ratio": s2.get("head_break_ratio"),
            "seg2_lum_drop": s2.get("lum_drop_3frames"),
            "seam_L1": seam_l1,
        })

    # ---- 二采 ON 对照（测量器鉴别力自证）----
    controls = []
    for jid in args.control:
        a = seam.analyze(jid)
        segs = a.get("segments") or []
        if len(segs) < 2:
            continue
        s2 = segs[1]
        controls.append({
            "job": jid, "mode": a.get("mode"), "twopass": a.get("twopass"),
            "seg2_ratio": s2.get("head_break_ratio"),
            "seg2_lum_drop": s2.get("lum_drop_3frames"),
            "seam_L1": (a.get("seams") or [{}])[0].get("seam_L1"),
        })

    lum = [r["seg2_lum_drop"] for r in rows if r.get("seg2_lum_drop") is not None]
    ratio = [r["seg2_ratio"] for r in rows if r.get("seg2_ratio") is not None]
    seaml1 = [r["seam_L1"] for r in rows if r.get("seam_L1") is not None]

    out = {
        "metric": "等价 L1 口径（逐帧 RGB L1 均值差，320x180，numpy 均值）",
        "sample_rows": rows,
        "sample_count": len(rows),
        "stats": {
            "seg2_lum_drop": stats(lum),
            "seg2_head_break_ratio": stats(ratio),
            "seam_L1": stats(seaml1),
        },
        "twopass_on_controls": controls,
        "all_seg2_standard_sampler": all(
            (r.get("job2_selflift_count") == 0) for r in rows
            if r.get("job2_selflift_count") is not None),
        "all_seg2_twopass_disabled": all(
            (r.get("job2_twopass_in_db") is False) for r in rows
            if r.get("job2_twopass_in_db") is not None),
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    print("=" * 78)
    print(f"样本池 = {len(rows)} 条（判定池：flf 段2 三帧亮度降）")
    print("=" * 78)
    print(f"{'来源':<14}{'seed':<10}{'job2':<14}{'采样节点':<34}"
          f"{'头断点比':>10}{'亮度降':>10}{'拼接L1':>10}")
    for r in rows:
        nodes = ",".join(r.get("job2_sampler_nodes") or []) or "-"
        print(f"{r['origin']:<14}{str(r.get('seed')):<10}{str(r.get('job2')):<14}"
              f"{nodes:<34}"
              f"{(r.get('seg2_ratio') or 0):>10.4f}"
              f"{(r.get('seg2_lum_drop') or 0):>+10.4f}"
              f"{(r.get('seam_L1') or 0):>10.4f}")
    print("-" * 78)
    for key, name in (("seg2_lum_drop", "段2 三帧亮度降"),
                      ("seg2_head_break_ratio", "段2 头断点比"),
                      ("seam_L1", "拼接点 L1")):
        s = out["stats"][key]
        if not s:
            continue
        print(f"\n{name}: n={s['n']}")
        print(f"  原始值 = {[round(v, 4) for v in s['values_raw']]}")
        print(f"  均值={s['mean']:+.4f} 中位数={s['median']:+.4f} "
              f"标准差={s['std_dev_s']:.4f} 极差={s['range']:.4f} "
              f"min={s['min']:+.4f} max={s['max']:+.4f}")
        print(f"  符号计数: 正={s['sign_pos']} 负={s['sign_neg']} 零={s['sign_zero']}")
        if key == "seg2_lum_drop":
            print(f"  超 +0.3 门槛: {s['over_threshold_0.3']}/{s['n']} 条; "
                  f"低于 -0.3: {s['under_threshold_-0.3']}/{s['n']} 条")
    print("\n--- 二采 ON 对照（测量器鉴别力自证）---")
    for c in controls:
        print(f"  job={c['job']} mode={c['mode']} twopass={c['twopass']} "
              f"头断点比={c['seg2_ratio']:.4f} 亮度降={c['seg2_lum_drop']:+.4f} "
              f"拼接L1={c['seam_L1']:.4f}")
    print(f"\n  全部样本段2 走标准采样(无 SelfLift)? {out['all_seg2_standard_sampler']}")
    print(f"  全部样本段2 twopass 落库为 False? {out['all_seg2_twopass_disabled']}")
    print(f"\n统计落盘: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
