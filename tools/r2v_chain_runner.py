#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 8190 上跑一条 r2v 接续链（真实生成），并复算段内跳变。

【用途】
第 2 项（r2v 段内独立跳变）的取证 + 第 3 项（r2v 扩样本）的采样器。

【口径】
等价 L1（与上轮一致）：scaled RGB -> numpy mean -> 帧间 L1 均值差。

【用法】
  python3 tools/r2v_chain_runner.py --seed 20261131 --tag s1
  python3 tools/r2v_chain_runner.py --json-out /tmp/r2v_chains.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8190"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
COMFY_OUTPUT = "/mnt/usb2t_toshiba/comfyui/output/h3studio"

# 上轮 r2v 链用的素材（受控）：参考图 + 参考视频
R2V_ASSET_REFS = [
    {"asset_id": "asset_afb7438fdadf", "slot": "ref_image", "ordinal": 0},
    {"asset_id": "asset_486ce2e0f061", "slot": "ref_video", "ordinal": 0},
]


def _http(url: str, data: bytes | None = None, ctype: str | None = None,
          method: str = "GET", timeout: int = 60):
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode()) if body else {}


def submit(mode: str, params: dict, asset_refs: list) -> str:
    """POST /api/jobs（multipart）-> 返回 job id。"""
    boundary = "----h3runner" + str(int(time.time() * 1000))
    chunks: list[bytes] = []

    def field(name: str, value: str):
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode())

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    out = _http(f"{BASE}/api/jobs", data=body,
                ctype=f"multipart/form-data; boundary={boundary}",
                method="POST", timeout=120)
    if out.get("error"):
        raise SystemExit(f"提交失败: {out}")
    jid = out.get("id") or out.get("job_id")
    if not jid:
        raise SystemExit(f"提交返回没有 id: {out}")
    return jid


def wait_job(jid: str, timeout: int = 1800) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = _http(f"{BASE}/api/jobs?limit=200")
        except Exception:
            time.sleep(5)
            continue
        for j in (data.get("items") or data.get("jobs") or []):
            if j.get("id") == jid:
                if j.get("status") in ("done", "failed", "error", "cancelled"):
                    return j
        time.sleep(6)
    raise SystemExit(f"任务 {jid} 超时")


def db_params(jid: str) -> dict:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("SELECT params FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    return json.loads(row[0]) if row else {}


def outputs_of(jid: str) -> list:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute("SELECT outputs FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    return json.loads(row[0] or "[]") if row else []


def comfy_sampler_nodes(jid: str) -> dict:
    """从 ComfyUI history 里找出该 job 的采样节点名 + 是否有 SelfLift。

    【为什么必须查 history】判定「段2 走的是哪条采样路径」不能靠猜：
    落库的 params 只说明请求参数，真正的执行图在 ComfyUI 侧。
    """
    hist = _http("http://127.0.0.1:8188/history", timeout=60)
    entry = None
    for pid, rec in (hist or {}).items():
        # prompt_id 通常带 job id 前缀或 prompt_id 字段能反查
        if jid in str(pid) or jid in str(rec.get("prompt", [])[1:3] if len(rec.get("prompt", [])) > 2 else ""):
            entry = rec
            break
    if entry is None:
        # 退一步：按 outputs 里的文件名反查
        # ComfyUI 的 prompt_id 是 UUID，但输出文件名里带 h3studio 的 job id
        # （如 r2v_2a981380c6bf_00001_.mp4），所以按 job id 匹配文件名。
        for pid, rec in (hist or {}).items():
            for out in (rec.get("outputs") or {}).values():
                if not isinstance(out, dict):
                    continue
                for key, val in out.items():
                    if not isinstance(val, list):
                        continue
                    for f in val:
                        if not isinstance(f, dict):
                            continue
                        blob = f"{f.get('filename','')} {f.get('subfolder','')} {f.get('type','')}"
                        if jid in blob:
                            entry = rec
                            break
    if entry is None:
        return {"found": False, "prompt_id": None, "sampler_nodes": [],
                "has_selflift": None, "total_nodes": 0}

    nodes = entry.get("prompt")[2] if isinstance(entry.get("prompt"), list) and len(entry["prompt"]) > 2 else {}
    sampler_nodes = []
    for nid, n in (nodes or {}).items():
        ct = str(n.get("class_type", ""))
        if "Sampler" in ct or "sampler" in ct.lower():
            sampler_nodes.append({"id": nid, "class_type": ct,
                                  "title": (n.get("_meta") or {}).get("title", "")})
    has_selflift = any("SelfLift" in (n.get("_meta") or {}).get("title", "") or
                       "SelfLift" in str(n.get("class_type", ""))
                       for n in (nodes or {}).values())
    return {"found": True, "prompt_id": entry.get("prompt", [None, None])[1]
            if isinstance(entry.get("prompt"), list) else None,
            "sampler_nodes": sampler_nodes, "has_selflift": has_selflift,
            "total_nodes": len(nodes or {})}


def video_drop_verdict(params: dict) -> dict:
    """用**生产代码同一个函数**复算接续段的参考视频处置（video_drop）。

    【为什么不复刻逻辑】复刻 = 两份实现必然漂移。
    直接调 `h3studio.h3_video_modes._continuation_media_slice`，
    判据与线上**逐字节一致**。

    【为什么需要这个字段】`video_drop` 是**工作流构建期**的判定，
    不落库（落库的 `ref_videos` 是**请求参数**，工作流层会把它清空）。
    要证明「段2 确实没带参考视频」，只能靠：
      1. 落库的 ref_video_frame_count / chain_total_frames（可观测）
      2. 用生产函数复算判定（与线上同一套逻辑）
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from h3studio import h3_video_modes as hvm
        importlib.reload(hvm)
        info = hvm.cont_service.chain_info(params)
        if not info:
            return {"applicable": False, "reason": "no continuation info"}
        slice_info = hvm._continuation_media_slice(params)
        return {
            "applicable": True,
            "video_skip_frames": slice_info.get("video_skip_frames"),
            "video_drop": bool(slice_info.get("video_drop")),
            "chain_total_frames": int(info.get("chain_total_frames") or 0),
            "ref_video_frame_count": int(info.get("ref_video_frame_count") or 0),
            "audio_skip_frames": slice_info.get("skip_frames"),
            "audio_active": slice_info.get("active"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"applicable": False, "error": str(exc)}


def continuation_fields(jid: str) -> dict:
    """取落库 params 里的接续相关字段（修复前/后对照的关键证据）。"""
    p = db_params(jid)
    cont = p.get("continuation")
    if isinstance(cont, str):
        try:
            cont = json.loads(cont)
        except Exception:
            cont = {}
    if not isinstance(cont, dict):
        cont = {}
    return {
        "continuation_present": bool(cont),
        "ref_video_frame_count": cont.get("ref_video_frame_count"),
        "chain_total_frames": cont.get("chain_total_frames"),
        "video_drop": cont.get("video_drop"),
        "video_drop_recomputed": video_drop_verdict(p),
        "segment_frames": cont.get("segment_frames"),
        "stage_index": cont.get("stage_index"),
        "chain_id": cont.get("chain_id"),
        "source_job": cont.get("source_job"),
        "ref_videos_in_params": p.get("ref_videos"),
        "audio_track_source": p.get("audio_track_source"),
        "twopass": p.get("twopass"),
    }


def locate_segment_file(jid: str, outs: list) -> str:
    """分段视频落在 ComfyUI 的 output 目录（名字带 job id）。"""
    for o in outs:
        name = str(o.get("file") or o.get("name") or "")
        if name.startswith("videos/"):
            continue
        cand = os.path.join(COMFY_OUTPUT, os.path.basename(name))
        if os.path.exists(cand):
            return cand
    import glob
    hits = glob.glob(os.path.join(COMFY_OUTPUT, f"r2v_{jid}_*.mp4"))
    return hits[0] if hits else ""


def seam(path: str, scale_width: int = 1280) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,nb_frames,duration",
         "-of", "json", path], capture_output=True, text=True)
    info = json.loads(out.stdout)["streams"][0]
    w, h = int(info["width"]), int(info["height"])
    if w != scale_width:
        h = round(h * scale_width / w)
        w = scale_width
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}",
         "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    import numpy as np
    fb = w * h * 3
    n = len(raw) // fb
    arr = np.frombuffer(raw[:n * fb], dtype=np.uint8).reshape(n, h, w, 3)
    means = arr.reshape(n, -1).mean(axis=1)
    l1 = np.abs(arr[1:].astype(np.int16) - arr[:-1].astype(np.int16)).reshape(n - 1, -1).mean(axis=1)
    med = float(sorted(l1)[len(l1) // 2]) if len(l1) else 0.0
    top = sorted(range(len(l1)), key=lambda i: -float(l1[i]))[:3]
    return {
        "path": path, "frames": n, "width": w, "height": h,
        "duration": float(info.get("duration") or 0.0),
        "median_l1": round(med, 4),
        "max_l1": round(float(max(l1)), 4) if len(l1) else 0.0,
        "max_l1_at_frame": (top[0] + 1) if top else 0,
        "max_l1_ratio": round(float(max(l1)) / med, 2) if med else 0.0,
        "top3": [{"at_frame": i + 1, "l1": round(float(l1[i]), 4),
                  "lum_from": round(float(means[i]), 2),
                  "lum_to": round(float(means[i + 1]), 2),
                  "d_lum": round(float(means[i + 1] - means[i]), 2)}
                 for i in top],
        "means_head": [round(float(x), 2) for x in means[:12]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--tag", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--prompt", default="教室里的日常，少女走上讲台")
    ap.add_argument("--control", action="store_true",
                    help="额外跑一条**非接续**的段2 对照（同 seed 同素材，"
                         "只是不带 continuation）—— 用来隔离「接续」这个变量")
    args = ap.parse_args()

    base_params = {
        "prompt": args.prompt,
        "original_prompt": args.prompt,
        "model_key": "hybrid_fl2va_ref2va",
        "width": 1280, "height": 736,
        "resolution": "736p", "aspect": "16:9",
        "duration": 5, "steps": 4,
        "sampler": "euler", "scheduler": "beta",
        "shift_video": 12.0, "shift_audio": 12.0,
        "seed": args.seed,
        "audio_track_source": "generated",
        "twopass": {"enabled": False},
        "accel_lora": "", "accel_lora_strength": 1.0,
        "accel_loras": [], "effect_loras": [],
    }

    print(f"=== 段1 (r2v, seed={args.seed}) ===")
    j1 = submit("r2v", dict(base_params), R2V_ASSET_REFS)
    print("  job:", j1)
    r1 = wait_job(j1)
    print("  status:", r1.get("status"), "error:", (r1.get("error") or "")[:200])
    if r1.get("status") != "done":
        return 1
    # 立刻抓：ComfyUI history 会被重启清空，不能攒到最后统一查
    c1 = comfy_sampler_nodes(j1)

    # 取接续上下文（与服务端同一口径）
    ctx = _http(f"{BASE}/api/jobs/{j1}/continuation?duration=5")
    if not ctx.get("ok"):
        print("  取接续上下文失败:", ctx)
        return 1
    cont = ctx.get("continuation") or {}
    print("  接续上下文 chain_id:", cont.get("chain_id"),
          "stage:", cont.get("stage_index"))

    seg2_params = dict(base_params)
    seg2_params["prompt"] = args.prompt + "（接续段）"
    seg2_params["original_prompt"] = seg2_params["prompt"]
    seg2_params["seed"] = args.seed + 1
    seg2_params["continuation"] = cont

    print(f"=== 段2 (r2v 接续, seed={args.seed + 1}) ===")
    j2 = submit("r2v", seg2_params, R2V_ASSET_REFS)
    print("  job:", j2)
    r2 = wait_job(j2)
    print("  status:", r2.get("status"), "error:", (r2.get("error") or "")[:300])
    if r2.get("status") != "done":
        return 1
    c2 = comfy_sampler_nodes(j2)

    j2c = ""
    cc = None
    if args.control:
        print(f"=== 对照段2' (r2v 非接续, seed={args.seed + 1}) ===")
        ctrl_params = dict(base_params)
        ctrl_params["prompt"] = args.prompt + "（接续段）"
        ctrl_params["original_prompt"] = ctrl_params["prompt"]
        ctrl_params["seed"] = args.seed + 1
        j2c = submit("r2v", ctrl_params, R2V_ASSET_REFS)
        print("  job:", j2c)
        rc = wait_job(j2c)
        print("  status:", rc.get("status"), "error:", (rc.get("error") or "")[:300])
        cc = comfy_sampler_nodes(j2c)

    outs1 = outputs_of(j1)
    outs2 = outputs_of(j2)
    f1 = locate_segment_file(j1, outs1)
    f2 = locate_segment_file(j2, outs2)
    print("  段1 分段文件:", f1)
    print("  段2 分段文件:", f2)
    if not f2:
        return 1

    s1 = seam(f1)
    s2 = seam(f2)
    print(f"\n  段1: 帧数={s1['frames']} 中位L1={s1['median_l1']} 最大L1={s1['max_l1']}"
          f" @{s1['max_l1_at_frame']} 比值={s1['max_l1_ratio']}x")
    print(f"  段2: 帧数={s2['frames']} 中位L1={s2['median_l1']} 最大L1={s2['max_l1']}"
          f" @{s2['max_l1_at_frame']} 比值={s2['max_l1_ratio']}x")
    print(f"  段2 前 12 帧亮度: {s2['means_head']}")

    s2c = None
    if j2c:
        fc = locate_segment_file(j2c, outputs_of(j2c))
        if fc:
            s2c = seam(fc)
            print(f"\n  对照段2': 帧数={s2c['frames']} 中位L1={s2c['median_l1']} "
                  f"最大L1={s2c['max_l1']} @{s2c['max_l1_at_frame']} "
                  f"比值={s2c['max_l1_ratio']}x")

    rec = {
        "tag": args.tag, "seed": args.seed,
        "seg1_job": j1, "seg2_job": j2, "ctrl_job": j2c,
        "seg1": s1, "seg2": s2, "ctrl": s2c,
        "seg1_fields": continuation_fields(j1),
        "seg2_fields": continuation_fields(j2),
        "ctrl_fields": continuation_fields(j2c) if j2c else None,
        "seg2_comfy": c2,
        "seg1_comfy": c1,
        "ctrl_comfy": cc if j2c else None,
        "seg2_params": {k: v for k, v in db_params(j2).items()
                        if k in ("twopass", "continuation", "width", "height",
                                 "duration", "steps", "model_key", "seed",
                                 "audio_track_source", "ref_videos")},
    }
    # 醒目打印关键证据
    print(f"\n  ── 证据 ──")
    print(f"  段2 ComfyUI 采样节点: "
          f"{[s['class_type'] for s in rec['seg2_comfy']['sampler_nodes']]} "
          f"SelfLift={rec['seg2_comfy']['has_selflift']}")
    print(f"  段2 落库: ref_video_frame_count={rec['seg2_fields']['ref_video_frame_count']} "
          f"chain_total_frames={rec['seg2_fields']['chain_total_frames']} "
          f"video_drop={rec['seg2_fields']['video_drop']} "
          f"stage_index={rec['seg2_fields']['stage_index']}")
    print(f"  段1 落库: ref_video_frame_count={rec['seg1_fields']['ref_video_frame_count']} "
          f"chain_total_frames={rec['seg1_fields']['chain_total_frames']} "
          f"video_drop={rec['seg1_fields']['video_drop']}")
    if args.json_out:
        rows = []
        if os.path.exists(args.json_out):
            rows = json.load(open(args.json_out))
        rows.append(rec)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}（累计 {len(rows)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
