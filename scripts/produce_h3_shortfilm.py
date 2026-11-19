#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H3-Studio 端到端成片生产：批量提交 r2v 片段 + 长片合成。

【做什么】
  1. 用**自产参考图**（文生图产物）+ **自产音乐**（YuE2 产物），
     按分镜提交 N 个 r2v 片段（480P，H3 真实出片）。
  2. 等所有片段 done -> 调 POST /api/longfilms 合成长片。
  3. 把成片与全部证据落到 evidence 目录。

【为什么用 r2v 而不是 t2v】任务要求「参考图必须是系统里制作/编辑出来的」，
r2v 走的是真实参考图通道（实测 ComfyUI 工作流里出现 LoadImage 节点
指向自产参考图），证据链更硬。

【音轨】每个片段用 audio_track_source=reference —— 用自产 YuE2 音乐
当成片音轨（外贴，不参与采样），长片合成阶段再统一铺一条完整音轨。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8190"
#: 证据目录。
#:
#: 【2026-11-15 改 —— 可移植】原来写死成
#:   /root/projects/h3studio-refactor/evidence/h3-refactor-20261115
#: —— 那是**另一个 worktree** 的路径。本脚本合入 local/maintainer 后，
#: 那个目录在新环境下可能不存在（或者存在但属于别人的产物）。
#:
#: 现在：默认落在**本仓库自己的** evidence 目录，
#: 并且可以用环境变量 H3_SHORTFILM_EV 覆盖。
EV = Path(os.environ.get("H3_SHORTFILM_EV")
          or (Path(__file__).resolve().parent.parent
              / "evidence" / "h3-shortfilm"))

#: 自产素材（由本轮 t2i / t2a 任务产出并已入库素材库）
REF_IMAGE_ASSET = "asset_21f5d051d841"     # 文生图产物（Z-Image，1360×768）
REF_AUDIO_ASSET = "asset_bfaca307e2cf"     # YuE2 产物（FLAC，149.88s）

#: 分镜 —— 每个片段一个镜头描述（用于 2 分钟成片）
#:
#: 【片段长度安排】A 段用 30 秒（已提交探针任务 6315cb9eb935），
#: 其余 18 段各 5 秒 —— 合计 30 + 18×5 = **120 秒（正好 2 分钟）**。
#: 【为什么不都做 30 秒】H3 的时间注意力开销随帧数超线性增长
#: （实测 5 秒片段 124 帧约 132 秒；30 秒片段 736 帧约 1260 秒），
#: 拆成更多短片段更省时间，且长片合成阶段会拼回 2 分钟。
SHOTS = [
    ("A. 序：辽阔的火星沙丘，远处穹顶基地与淡紫色双月天空，宇航员小小的剪影走向地平线，风卷起细沙",
     "Slow continuous cinematic shot of the Mars colony at dusk: the astronaut walks across the red dunes toward the geodesic dome, wind carries dust across the frame, the violet sky darkens, long lens, anamorphic widescreen, 35mm film grain"),
    ("B. 侧影：宇航员侧身逆光，蓝色飞行服被风扬起，头发在风中飘动，轮廓被勾亮",
     "Profile silhouette of the astronaut in a deep blue flight suit, backlit by the low sun, hair lifting in the wind, rim light outlining her shape, static camera"),
    ("C. 特写：面罩边缘的细尘与反光，眼神望向远方，浅景深",
     "Close-up on the helmet rim, dust motes catching the light, her eyes looking into the distance, shallow depth of field, slow push in"),
    ("D. 基地：巨大的半透明穹顶，内部的灯光在暮色里发亮，能看到进出的人影",
     "The vast translucent geodesic dome habitat, interior lights glowing at dusk, small silhouettes of people moving inside, wide static shot"),
    ("E. 跟拍：她走过一排能源塔，镜头从背后跟隨，塔身在风里轻晃",
     "Tracking shot from behind as she walks past a row of power pylons, the towers swaying in the wind, steady dolly"),
    ("F. 地面：脚印与沙尘被风抹平，鞋印一路向穹顶",
     "Ground level on footprints in red dust being smoothed away by the wind, the trail leading toward the dome, low angle"),
    ("G. 远景：她站在穹顶前很小，巨大的建筑压过来",
     "She stands tiny before the dome, the enormous structure looming over her, slow crane up"),
    ("H. 内部：穹顶里温暖的灯光，悬浮的全息火星地图，她抬头看",
     "Inside the dome, warm light, a floating holographic map of Mars, she looks up at it, gentle orbit"),
    ("I. 中景：她伸手触碰全息投影，指尖穿过火星的红色光点",
     "Medium shot, her hand reaching into the hologram, fingertips passing through red points of light, slow motion"),
    ("J. 窗边：透过观测窗，外面的双月挂在紫天上，玻璃上映出她的脸",
     "By the observation window, the twin moons in the violet sky outside, her face reflected in the glass, slow pan"),
    ("K. 夜景：穹顶外景，灯光变成琥珀色，风沙在灯下流淌",
     "Exterior night shot of the dome, lights turned amber, sand streaming through the light, static wide"),
    ("L. 终：她独自站在夜色沙丘上仰望星空，银河横过，剪影定格",
     "Final shot, she stands alone on the dunes at night looking up, the Milky Way across the sky, her silhouette settling into stillness"),
    ("M. 走廊：穹顶内部的金属走廊，她迎面走来，灯带在身后延伸",
     "Interior metal corridor of the dome, she walks toward camera, light strips stretching behind her, steady dolly back"),
    ("N. 舷窗：她在控制台前查看全息数据，蓝色屏光照在脸上",
     "At a console she studies holographic data, blue screen light on her face, over the shoulder shot"),
    ("O. 温室：穹顶内的绿色植物区，水珠悬在叶尖，暖光穿过叶片",
     "Inside the dome greenhouse, green foliage with water droplets, warm light through leaves, macro to medium"),
    ("P. 黄昏：穹顶外的沙暴逼近，橙红色的尘墙吞没地平线",
     "At dusk a sandstorm approaches the colony, an orange dust wall swallowing the horizon, wide static"),
    ("Q. 夜行：她在夜色沙丘上独自行走，头灯的光柱切开黑暗",
     "At night she walks alone across the dunes, the headlamp beam cutting the darkness, long lens, follow shot"),
    ("R. 星空：巨大的穹顶在银河下亮着，灯光微弱，人影在窗后",
     "The huge dome glows faintly under the Milky Way, a tiny figure behind the window, static wide, long exposure"),
    ("S. 收束：她站在穹顶前抬头，掌心贴在穹顶上，星图在玻璃上流转",
     "She stands before the dome, palm against it, star charts drifting across the glass, slow push in, anamorphic flare"),
]


def _post_multipart(fields: dict) -> dict:
    bnd = "----h3studio" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append(
            f"--{bnd}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    body = b"".join(parts) + f"--{bnd}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={bnd}"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def submit_shot(index: int, cn: str, en: str, duration: float) -> str:
    resp = _post_multipart({
        "mode": "r2v",
        "params": {
            "prompt": en,
            "width": 864, "height": 480, "duration": duration,
            "model_key": "ref2va_pruned_int8",
            "audio_track_source": "reference",
            "steps": 4, "sampler": "euler", "scheduler": "beta",
            "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
            "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
        },
        "asset_refs": [
            {"asset_id": REF_IMAGE_ASSET, "slot": "ref_image", "ordinal": 0},
            {"asset_id": REF_AUDIO_ASSET, "slot": "ref_audio", "ordinal": 0},
        ],
    })
    job_id = resp["id"]
    print(f"  [{index:02d}] submitted {job_id}  ({cn[:26]}...)  dur={duration}s", flush=True)
    return job_id


def wait_all(job_ids: list[str], timeout_sec: float) -> dict:
    deadline = time.time() + timeout_sec
    done: dict = {}
    while time.time() < deadline:
        pending = []
        for jid in job_ids:
            if jid in done:
                continue
            try:
                job = _get(f"/api/jobs/{jid}")
            except Exception:
                pending.append(jid)
                continue
            status = job.get("status")
            if status == "done":
                done[jid] = job
            elif status == "failed":
                done[jid] = job
                print(f"  !! {jid} FAILED: {(job.get('error') or '')[:160]}", flush=True)
            else:
                pending.append(jid)
        if not pending:
            break
        running = [j for j in pending]
        print(f"  ... {len(done)}/{len(job_ids)} done; pending={len(running)} "
              f"first={running[0] if running else '-'} "
              f"({time.strftime('%H:%M:%S')})", flush=True)
        time.sleep(20)
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=30.0, help="每片段秒数")
    ap.add_argument("--count", type=int, default=0, help="片段数（0 = 全部）")
    ap.add_argument("--submit-only", action="store_true")
    ap.add_argument("--jobs", default="", help="已有 job id（逗号分隔），跳过提交")
    ap.add_argument("--start", type=int, default=0, help="从第几个分镜开始提交")
    ap.add_argument("--duration-map", default="", help="分镜:秒 逗号分隔，如 A:30,B:5,C:5")
    args = ap.parse_args()

    EV.mkdir(parents=True, exist_ok=True)
    shots = SHOTS[args.start: args.count] if args.count else SHOTS[args.start:]
    # 分镜 -> 秒数映射（默认统一 args.duration）
    durations = {}
    for pair in (args.duration_map or "").split(","):
        if ":" in pair:
            key, val = pair.split(":", 1)
            try:
                durations[key.strip()] = float(val)
            except ValueError:
                pass
    job_ids: list[str] = []
    if args.jobs:
        job_ids = [x.strip() for x in args.jobs.split(",") if x.strip()]
        print(f"[mode] reuse {len(job_ids)} existing jobs")
    else:
        print(f"[submit] {len(shots)} shots")
        for idx, (cn, en) in enumerate(shots, args.start + 1):
            letter = chr(ord("A") + idx - 1)
            dur = durations.get(letter, args.duration)
            job_ids.append(submit_shot(idx, cn, en, dur))
        (EV / "clip_job_ids.json").write_text(
            json.dumps(job_ids, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.submit_only:
            print("submitted, exiting (--submit-only)")
            return 0

    print(f"[wait] {len(job_ids)} jobs")
    done = wait_all(job_ids, timeout_sec=7200)
    summary = []
    clips = []
    for jid in job_ids:
        job = done.get(jid) or _get(f"/api/jobs/{jid}")
        outs = job.get("outputs") or []
        summary.append({"job_id": jid, "mode": job.get("mode"),
                        "status": job.get("status"),
                        "error": (job.get("error") or "")[:200],
                        "outputs": outs,
                        "audio_track_source": (job.get("params") or {}).get("audio_track_source")})
        if job.get("status") == "done" and outs:
            clips.append({"job_id": jid, "file": outs[0]["file"],
                          "name": outs[0].get("name", ""),
                          "width": outs[0].get("width"), "height": outs[0].get("height")})
    (EV / "clip_jobs_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] {len(clips)}/{len(job_ids)} clips ready")
    for c in clips:
        print("  ", c["job_id"], c["file"], f"{c['width']}x{c['height']}")
    if clips:
        (EV / "longfilm_payload.json").write_text(
            json.dumps({"title": "火星基地 · 两分钟", "intro": "H3-Studio 端到端实测成片",
                        "clips": clips}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[longfilm payload written]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
