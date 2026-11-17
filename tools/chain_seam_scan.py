#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动发现接续链并批量测量接缝（2026-11-17 二采残缺陷）。

【为什么自动发现】
手工传 seg1/seg2 容易传反（本轮就踩了一次：rho=0.6 那条链把
段1/段2 传反了，测出来的「跨段跳变 +12.1」是伪值）。
本脚本从线上 API 的 job 列表 + continuation 上下文**自动**推出
每条链的 (full, seg1, seg2) 三元组，再调用 seam_metric 测量。

【链的推导规则】
  * job 的 outputs 里 is_full=True 的那个 = 成片
  * chain 里 stage_index 最小的 = 段1
  * chain 里 stage_index 最大的 = 段2
  * 段1/段2 的 output 文件名 = 该 job 的非 is_full output
"""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8190"
# outputs 里的 file 字段已经是「videos/xxx.mp4」这种相对路径，
# 所以这里要用 **outputs 根目录**，不能再拼一层 videos/。
OUT_ROOT = Path("/root/projects/comfyui-minimax-h3-studio/data/outputs")
HERE = Path(__file__).resolve().parent


def api(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


def dig(o, k):
    if isinstance(o, dict):
        if k in o:
            return o[k]
        for v in o.values():
            r = dig(v, k)
            if r is not None:
                return r
    elif isinstance(o, list):
        for v in o:
            r = dig(v, k)
            if r is not None:
                return r
    return None


def discover():
    jobs = api("/api/jobs")
    if not jobs:
        return []
    js = jobs.get("jobs", jobs) if isinstance(jobs, dict) else jobs
    chains = {}
    for j in js:
        jid = j.get("id")
        if not jid:
            continue
        outs = dig(j, "outputs") or []
        files = {}
        for o in outs:
            if isinstance(o, dict) and o.get("file"):
                files[o["file"]] = bool(o.get("is_full"))
        cont = api(f"/api/jobs/{jid}/continuation") or {}
        ctx = dig(cont, "context") or dig(cont, "continuation") or {}
        chain = ctx.get("chain_id")
        stage = ctx.get("stage_index")
        tp = dig(j, "twopass") or {}
        rec = {
            "job": jid,
            "stage": stage,
            "full": next((f for f, isf in files.items() if isf), None),
            "seg": next((f for f, isf in files.items() if not isf), None),
            "twopass": bool(tp.get("enabled")),
            "transition_step": tp.get("transition_step"),
            "rho": tp.get("rho"),
            "lowres_scale": tp.get("lowres_scale"),
            "steps": dig(j, "steps"),
            "seed": dig(j, "seed"),
            "mode": dig(j, "mode"),
        }
        if chain is None or stage is None:
            continue
        chains.setdefault(chain, []).append(rec)
    out = []
    for chain, recs in chains.items():
        recs.sort(key=lambda r: (r["stage"] if r["stage"] is not None else 99))
        if len(recs) < 2:
            continue
        s1 = recs[0]
        s2 = recs[-1]
        if not (s1["seg"] and s2["seg"] and s2["full"]):
            continue
        out.append({
            "chain": chain,
            "full": s2["full"], "seg1": s1["seg"], "seg2": s2["seg"],
            "meta": s2,
            "meta1": s1,
        })
    return out


def main():
    chains = discover()
    if not chains:
        print("没发现链")
        return 1
    print(f"发现 {len(chains)} 条接续链\n")
    results = []
    for c in chains:
        m = c["meta"]
        tag = (f"{'二采ON ' if m['twopass'] else '二采OFF'} "
               f"trans={m['transition_step']} rho={m['rho']} "
               f"steps={m['steps']} seed={m['seed']} mode={m['mode']} "
               f"chain={c['chain'][:8]}")
        full = OUT_ROOT / c["full"]
        s1 = OUT_ROOT / c["seg1"]
        s2 = OUT_ROOT / c["seg2"]
        if not (full.is_file() and s1.is_file() and s2.is_file()):
            print(f"  [skip] {tag} —— 文件缺失")
            continue
        jf = f"/tmp/chain_{c['chain']}.json"
        rc = subprocess.run(
            [sys.executable, str(HERE / "seam_metric.py"),
             str(full), str(s1), str(s2), "--json", jf],
            capture_output=True, text=True, timeout=600)
        if rc.returncode != 0:
            print(f"  [FAIL] {tag}: {rc.stderr[-200:]}")
            continue
        d = json.loads(Path(jf).read_text())
        a = d["attribution"]
        results.append({"chain": c["chain"], "tag": tag, "data": d})
        print(f"  {tag}")
        print(f"      帧数 full={d['frames']['full']} = seg1={d['frames']['seg1']}"
              f" + seg2={d['frames']['seg2']}  守恒={d['frames']['conserved']}"
              f"  接续点帧={d['seam_frame_index']}")
        print(f"      拼接点跳变(L1)={d['seam_rgb_jump']}  比值(全片中位)={d['seam_ratio_vs_all_median']}")
        print(f"      ★段2内部断点(帧0->1)={a['seg2_head_break_l1']}  "
              f"段2正常帧间={a['seg2_internal_scale_median']}  "
              f"段内比值={a['seg2_head_break_ratio']}")
        print(f"      高频能量 缝后/缝前={d['hf_energy_ratio_after_over_before']}")
        print(f"      段1尾3亮度={a['seg1_tail_luma'][-3:]}  段2头3亮度={a['seg2_head_luma'][:3]}")
        print()
    Path("/tmp/all_chains.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[saved] /tmp/all_chains.json  ({len(results)} 条)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
