#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 4 项：参考音频续接时序的**在线**复核（8190 真实生成）。

【要验证什么】
按 docs/接续模式深度研究-20261116.md §5.4：

  「被接续段为首尾帧/参考生视频且音轨来源为参考音频」时：
    (a) 次段填入**原始参考图**（素材回填）
    (b) 次段填入「**已移除前段已用音频**的后续音频」= 原始音频[C_n/24, ...)
        并**前置 1 秒空白**
    (c) 最终合并以**原始参考音频**为音轨，长度 = 合并后长度
    (d) 若音轨取参考视频音轨，则音视频**一起切**

【怎么验】
  1. 段1：r2v + 参考音频素材 + audio_track_source=reference
  2. 段2：接续段1 —— 检查落库 params 里的音频时序字段
     （audio_segment_start / _duration / _skip_frames / audio_consumed_frames）
  3. 检查 ComfyUI input 里的段音频文件 = 「1s 空白 ⊕ 切片」
     —— 用 ffmpeg 实测：前 1 秒必须是静音，1 秒之后要有声
  4. 检查成片的音轨 = 原始参考音频（长度 = 成片长度）

【用法】
  python3 tools/audio_continuation_probe.py --audio-asset asset_7b143a08a57f
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8190"
DB = "/root/projects/comfyui-minimax-h3-studio/data/jobs.db"
COMFY_INPUT = "/mnt/usb2t_toshiba/comfyui/input"
COMFY_OUTPUT = "/mnt/usb2t_toshiba/comfyui/output/h3studio"

# 参考图素材（与 r2v 扩样本同一张，受控）
REF_IMAGE_ASSET = "asset_afb7438fdadf"


def http(url, data=None, ctype=None, method="GET", timeout=60):
    req = urllib.request.Request(url, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body.decode()) if body else {}


def submit(mode, params, asset_refs):
    boundary = "----h3aud" + str(int(time.time() * 1000))
    chunks = []

    def field(name, value):
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode())

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    chunks.append(f"--{boundary}--\r\n".encode())
    out = http(f"{BASE}/api/jobs", data=b"".join(chunks),
               ctype=f"multipart/form-data; boundary={boundary}",
               method="POST", timeout=120)
    if out.get("error"):
        raise SystemExit(f"提交失败: {out}")
    return out.get("id") or out.get("job_id")


def wait_job(jid, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = http(f"{BASE}/api/jobs?limit=200")
        except Exception:
            time.sleep(5)
            continue
        for j in (data.get("jobs") or []):
            if j.get("id") == jid and j.get("status") in (
                    "done", "failed", "error", "cancelled"):
                return j
        time.sleep(6)
    raise SystemExit(f"任务 {jid} 超时")


def db_row(jid):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT params, outputs, mode, status FROM jobs WHERE id=?",
                       (jid,)).fetchone()
    con.close()
    return dict(row) if row else {}


def audio_stat(path):
    """返回 (总时长, 前1秒RMS, 1秒后RMS, 整体RMS)。"""
    import numpy as np
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration,sample_rate,channels,codec_name",
         "-of", "json", path], capture_output=True, text=True)
    try:
        st = (json.loads(out.stdout).get("streams") or [{}])[0]
    except Exception:
        st = {}
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "44100",
         "-f", "s16le", "-"], capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if len(a) == 0:
        return {"duration": float(st.get("duration") or 0.0), "n_samples": 0,
                "rms_lead_1s": 0.0, "rms_after_1s": 0.0, "rms_all": 0.0,
                "codec": st.get("codec_name")}
    sr = 44100
    lead = a[:sr]
    rest = a[sr:]
    return {
        "duration": float(st.get("duration") or 0.0),
        "n_samples": int(len(a)),
        "rms_lead_1s": round(float(np.sqrt((lead ** 2).mean())), 6),
        "rms_after_1s": round(float(np.sqrt((rest ** 2).mean())), 6) if len(rest) else 0.0,
        "rms_all": round(float(np.sqrt((a ** 2).mean())), 6),
        "codec": st.get("codec_name"),
        "sample_rate": st.get("sample_rate"),
        "channels": st.get("channels"),
    }


def find_segment_audio(job_id):
    """段音频文件在 ComfyUI input/h3studio/segment_audio/ 下。"""
    hits = glob.glob(f"{COMFY_INPUT}/**/seg-*.m4a", recursive=True)
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio-asset", required=True)
    ap.add_argument("--seed", type=int, default=20261250)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    asset_refs = [
        {"asset_id": REF_IMAGE_ASSET, "slot": "ref_image", "ordinal": 0},
        {"asset_id": args.audio_asset, "slot": "ref_audio", "ordinal": 0},
    ]
    base = {
        "prompt": "安静的教室，少女走上讲台",
        "original_prompt": "安静的教室，少女走上讲台",
        "model_key": "hybrid_fl2va_ref2va",
        "width": 1280, "height": 736,
        "resolution": "736p", "aspect": "16:9",
        "duration": 5, "steps": 4,
        "sampler": "euler", "scheduler": "beta",
        "shift_video": 12.0, "shift_audio": 12.0,
        "seed": args.seed,
        "audio_track_source": "reference",
        "twopass": {"enabled": False},
        "accel_lora": "", "accel_lora_strength": 1.0,
        "accel_loras": [], "effect_loras": [],
    }

    print("=== 段1（r2v + 参考音频，音轨来源=reference）===")
    j1 = submit("r2v", dict(base), asset_refs)
    print("  job:", j1)
    r1 = wait_job(j1)
    print("  status:", r1.get("status"), "err:", (r1.get("error") or "")[:200])
    if r1.get("status") != "done":
        return 1
    row1 = db_row(j1)
    p1 = json.loads(row1["params"])
    c1 = p1.get("continuation") or {}
    print("  落库 ref_audios:", p1.get("ref_audios"))
    print("  落库 cont.audio_track_mode:", c1.get("audio_track_mode"))
    print("  落库 cont.audio_source_kind:", c1.get("audio_source_kind"))
    print("  落库 cont.audio_source_file:", c1.get("audio_source_file"))
    print("  落库 cont.audio_source_duration:", c1.get("audio_source_duration"))
    print("  落库 cont.audio_consumed_frames:", c1.get("audio_consumed_frames"))

    ctx = http(f"{BASE}/api/jobs/{j1}/continuation?duration=5")
    cont = ctx.get("continuation") or {}
    print("  接续上下文 stage:", cont.get("stage_index"))

    seg2 = dict(base)
    seg2["prompt"] = base["prompt"] + "（接续段）"
    seg2["original_prompt"] = seg2["prompt"]
    seg2["seed"] = args.seed + 1
    seg2["continuation"] = cont

    print("=== 段2（接续段）===")
    j2 = submit("r2v", seg2, asset_refs)
    print("  job:", j2)
    r2 = wait_job(j2)
    print("  status:", r2.get("status"), "err:", (r2.get("error") or "")[:300])
    if r2.get("status") != "done":
        return 1
    row2 = db_row(j2)
    p2 = json.loads(row2["params"])
    c2 = p2.get("continuation") or {}
    print("\n  ── (b) 音频时序字段 ──")
    for k in ("audio_track_mode", "audio_source_kind", "audio_source_file",
              "audio_source_duration", "audio_consumed_frames",
              "audio_lead_blank", "audio_segment_start",
              "audio_segment_duration", "audio_segment_skip_frames",
              "audio_sampling_duration", "audio_warnings"):
        print(f"    cont.{k} = {c2.get(k)}")
    print("    params.segment_audio_file =", p2.get("segment_audio_file"))
    print("    params.ref_audios =", p2.get("ref_audios"))

    seg_audio = find_segment_audio(j2)
    print("\n  ── (b) 段音频文件实测（1s 空白 ⊕ 切片）──")
    seg_stat = {}
    if seg_audio:
        print("    文件:", seg_audio)
        seg_stat = audio_stat(seg_audio)
        print("   ", json.dumps(seg_stat, ensure_ascii=False))
        print(f"    前 1 秒 RMS={seg_stat.get('rms_lead_1s')}（应≈0 = 空白）")
        print(f"    1 秒后 RMS={seg_stat.get('rms_after_1s')}（应>0 = 有声）")
    else:
        print("    ⚠ 没找到段音频文件")

    print("\n  ── (c) 成片音轨 ──")
    outs = json.loads(row2["outputs"] or "[]")
    full = ""
    for o in outs:
        f = str(o.get("file") or "")
        if f.startswith("videos/"):
            full = os.path.join(
                "/root/projects/comfyui-minimax-h3-studio/data/outputs", f)
            break
    full_stat = {}
    if full and os.path.exists(full):
        print("    成片:", full)
        full_stat = audio_stat(full)
        print("   ", json.dumps(full_stat, ensure_ascii=False))
    else:
        print("    ⚠ 没找到成片")

    rec = {
        "seg1_job": j1, "seg2_job": j2,
        "seg1_cont": {k: c1.get(k) for k in
                      ("audio_track_mode", "audio_source_kind",
                       "audio_source_file", "audio_source_duration",
                       "audio_consumed_frames")},
        "seg2_cont": {k: c2.get(k) for k in
                      ("audio_track_mode", "audio_source_kind",
                       "audio_source_file", "audio_source_duration",
                       "audio_consumed_frames", "audio_lead_blank",
                       "audio_segment_start", "audio_segment_duration",
                       "audio_segment_skip_frames", "audio_sampling_duration")},
        "segment_audio_file": seg_audio,
        "segment_audio_stat": seg_stat,
        "full_file": full,
        "full_audio_stat": full_stat,
    }
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
        print(f"\n[写出] {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
