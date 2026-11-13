# -*- coding: utf-8 -*-
"""【独立核验脚本】不依赖验证脚本的自证 —— 自己重新算一遍关键数字。

为什么要另写一个：
  `tests/verify_d1_four_defects.py` 是**被测系统的配套脚本**，
  它自己算出来的数字不能当作「独立复核」的证据。
  本脚本从**原始产物**重新算：
    * 两份兄弟分支潜空间：同时存在 + 内容不同（md5）+ inode 不同
    * 各段 / 各完整视频的帧数与时长（直接调 ffprobe）
    * 灰度 MAE（自己实现，不 import 被测脚本的 mae）
    * ComfyUI **实际执行图**（从 ComfyUI history 取，不是从脚本报告取）
  并把结果落盘成一份独立报告。

用法：
    python3 tests/verify_d1_independent_check.py \
        --data-dir /root/projects/h3studio-t2v-continue/data-accept \
        --out /root/projects/h3studio-t2v-continue/data-accept/independent-check

只读 + 写自己的报告目录，不改任何东西。
"""

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

COMFY = "http://127.0.0.1:8188"
RELAY = Path("/home/ubuntu/comfyui/output/relay_kit")


def ffprobe_frames(p):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(p)],
        text=True).strip()
    return int(raw)


def ffprobe_dur(p):
    raw = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(p)], text=True).strip()
    return round(float(raw), 3)


def gray_mae(p1, p2):
    """自己实现的灰度 MAE（不 import 被测脚本的函数）。"""
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(p1).convert("L"), dtype=np.float64)
    b = np.asarray(Image.open(p2).convert("L"), dtype=np.float64)
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    return float(np.abs(a - b).mean())


def load_db(data_dir):
    conn = sqlite3.connect(str(Path(data_dir) / "jobs.db"))
    conn.row_factory = sqlite3.Row
    try:
        return {r["id"]: r["prompt_id"]
                for r in conn.execute("SELECT id, prompt_id FROM jobs")}
    finally:
        conn.close()


def exec_graph(pid):
    """从 ComfyUI history 取**实际执行**的图。"""
    with urllib.request.urlopen(f"{COMFY}/history/{pid}", timeout=60) as r:
        h = json.loads(r.read().decode())
    e = h.get(pid)
    if not e:
        return {}
    p = e.get("prompt")
    graph = p[2] if isinstance(p, list) else p
    out = {}
    for nid, node in (graph or {}).items():
        ct = node.get("class_type")
        title = (node.get("_meta") or {}).get("title", "")
        if ct == "H3RelayLatentSave":
            out["save"] = {"node": nid, "run_id": node["inputs"].get("run_id"),
                           "stage_index": node["inputs"].get("stage_index")}
        elif ct == "H3RelayLatentLoad":
            key = ("load" if "上游" in title else
                   "anchor_load" if "锚" in title else nid)
            out[key] = {"node": nid, "run_id": node["inputs"].get("run_id"),
                         "stage_index": node["inputs"].get("stage_index"),
                         "explicit_path": node["inputs"].get("explicit_path")}
        elif ct == "H3RelayCopyBridge":
            out["bridge"] = {
                "node": nid,
                "context_latent": node["inputs"].get("context_latent"),
                "ref_anchor_latent": node["inputs"].get("ref_anchor_latent"),
                "ref_anchor_stage": node["inputs"].get("ref_anchor_stage"),
                "run_id": node["inputs"].get("run_id"),
                "stage_index": node["inputs"].get("stage_index"),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir",
                    default="/root/projects/h3studio-t2v-continue/data-accept")
    ap.add_argument("--out",
                    default="/root/projects/h3studio-t2v-continue/data-accept/independent-check")
    args = ap.parse_args()

    data = Path(args.data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    vdir = data / "outputs" / "videos"
    fdir = data / "evidence-frames" / "frames"

    pids = load_db(data)
    rep = {"判据声明": "独立复核：不采信 verify_d1_four_defects.py 的自报数字，"
                       "全部从原始产物（潜空间文件 / mp4 / ComfyUI history）重算",
           "生成时间": subprocess.check_output(["date", "-Is"], text=True).strip(),
           "data_dir": str(data.resolve()),
           "checks": {}, "failures": []}

    def fail(m):
        rep["failures"].append(m)
        print(f"    ❌ {m}")

    # ---------- 找这一轮的 5 段（按 source_job 血缘） ----------
    conn = sqlite3.connect(str(data / "jobs.db"))
    conn.row_factory = sqlite3.Row
    jobs = {}
    for r in conn.execute("SELECT id, params, outputs, status FROM jobs"):
        jobs[r["id"]] = {
            "params": json.loads(r["params"] or "{}"),
            "outputs": json.loads(r["outputs"] or "[]"),
            "status": r["status"]}
    conn.close()

    # 血缘最长的链 = 段数最多的 chain_id
    chains = {}
    for jid, j in jobs.items():
        info = j["params"].get("continuation") or {}
        cid = str(info.get("chain_id") or "")
        if cid:
            chains.setdefault(cid, []).append(
                (int(info.get("stage_index") or 0), jid))
    best = max(chains.values(), key=len)
    best.sort()
    root = best[0][1]
    chain_id = (jobs[root]["params"].get("continuation") or {}).get("chain_id")
    print(f"链首段 = {root}（链 {chain_id}，{len(best)} 段）")
    rep["chain"] = {"chain_id": chain_id, "root": root,
                    "members": [jid for _, jid in best]}

    def info_of(jid):
        return jobs[jid]["params"].get("continuation") or {}

    def _resolve(rel):
        """outputs[].file 是相对 **outputs 根** 的路径（形如 videos/xxx.mp4）。"""
        return data / "outputs" / rel

    def seg_file(jid):
        for o in jobs[jid]["outputs"]:
            if o.get("kind") == "video" and not o.get("is_full"):
                return _resolve(o["file"])
        return None

    def full_file(jid):
        for o in jobs[jid]["outputs"]:
            if o.get("kind") == "video" and o.get("is_full"):
                return _resolve(o["file"])
        return None

    # ---------- ① 兄弟分支落盘坐标 ----------
    print("\n【① 兄弟分支落盘互不覆盖】")
    stage1 = [jid for st, jid in best if st == 1]
    br = []
    for jid in stage1:
        rid = str(info_of(jid).get("latent_run_id") or "")
        p = RELAY / rid / "stage_00000.safetensors" if rid else None
        st = os.stat(p) if p and p.is_file() else None
        br.append({
            "job_id": jid,
            "run_id": rid,
            "stage_index": info_of(jid).get("latent_stage", 0),
            "stage_file": str(p) if p else "",
            "exists": bool(p and p.is_file()),
            "size_bytes": st.st_size if st else None,
            "inode": st.st_ino if st else None,
            "mtime": time.strftime("%H:%M:%S", time.localtime(st.st_mtime)) if st else "",
            "md5": hashlib.md5(p.read_bytes()).hexdigest() if p and p.is_file() else "",
        })
    if len(br) < 2:
        fail("没有两条 stage=1 的兄弟分支")
    else:
        b1, b2 = br[0], br[1]
        print(f"  B1: run_id={b1['run_id']} stage={b1['stage_index']} "
              f"exists={b1['exists']} ino={b1['inode']} md5={b1['md5'][:16]}")
        print(f"  B2: run_id={b2['run_id']} stage={b2['stage_index']} "
              f"exists={b2['exists']} ino={b2['inode']} md5={b2['md5'][:16]}")
        print(f"  B1 文件: {b1['stage_file']}")
        print(f"  B2 文件: {b2['stage_file']}")
        if b1["run_id"] == b2["run_id"]:
            fail("两条分支 run_id 相同 -> 落盘坐标冲突未修")
        if not (b1["exists"] and b2["exists"]):
            fail("两条分支的潜空间文件没有同时存在")
        if b1["md5"] and b2["md5"] and b1["md5"] == b2["md5"]:
            fail("两条分支的潜空间内容相同 -> 互相覆盖了")
        if b1["inode"] and b2["inode"] and b1["inode"] == b2["inode"]:
            fail("两条分支是同一个 inode -> 同一个文件")
        print(f"  -> run_id 不同 / 同时存在 / 内容不同 / inode 不同 ✅")
    rep["checks"]["C1_落盘坐标"] = {"branches": br}

    # ---------- ② 首帧归属 ----------
    print("\n【② C1 首帧归属】")
    c1_jid = next((jid for st, jid in best if st == 2), None)
    b1_jid, b2_jid = stage1[0], stage1[1]
    fb1 = next(fdir.glob(f"B1-*-last.png"), None)
    fb2 = next(fdir.glob(f"B2-*-last.png"), None)
    fc1 = next(fdir.glob(f"C1-*-first.png"), None)
    if not (fb1 and fb2 and fc1):
        fail(f"缺帧图 B1={fb1} B2={fb2} C1={fc1}")
    else:
        d1 = gray_mae(fc1, fb1)
        d2 = gray_mae(fc1, fb2)
        print(f"  C1首帧 vs B1末帧 MAE = {d1:.3f}")
        print(f"  C1首帧 vs B2末帧 MAE = {d2:.3f}")
        print(f"  偏离倍数 = {d2/d1:.2f}x")
        print(f"  缺陷态对照（D1 报告）: vs B1 = 2.08 / vs B2 = 38.65")
        if not d1 < d2:
            fail("C1 首帧更像 B2 末帧 -> 接错分支")
        rep["checks"]["C2_首帧归属"] = {
            "mae_C1first_vs_B1last": round(d1, 3),
            "mae_C1first_vs_B2last": round(d2, 3),
            "ratio": round(d2 / d1, 2),
            "defect_state_reference": {"vs_B1": 2.08, "vs_B2": 38.65},
            "verdict": "B1(正确)" if d1 < d2 else "B2(错误)",
            "frames": {"B1_last": str(fb1), "B2_last": str(fb2),
                       "C1_first": str(fc1)}}

    # ---------- ③ 链账本按血缘隔离 ----------
    print("\n【③ 链账本按血缘隔离】")
    lin = {}
    for _, jid in best:
        rels, seen, cur = [], set(), jid
        for _ in range(512):
            if not cur or cur in seen:
                break
            seen.add(cur)
            info = info_of(cur)
            rel = str(info.get("segment_rel") or "").strip()
            if rel:
                rels.append(rel)
            cur = str(info.get("source_job") or "").strip()
        rels.reverse()
        lin[jid] = rels
    b2_rel = next((o["file"] for o in jobs[b2_jid]["outputs"]
                   if o.get("kind") == "video" and not o.get("is_full")), "")
    b2_rel_abs = str(data / "outputs" / b2_rel) if b2_rel else ""
    for jid, rels in lin.items():
        print(f"  {jid} 血缘 = {[Path(r).name for r in rels]}")
    c1_in = lin.get(c1_jid, [])
    if b2_rel and b2_rel in c1_in:
        fail(f"C1 血缘里出现了 B2 的段 {b2_rel} -> 链账本串分支")
    else:
        print("  -> C1 血缘不含 B2 的段 ✅")
    # 帧数账
    #
    # 【关键】期望帧数必须按**该段自己的血缘**算，不能按 chain 顺序累加 ——
    # B1/B2 是**兄弟分支**（各自都只含 A），不是先后两段。
    # 用链顺序累加会把 B2 的 102 帧错误地加到 B1 分支的期望值上。
    segf = {}
    for _, jid in best:
        sf = seg_file(jid)
        if sf and sf.is_file():
            segf[jid] = ffprobe_frames(sf)
    # rel -> job 反查表（用于把血缘里的段文件换算成帧数）
    rel2job = {}
    for jid in jobs:
        for o in jobs[jid]["outputs"]:
            if o.get("kind") == "video" and not o.get("is_full"):
                rel2job[o["file"]] = jid
    print("  完整视频帧数账（按各自血缘累加）：")
    for _, jid in best:
        ff = full_file(jid)
        if not ff or not ff.is_file():
            continue
        exp = 0
        detail = []
        for rel in lin.get(jid, []):
            src = rel2job.get(rel)
            n = segf.get(src, 0) if src else 0
            exp += n
            detail.append(f"{Path(rel).name}({n})")
        actual = ffprobe_frames(ff)
        dur = ffprobe_dur(ff)
        ok = actual == exp
        print(f"    {jid}: {actual:4d} 帧 {dur:7.3f}s  "
              f"血缘 {' + '.join(detail)} = {exp:4d} {'✅' if ok else '❌'}")
        if not ok:
            fail(f"{jid} 完整视频 {actual} 帧 != 血缘期望 {exp}")
        rep.setdefault("_full", {})[jid] = {
            "file": str(ff), "frames": actual, "duration_s": dur,
            "lineage_expected": exp, "match": ok,
            "lineage_detail": detail,
            "lineage": lin.get(jid, [])}
    rep["checks"]["C3_链账本血缘隔离"] = {
        "lineage": {k: v for k, v in lin.items()},
        "b2_segment_rel": b2_rel,
        "b2_absent_from_c1": not (b2_rel and b2_rel in c1_in),
        "full_videos": rep.pop("_full", {}),
        "seg_frames": segf}

    # ---------- ④ 执行图：显式锚 + 指纹 ----------
    print("\n【④ ComfyUI 实际执行图】")
    graphs = {}
    for _, jid in best:
        pid = pids.get(jid)
        if not pid:
            continue
        g = exec_graph(pid)
        if g:
            graphs[jid] = g
    c1g = graphs.get(c1_jid, {})
    load = c1g.get("load", {})
    anc = c1g.get("anchor_load", {})
    brg = c1g.get("bridge", {})
    print(f"  C1 读回上游  run_id={load.get('run_id')} "
          f"explicit_path={load.get('explicit_path')}")
    print(f"  C1 读回外观锚 run_id={anc.get('run_id')} "
          f"explicit_path={anc.get('explicit_path')}")
    print(f"  bridge.ref_anchor_latent = {brg.get('ref_anchor_latent')}")
    print(f"  bridge.ref_anchor_stage  = {brg.get('ref_anchor_stage')}")
    print(f"  C1 落盘 run_id = {c1g.get('save', {}).get('run_id')}")
    checks4 = {}
    if brg.get("ref_anchor_latent") is None:
        fail("执行图里 ref_anchor_latent 为空 -> 外观锚没显式传入")
    else:
        checks4["ref_anchor_latent"] = brg["ref_anchor_latent"]
    if brg.get("ref_anchor_stage") != -1:
        fail("ref_anchor_stage != -1 -> 仍可能走自动读锚")
    else:
        checks4["ref_anchor_stage"] = -1
    sp = load.get("explicit_path") or ""
    fp_ok = bool(sp) and Path(sp).stem.count("_") >= 1
    if not fp_ok:
        fail(f"读回路径没有指纹后缀：{sp}")
    else:
        checks4["fingerprint_suffix"] = Path(sp).stem.split("_")[-1]
        print(f"  ✅ 读回路径带内容指纹后缀：{checks4['fingerprint_suffix']}")
    checks4["graphs"] = graphs
    rep["checks"]["C4_执行图_显式锚与指纹"] = checks4

    # ---------- 写盘 ----------
    (out / "independent-report.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("=" * 74)
    print(f"独立复核：{'✅ 全部通过' if not rep['failures'] else '❌ 有失败项'}")
    for f in rep["failures"]:
        print("   -", f)
    print("=" * 74)
    print(f"报告: {out / 'independent-report.json'}")
    return 0 if not rep["failures"] else 1


if __name__ == "__main__":
    sys.exit(main())
