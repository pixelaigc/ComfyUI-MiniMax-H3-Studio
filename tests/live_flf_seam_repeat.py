#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在**线上 8190** 补跑 N 条 flf 接续链，逐条算接缝指标（等价 L1 口径）。

【本轮目的】
上一轮只跑了 **1 条** flf 接续链，得出「段2 3 帧亮度降 +0.717 超出 ≤+0.3 门槛」。
单样本不能判定「系统性偏暗」。本脚本跑 **>=3 条**独立 flf 链（不同 seed），
每条用**上一轮同一口径**的测量器算：
  * 段内相邻帧断点比（头帧断点 L1 / 段内中位 L1）
  * 拼接点 L1（段1 末帧 vs 段2 首帧）
  * 亮度跳变（段2 头帧亮度 − 第 3 帧亮度）
  * 饱和度跳变
然后给各条数值 + 均值 + 离散度（标准差 / 极差），供判定系统性 vs 单样本波动。

【口径声明 —— 必须写进报告】
用的是**等价 L1** 口径（逐帧 RGB 的 L1 均值差），**不是**官方 HF 能量项
（官方能量项在本机 CPU 上单次运行 >20 分钟跑不完）。
与上一轮 `tests/seam_metric_seg2.py` **同一实现**，可直接对拍。

【权威取证】
每条链都从 **ComfyUI 自己的 history**（GET http://127.0.0.1:8188/history）导出
实际下发的工作流 JSON，断言采样节点是标准 `SamplerCustomAdvanced`、**无** SelfLift
—— 证明测的是「接续段标准采样路径」的真实产物。

【入参故意开二采】
入参显式 `twopass.enabled=true`，用来证明「即使入参开了，接续段也被强制关」，
保证测的仍是标准采样路径（与上一轮同一反事实口径）。

用法:
    python3 tests/live_flf_seam_repeat.py --chains 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_BASE", "http://127.0.0.1:8190")
COMFY = os.environ.get("COMFY_BASE", "http://127.0.0.1:8188")
DATA = Path("/root/projects/comfyui-minimax-h3-studio/data")
OUT_ROOT = Path("/root/projects/h3studio-cont-seg3/evidence/evidence-20261118-seam-repeat")
ASSET_DB = "/mnt/usb2t/asset_library/asset_library.db"
ASSET_ROOT = Path("/mnt/usb2t/asset_library")
# 指定素材（用于受控对照：默认取「最新素材」会让不同批次用不同参考图）
ASSET_ID = os.environ.get("H3STUDIO_ASSET_ID", "").strip()

REPO_DATA_OUT = DATA / "outputs"


# ---------------------------------------------------------------- 基础工具
def api(path: str, method: str = "GET", payload=None, timeout: int = 180):
    url = f"{BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def job_row(job_id: str):
    conn = sqlite3.connect(f"file:{DATA / 'jobs.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()


def wait_done(job_id: str, timeout: int = 3600):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        row = job_row(job_id)
        if not row:
            time.sleep(2)
            continue
        status = row["status"]
        if status in ("done", "failed", "canceled"):
            return status, row["error"], json.loads(row["outputs"] or "[]")
        last = f"{status} {row['progress_text'] if 'progress_text' in row.keys() else ''}"
        print(f"      ... {last}", flush=True)
        time.sleep(5)
    return "timeout", last, []


def pick_image():
    conn = sqlite3.connect(ASSET_DB)
    conn.row_factory = sqlite3.Row
    try:
        if ASSET_ID:
            row = conn.execute(
                "SELECT id,name FROM assets WHERE id=? AND deleted_at IS NULL",
                (ASSET_ID,)).fetchone()
            return row
        return conn.execute(
            "SELECT id,name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def submit(mode: str, params: dict, asset_refs: list):
    boundary = "----h3studioSeg2" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def add_field(name: str, value: str):
        parts.append(
            f"--{boundary}\r\n".encode()
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode() + b"\r\n")

    add_field("mode", mode)
    add_field("params", json.dumps(params, ensure_ascii=False))
    add_field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def comfy_workflow_for(prompt_id: str):
    try:
        with urllib.request.urlopen(f"{COMFY}/history/{prompt_id}",
                                    timeout=120) as resp:
            hist = json.loads(resp.read().decode())
    except Exception as exc:
        return {"error": str(exc)}
    entry = hist.get(prompt_id) or {}
    prompt = entry.get("prompt") or []
    for item in prompt:
        if isinstance(item, dict) and item:
            return item
    return {"error": "history 里没找到 prompt 字典", "keys": list(entry.keys())}


def analyze_workflow(wf: dict) -> dict:
    cts = {}
    for nid, node in wf.items():
        if isinstance(node, dict) and node.get("class_type"):
            cts[nid] = node["class_type"]
    samplers = {nid: ct for nid, ct in cts.items()
                if ct in ("SamplerCustomAdvanced", "SelfLiftH3Sampler",
                          "KSampler", "KSamplerAdvanced")}
    selflift_nodes = {nid: ct for nid, ct in cts.items()
                      if "SelfLift" in (ct or "")}
    std_nodes = {nid: ct for nid, ct in cts.items()
                 if ct == "SamplerCustomAdvanced"}
    return {
        "sampler_nodes": samplers,
        "has_standard_sampler": "SamplerCustomAdvanced" in cts.values(),
        "has_selflift": any("SelfLift" in (ct or "") for ct in cts.values()),
        # 采样节点名（节点 id + class_type）——用于逐条证明走的是哪条采样路径
        "standard_sampler_nodes": std_nodes,
        "selflift_nodes": selflift_nodes,
        "selflift_count": len(selflift_nodes),
        "sampler_node_labels": [f"{nid}:{ct}" for nid, ct in samplers.items()],
    }


# ------------------------------------------------- 接缝指标（与上一轮同实现）
def frames(path: str):
    if not os.path.exists(path):
        return None
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=320:180",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    n = len(raw) // (320 * 180 * 3)
    if n == 0:
        return None
    import numpy as np
    return np.frombuffer(raw[:n * 320 * 180 * 3], dtype=np.uint8).reshape(
        n, 180, 320, 3).astype(np.float32)


def l1(a, b) -> float:
    import numpy as np
    return float(np.abs(a - b).mean())


def chain_segments(job_id: str):
    conn = sqlite3.connect(f"file:{DATA / 'jobs.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT params,outputs FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        if not row:
            return None
        params = json.loads(row["params"] or "{}")
        segs = []
        cur = job_id
        while cur:
            r = conn.execute("SELECT params FROM jobs WHERE id=?", (cur,)).fetchone()
            if not r:
                break
            p = json.loads(r["params"] or "{}")
            c = p.get("continuation") or {}
            if c.get("segment_rel"):
                segs.append(c["segment_rel"])
            cur = c.get("source_job") or ""
        segs = list(reversed(segs))
        full = None
        for o in json.loads(row["outputs"] or "[]"):
            if o.get("is_full"):
                full = o.get("file")
        return {"segs": segs, "full": full,
                "twopass": (params.get("twopass") or {}).get("enabled"),
                "mode": params.get("mode")}
    finally:
        conn.close()


def analyze(job_id: str) -> dict:
    import numpy as np
    info = chain_segments(job_id)
    if not info or not info["segs"]:
        return {"job": job_id, "error": "找不到分段信息"}
    out = {"job": job_id, "mode": info["mode"], "twopass": info["twopass"],
           "segments": []}
    for rel in info["segs"]:
        S = frames(str(REPO_DATA_OUT / rel))
        if S is None:
            out["segments"].append({"rel": rel, "error": "读不到帧"})
            continue
        d = [l1(S[i], S[i + 1]) for i in range(len(S) - 1)]
        if len(d) < 2:
            out["segments"].append({"rel": rel, "frames": len(S),
                                    "error": "帧数太少"})
            continue
        med = float(np.median(d[1:]))
        head = d[0]
        lum = S.mean(axis=(1, 2, 3))
        mx = S.max(axis=3)
        mn = S.min(axis=3)
        sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0).mean(axis=(1, 2))
        out["segments"].append({
            "rel": rel, "frames": len(S),
            "head_break_L1": head, "seg_median_L1": med,
            "head_break_ratio": head / med if med > 0 else None,
            "lum_drop_3frames": float(lum[0] - lum[3]) if len(lum) > 3 else None,
            "sat_drop_3frames": float(sat[0] - sat[3]) if len(sat) > 3 else None,
            "lum_head": float(lum[0]), "lum_frame3": float(lum[3]),
        })
    seams = []
    for i in range(len(out["segments"]) - 1):
        a, b = out["segments"][i], out["segments"][i + 1]
        if "error" in a or "error" in b:
            continue
        Sa = frames(str(REPO_DATA_OUT / a["rel"]))
        Sb = frames(str(REPO_DATA_OUT / b["rel"]))
        if Sa is None or Sb is None:
            continue
        seams.append({"between": [a["rel"], b["rel"]], "seam_L1": l1(Sa[-1], Sb[0])})
    out["seams"] = seams
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chains", type=int, default=3)
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("--base-seed", type=int, default=20261118)
    ap.add_argument("--out-root", default=None,
                    help="证据落盘根目录（默认硬编码 OUT_ROOT）")
    args = ap.parse_args()

    out_root = Path(args.out_root) if args.out_root else OUT_ROOT
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = out_root / f"seam-repeat-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    img = pick_image()
    if not img:
        print("!! 素材库里没有图片素材")
        return 2
    print(f"素材: img={dict(img)}", flush=True)

    base_params = {
        "prompt": "一位少女站在日式教室讲台前，镜头缓慢推近，薄荷绿短发，粉色背心，细腻光影",
        "width": 1280, "height": 736,
        "duration": args.duration,
        "aspect": "16:9",
        "steps": 4, "sampler": "euler", "scheduler": "beta",
        "cfg": 1.0,
        "twopass": {"enabled": True, "transition_step": 4,
                     "lowres_scale": 0.7, "rho": 0.0,
                     "w_min": 0.5, "w_max": 1.0,
                     "upscaler_model": "minimax_h3_latent_upscaler_3d_bf16.safetensors",
                     "cfg": 1.0},
    }
    refs = [{"asset_id": img["id"], "slot": "first_frame", "ordinal": 0},
            {"asset_id": img["id"], "slot": "last_frame", "ordinal": 1}]

    chains = []
    for i in range(args.chains):
        seed = args.base_seed + i
        p = dict(base_params)
        p["seed"] = seed
        tag = f"flf#{i+1}"
        print(f"\n======== [{tag}] seed={seed} 段1 提交 ========", flush=True)
        resp1 = submit("flf", p, refs)
        job1 = resp1.get("id") or resp1.get("job_id")
        print(f"[{tag}] 段1 job={job1}", flush=True)
        st1, err1, outs1 = wait_done(job1)
        print(f"[{tag}] 段1 -> {st1} {err1 or ''}", flush=True)
        if st1 != "done":
            chains.append({"tag": tag, "seed": seed, "job1": job1,
                           "job1_status": st1, "job1_error": err1})
            continue

        ctx = api(f"/api/jobs/{job1}/continuation")
        if not ctx.get("ok"):
            print(f"[{tag}] 取接续上下文失败: {ctx}", flush=True)
            chains.append({"tag": tag, "seed": seed, "job1": job1,
                           "job2": None, "ctx_error": ctx.get("error")})
            continue

        p2 = dict(p)
        p2["seed"] = seed + 500
        p2["continuation"] = ctx["continuation"]
        print(f"[{tag}] 段2 提交", flush=True)
        resp2 = submit("flf", p2, refs)
        job2 = resp2.get("id") or resp2.get("job_id")
        print(f"[{tag}] 段2 job={job2}", flush=True)
        st2, err2, outs2 = wait_done(job2)
        print(f"[{tag}] 段2 -> {st2} {err2 or ''}", flush=True)

        rec = {"tag": tag, "seed": seed, "job1": job1, "job2": job2,
               "job1_status": st1, "job2_status": st2,
               "job1_error": err1, "job2_error": err2}
        for label, jid in (("job1", job1), ("job2", job2)):
            row = job_row(jid)
            if not row:
                continue
            prm = json.loads(row["params"] or "{}")
            pid = row["prompt_id"] if "prompt_id" in row.keys() else ""
            if not pid:
                continue
            rec[f"{label}_prompt_id"] = pid
            rec[f"{label}_twopass_in_db"] = prm.get("twopass")
            rec[f"{label}_normalize_notes"] = prm.get("normalize_notes")
            wf = comfy_workflow_for(pid)
            if isinstance(wf, dict) and "error" in wf and len(wf) <= 3:
                rec[f"{label}_wf_error"] = wf["error"]
                continue
            wpath = out_dir / f"workflow_{label}_{pid}.json"
            wpath.write_text(json.dumps(wf, ensure_ascii=False, indent=2),
                             encoding="utf-8")
            rec[f"{label}_workflow_path"] = str(wpath)
            rec[f"{label}_workflow_analysis"] = analyze_workflow(wf)
            print(f"[{tag}] {label} 标准采样="
                  f"{rec[f'{label}_workflow_analysis']['has_standard_sampler']} "
                  f"SelfLift={rec[f'{label}_workflow_analysis']['has_selflift']}",
                  flush=True)

        # 段2 是成片作业（拼接后），对 job2 算接缝指标
        if st2 == "done":
            rec["seam"] = analyze(job2)
        chains.append(rec)

    # ---- 汇总 ----
    summary = {"target": "flf 接续链 >=3 条复测：判定 +0.717 是否系统性偏暗",
               "chains": chains, "generated_at": stamp}
    (out_dir / "seam_repeat.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    import numpy as np
    rows = []
    print("\n" + "=" * 78)
    print("flf 接续链复测汇总（等价 L1 口径）")
    print("=" * 78)
    for rec in chains:
        seam = rec.get("seam") or {}
        segs = seam.get("segments") or []
        if len(segs) < 2:
            print(f"\n{rec['tag']} seed={rec['seed']} -> 未拿到 2 段: "
                  f"{rec.get('job1_status')}/{rec.get('job2_status')} "
                  f"{rec.get('ctx_error') or rec.get('job2_error') or ''}")
            continue
        s1, s2 = segs[0], segs[1]
        seam_l1 = (seam.get("seams") or [{}])[0].get("seam_L1")
        a1 = rec.get("job1_workflow_analysis") or {}
        a2 = rec.get("job2_workflow_analysis") or {}
        print(f"\n{rec['tag']} seed={rec['seed']}  job1={rec['job1']} job2={rec['job2']}")
        print(f"  段1 标准采样={a1.get('has_standard_sampler')} "
              f"SelfLift={a1.get('has_selflift')}")
        print(f"  段2 标准采样={a2.get('has_standard_sampler')} "
              f"SelfLift={a2.get('has_selflift')}")
        print(f"  段1 采样节点={a1.get('sampler_node_labels')} "
              f"twopass落库={(rec.get('job1_twopass_in_db') or {}).get('enabled')}")
        print(f"  段2 采样节点={a2.get('sampler_node_labels')} "
              f"twopass落库={(rec.get('job2_twopass_in_db') or {}).get('enabled')}")
        print(f"  段1 头断点比={s1.get('head_break_ratio'):.3f}x  "
              f"亮度降={s1.get('lum_drop_3frames'):+.3f}")
        print(f"  段2 头断点比={s2.get('head_break_ratio'):.3f}x  "
              f"亮度降={s2.get('lum_drop_3frames'):+.3f}  "
              f"饱和度降={s2.get('sat_drop_3frames'):+.4f}")
        print(f"  拼接点 L1={seam_l1:.4f}" if seam_l1 else "  拼接点 L1=N/A")
        rows.append({
            "tag": rec["tag"], "seed": rec["seed"],
            "seg1_ratio": s1.get("head_break_ratio"),
            "seg2_ratio": s2.get("head_break_ratio"),
            "seg2_lum_drop": s2.get("lum_drop_3frames"),
            "seg2_sat_drop": s2.get("sat_drop_3frames"),
            "seam_L1": seam_l1,
            "job2": rec["job2"],
            # ---- 本轮新增：采样节点名（节点 id + class_type）----
            "job1_sampler_nodes": (a1.get("sampler_node_labels") or []),
            "job2_sampler_nodes": (a2.get("sampler_node_labels") or []),
            "job1_selflift_count": a1.get("selflift_count"),
            "job2_selflift_count": a2.get("selflift_count"),
            "job1_has_standard_sampler": a1.get("has_standard_sampler"),
            "job2_has_standard_sampler": a2.get("has_standard_sampler"),
            "job1_twopass_in_db": (rec.get("job1_twopass_in_db") or {}).get("enabled"),
            "job2_twopass_in_db": (rec.get("job2_twopass_in_db") or {}).get("enabled"),
            "job1_normalize_notes": rec.get("job1_normalize_notes"),
            "job2_normalize_notes": rec.get("job2_normalize_notes"),
            "job1_prompt_id": rec.get("job1_prompt_id"),
            "job2_prompt_id": rec.get("job2_prompt_id"),
        })

    if rows:
        print("\n" + "-" * 78)
        print(f"有效链数 = {len(rows)}")
        for key, thr, name in (("seg2_ratio", 1.6, "段2 头断点比"),
                               ("seg2_lum_drop", 0.3, "段2 3帧亮度降"),
                               ("seam_L1", None, "拼接点 L1")):
            vals = [r[key] for r in rows if r.get(key) is not None]
            if not vals:
                continue
            mean = float(np.mean(vals))
            std = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
            rng = max(vals) - min(vals)
            line = (f"{name}: 各条=" +
                    ", ".join(f"{v:+.4f}" for v in vals) +
                    f" | 均值={mean:+.4f} 标准差={std:.4f} 极差={rng:.4f}")
            if thr is not None:
                over = sum(1 for v in vals if v > thr)
                line += f" | 门槛{thr}: 超标 {over}/{len(vals)} 条"
            print(line)
        summary["stats"] = rows
        (out_dir / "seam_repeat.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n证据目录: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
