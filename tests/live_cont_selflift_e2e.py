# -*- coding: utf-8 -*-
"""线上 8190 真实跑通：二采（SelfLift）接续链（段1→段2）。

【跑什么】
  1. t2v 二采：段1（二采 + 无参考媒体）-> 点「接续」-> 段2（二采 + 接上一段潜空间）
  2. r2v 二采：段1（二采 + 参考图 + 参考音乐）-> 段2

【核对什么】
  * 段1 潜空间真的落盘（relay_kit/h3studio_s<job_id>/stage_00000.safetensors）
  * 段1 的 latent 时间步 == video_latent_t(align_frame_count(时长))（网格对齐）
  * 点「接续」返回 ok=true（说明二采不再被闸门挡）
  * 段2 真的接上：段2 的读回路径指向段1 的潜空间
  * 段2 的工作流里真的有 300:load/300:bridge/300:trim/300:save
    （不是静默退化成普通生成）
  * 二采 positive 走桥第 4 路（["300:bridge", 3]）
  * ffprobe 核对段视频与完整视频的时长/帧数
  * 接缝量化：帧间差（位移）+ 锐度（清晰度台阶），对照报告 §4.3.2

【产出】
  evidence/cont-selflift-live-<时间戳>/report.json

运行：python3 tests/live_cont_selflift_e2e.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("H3STUDIO_BASE", "http://127.0.0.1:8190")
DATA = Path("/root/projects/comfyui-minimax-h3-studio/data")
OUT_ROOT = Path("/root/projects/h3studio-live/evidence")
RELAY = Path("/home/ubuntu/comfyui/output/relay_kit")
sys.path.insert(0, "/root/projects/h3studio-cont-seg")
from h3studio.continuation import (align_frame_count, video_latent_t,  # noqa: E402
                                   tail_aligned)


def api(path, method="GET", payload=None, timeout=60, base=None):
    url = f"{base or BASE}{path}"
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def ffprobe(path):
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,duration,nb_frames,width,height",
           "-of", "json", str(path)]
    raw = subprocess.check_output(cmd, encoding="utf-8", timeout=60)
    data = json.loads(raw)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    frames = video.get("nb_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    return {
        "path": str(path),
        "format_duration": float(fmt.get("duration") or 0.0),
        "video_duration": float(video.get("duration") or 0.0),
        "width": video.get("width"),
        "height": video.get("height"),
        "frames": frames,
        "has_audio": audio is not None,
        "audio_duration": (float(audio.get("duration") or 0.0)
                           if audio else None),
    }


def latent_shape(path):
    """读 safetensors 头（不加载权重）拿 streams.video 的 shape。"""
    import struct
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        hdr = json.loads(f.read(n))
    for key, val in hdr.items():
        if key.endswith("streams.video"):
            return val.get("shape")
    return None


def job_row(job_id):
    conn = sqlite3.connect(DATA / "jobs.db")
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def job_params(job_id):
    row = job_row(job_id)
    return json.loads(row["params"] or "{}") if row else {}


def wait_done(job_id, timeout=2400):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        row = job_row(job_id)
        if row:
            status = row["status"]
            if status in ("done", "failed", "canceled"):
                return status, row["error"], json.loads(row["outputs"] or "[]")
            last = f"{status} {row.get('progress_text') or ''}"
        time.sleep(5)
    return "timeout", last, []


def pick_assets():
    conn = sqlite3.connect("/mnt/usb2t/asset_library/asset_library.db")
    conn.row_factory = sqlite3.Row
    try:
        image = conn.execute(
            "SELECT id, name FROM assets WHERE category='image' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
        audio = conn.execute(
            "SELECT id, name FROM assets WHERE category='audio' "
            "AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return (dict(image) if image else None), (dict(audio) if audio else None)


def submit(mode, params, asset_refs):
    boundary = "----h3studioSelflift" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:800]
        raise RuntimeError(f"提交失败 HTTP {exc.code}: {detail}") from exc
    if not data.get("id"):
        raise RuntimeError(f"提交失败: {data}")
    return data["id"]


def seam_metrics(seg_path):
    """接缝量化（报告 §4.3.2）：
       * 位移指标：接缝前后帧间差 vs 段内平均帧间差
       * 模糊指标：钉住区锐度（拉普拉斯方差）vs 新生成区锐度
    """
    import numpy as np
    cmd = ["ffmpeg", "-v", "error", "-i", str(seg_path),
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    raw = subprocess.check_output(cmd, timeout=120)
    probe = ffprobe(seg_path)
    w, h = probe["width"], probe["height"]
    frames = np.frombuffer(raw, dtype=np.uint8)
    n = len(frames) // (w * h)
    if n < 4:
        return {"error": "帧数不足"}
    frames = frames[:n * w * h].reshape(n, h, w).astype(np.float32)
    diffs = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    seam = float(diffs[0]) if len(diffs) else 0.0
    inner = float(diffs[1:].mean()) if len(diffs) > 1 else 0.0
    # 锐度（拉普拉斯方差）
    lap = (np.abs(4 * frames[:, 1:-1, 1:-1]
                   - frames[:, :-2, 1:-1] - frames[:, 2:, 1:-1]
                   - frames[:, 1:-1, :-2] - frames[:, 1:-1, 2:])).mean(axis=(1, 2))
    return {
        "frames": int(n),
        "seam_frame_diff": round(seam, 3),
        "inner_avg_frame_diff": round(inner, 3),
        "seam_vs_inner_ratio": round(seam / inner, 3) if inner > 0 else None,
        "first_frame_sharpness": round(float(lap[0]), 3),
        "last_frame_sharpness": round(float(lap[-1]), 3),
        "mean_sharpness": round(float(lap.mean()), 3),
    }


def run_chain(tag, mode, params1, refs1, params2, refs2):
    result = {"tag": tag, "mode": mode}
    print(f"\n=== [{tag}] 段1 提交 ===", flush=True)
    job1 = submit(mode, params1, refs1)
    result["job1"] = job1
    print(f"[{tag}] 段1 job={job1}", flush=True)
    status, error, outputs = wait_done(job1)
    result["job1_status"] = status
    result["job1_error"] = error
    result["job1_outputs"] = outputs
    print(f"[{tag}] 段1 -> {status} {error or ''}", flush=True)
    if status != "done":
        return result

    p1 = job_params(job1)
    cont1 = p1.get("continuation") or {}
    result["job1_continuation"] = cont1
    result["job1_twopass"] = p1.get("twopass")

    latent_dir = cont1.get("latent_run_id") or ""
    stage_path = RELAY / latent_dir / "stage_00000.safetensors"
    result["job1_latent_path"] = str(stage_path)
    result["job1_latent_exists"] = stage_path.is_file()
    result["job1_latent_bytes"] = (stage_path.stat().st_size
                                   if stage_path.is_file() else 0)
    print(f"[{tag}] 段1 潜空间 {stage_path} 存在={result['job1_latent_exists']} "
          f"大小={result['job1_latent_bytes']}", flush=True)

    # 网格对齐核对（用真实落盘的 shape）
    duration = float(p1.get("duration") or 0)
    fc = align_frame_count(int(round(duration * 24)))
    expect_T = video_latent_t(fc)
    result["grid"] = {"duration": duration, "frame_count": fc,
                       "expected_latent_T": expect_T,
                       "tail_aligned_22": tail_aligned(fc, 22)}
    if stage_path.is_file():
        shape = latent_shape(stage_path)
        result["grid"]["actual_latent_shape"] = shape
        result["grid"]["actual_latent_T"] = shape[2] if shape else None
        result["grid"]["aligned"] = bool(shape and shape[2] == expect_T)
        print(f"[{tag}] 网格: fc={fc} 期望T={expect_T} 实际T="
              f"{result['grid']['actual_latent_T']} 对齐={result['grid']['aligned']}",
              flush=True)

    # 点「接续」
    ctx = api(f"/api/jobs/{job1}/continuation")
    result["continuation_ctx_ok"] = ctx.get("ok")
    result["continuation_ctx_error"] = ctx.get("error")
    result["continuation_ctx_audio"] = ctx.get("audio")
    print(f"[{tag}] 接续上下文 ok={ctx.get('ok')} error={ctx.get('error') or ''}",
          flush=True)
    if not ctx.get("ok"):
        return result

    params2 = dict(params2)
    params2["continuation"] = ctx["continuation"]
    print(f"[{tag}] 段2 提交 ===", flush=True)
    job2 = submit(mode, params2, refs2)
    result["job2"] = job2
    print(f"[{tag}] 段2 job={job2}", flush=True)
    status2, error2, outputs2 = wait_done(job2)
    result["job2_status"] = status2
    result["job2_error"] = error2
    result["job2_outputs"] = outputs2
    print(f"[{tag}] 段2 -> {status2} {error2 or ''}", flush=True)
    if status2 != "done":
        return result

    p2 = job_params(job2)
    cont2 = p2.get("continuation") or {}
    result["job2_continuation"] = cont2
    result["job2_twopass"] = p2.get("twopass")
    # 段2 的读回坐标必须指向段1。
    #
    # 【注意：路径是「带内容指纹的副本」】服务端为了破 ComfyUI 的节点缓存，
    # 会把上游潜空间复制成带指纹的文件名（stage_00000_<hash>.safetensors）。
    # 所以这里要比的是**目录 + 内容**，不是完整文件名。
    src_path = cont2.get("source_latent_path") or ""
    orig_path = result.get("job1_latent_path") or ""
    result["job2_reads_upstream"] = bool(
        src_path and orig_path
        and Path(src_path).parent == Path(orig_path).parent)
    result["job2_source_path"] = src_path
    result["job2_source_is_fingerprinted"] = (
        bool(src_path) and Path(src_path).name != Path(orig_path).name)
    # 指纹副本内容必须与原文件**逐字节相同**
    if src_path and orig_path and Path(src_path).is_file():
        import hashlib
        h1 = hashlib.sha256(Path(orig_path).read_bytes()).hexdigest()
        h2 = hashlib.sha256(Path(src_path).read_bytes()).hexdigest()
        result["job2_fingerprint_content_identical"] = (h1 == h2)
    print(f"[{tag}] 段2 读回段1 目录={result['job2_reads_upstream']} "
          f"指纹副本={result.get('job2_source_is_fingerprinted')} "
          f"内容一致={result.get('job2_fingerprint_content_identical')}",
          flush=True)

    # ffprobe 产物
    def probe_outputs(outputs, label):
        found = []
        for out in outputs or []:
            if out.get("kind") != "video":
                continue
            # 产物落在 H3-Studio 自己的输出目录（DATA/outputs/<file>），
            # 不是 ComfyUI 的 output/ —— ComfyUI 那边的中间文件不登记。
            path = DATA / "outputs" / out["file"]
            if not path.is_file():
                continue
            info = ffprobe(path)
            info["kind"] = out.get("kind")
            info["job"] = label
            found.append(info)
        return found

    result["job1_videos"] = probe_outputs(outputs, job1)
    result["job2_videos"] = probe_outputs(outputs2, job2)
    for info in result["job1_videos"] + result["job2_videos"]:
        print(f"[{tag}] {info['job']} 视频 {info['path']} "
              f"时长={info['format_duration']:.3f}s 帧={info['frames']} "
              f"音轨={info['has_audio']}", flush=True)

    # 接缝量化（对每段视频）
    result["seam_metrics"] = []
    for info in result["job2_videos"]:
        try:
            metrics = seam_metrics(info["path"])
            metrics["path"] = info["path"]
            result["seam_metrics"].append(metrics)
            print(f"[{tag}] 接缝量化: {json.dumps(metrics, ensure_ascii=False)}",
                  flush=True)
        except Exception as exc:
            result["seam_metrics"].append({"path": info["path"],
                                           "error": str(exc)})

    # 段2 的工作流 —— 证明**真的接桥了**（不是静默退化成普通生成）。
    # 从 ComfyUI 的历史 API 取回实际提交过的工作流。
    result["job2_workflow"] = fetch_workflow(job2)

    return result


def fetch_workflow(job_id):
    """从 ComfyUI 的 /history 取回该任务实际提交过的工作流。

    这是「没有静默退化」的**最终**证据：只看服务端参数不够 ——
    必须看 ComfyUI 真正收到的工作流里有没有 300:load/bridge/trim/save，
    以及 SelfLift 采样器的 positive 是不是走桥第 4 路。

    【注意 key】ComfyUI 的 /history 是按**它自己的 prompt_id** 索引的，
    不是 H3 的 job_id —— 所以要先从 jobs.db 取出 prompt_id。
    """
    prompt_id = ""
    try:
        conn = sqlite3.connect(DATA / "jobs.db")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT prompt_id FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        conn.close()
        prompt_id = (row["prompt_id"] if row else "") or ""
    except Exception as exc:
        return {"error": "取 prompt_id 失败: %s" % exc}
    if not prompt_id:
        return {"error": "任务没有 prompt_id（没提交过）"}
    try:
        hist = api("/history/%s" % prompt_id, base="http://127.0.0.1:8188")
    except Exception as exc:
        return {"error": "取 ComfyUI 历史失败: %s" % exc}
    if not isinstance(hist, dict) or not hist:
        return {"error": "ComfyUI 历史里没有这个 prompt_id", "prompt_id": prompt_id}
    entry = None
    for value in hist.values():
        if isinstance(value, dict):
            entry = value
            break
    if not entry:
        return {"error": "ComfyUI 历史条目为空"}
    prompt = (entry.get("prompt") or [None, None])[2]
    if not isinstance(prompt, dict):
        return {"error": "ComfyUI 历史里没有 prompt 体"}
    nodes = {nid: node for nid, node in prompt.items()}
    check = {
        "node_count": len(nodes),
        "has_300_load": "300:load" in nodes,
        "has_300_bridge": "300:bridge" in nodes,
        "has_300_trim": "300:trim" in nodes,
        "has_300_save": "300:save" in nodes,
        "has_selflift_sampler": any(
            (n.get("class_type") == "SelfLiftH3Sampler") for n in nodes.values()),
    }
    sampler = next((n for n in nodes.values()
                    if n.get("class_type") == "SelfLiftH3Sampler"), None)
    if sampler:
        pos = (sampler.get("inputs") or {}).get("positive")
        lat = (sampler.get("inputs") or {}).get("latent_image")
        check["selflift_positive"] = pos
        check["selflift_latent_image"] = lat
        # 关键：positive 与 latent_image 都必须指向拷贝桥
        check["selflift_consumes_bridge"] = bool(
            pos == ["300:bridge", 3] and lat == ["300:bridge", 0])
    bridge = nodes.get("300:bridge")
    if bridge:
        check["bridge_mask_mode"] = (bridge.get("inputs") or {}).get("mask_mode")
        check["bridge_context_frames"] = (bridge.get("inputs") or {}).get(
            "context_frames")
    save = nodes.get("300:save")
    if save:
        check["save_latent"] = (save.get("inputs") or {}).get("latent")
        check["save_run_id"] = (save.get("inputs") or {}).get("run_id")
    load = nodes.get("300:load")
    if load:
        check["load_explicit_path"] = (load.get("inputs") or {}).get(
            "explicit_path")
    # 整体判定
    check["really_bridged"] = bool(
        check["has_300_load"] and check["has_300_bridge"]
        and check["has_300_trim"] and check["has_300_save"]
        and check.get("selflift_consumes_bridge"))
    return check


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = OUT_ROOT / f"cont-selflift-live-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"stamp": stamp, "base": BASE, "chains": []}

    image, audio = pick_assets()
    print(f"素材: image={image} audio={audio}", flush=True)
    report["assets"] = {"image": image, "audio": audio}

    twopass = {
        "enabled": True,
        "transition_step": 5,
        "lowres_scale": 0.75,
        "rho": 0.0,
        "w_min": 0.8,
        "w_max": 1.0,
        "upscaler_model": "minimax_h3_latent_upscaler_3d_fp16.safetensors",
        "cfg": 1.0,
    }

    # ---- 链 A：t2v 二采（无参考媒体，最小变量）----
    t2v_common = {
        "prompt": "一只橘猫在草地上奔跑，镜头跟拍",
        "model_key": "fl2va_pruned_int8",
        "duration": 5.0,
        "width": 1280,
        "height": 736,
        "steps": 4,
        "seed": 20261117,
        "sampler": "euler",
        "scheduler": "beta",
        "twopass": twopass,
        "aspect": "16:9",
    }
    try:
        chain = run_chain("t2v-selflift", "t2v", dict(t2v_common), {},
                          dict(t2v_common, seed=20261118), {})
        report["chains"].append(chain)
    except Exception as exc:
        report["chains"].append({"tag": "t2v-selflift", "fatal": str(exc)})
        print(f"t2v 二采链异常: {exc}", flush=True)

    # ---- 链 B：r2v 二采（参考图 + 参考音乐，走素材库引用）----
    if image:
        refs = []
        if audio:
            refs.append({"asset_id": str(audio["id"]), "slot": "ref_audio",
                         "ordinal": 0})
        refs.insert(0, {"asset_id": str(image["id"]), "slot": "ref_image",
                        "ordinal": 0})
        r2v_common = {
            "prompt": "参考图里的主体在移动，镜头缓慢推进",
            "model_key": "ref2va_pruned_int8",
            "duration": 5.0,
            # 参考生视频（Ref2VA）有分辨率上限 0.8M 像素
            # （服务端会明确报错：当前请求 942,080 px 超出上限）。
            "width": 1024,
            "height": 576,
            "steps": 4,
            "seed": 20261117,
            "sampler": "euler",
            "scheduler": "beta",
            "twopass": twopass,
            "aspect": "16:9",
            "audio_track_source": "reference" if audio else "generated",
        }
        try:
            chain = run_chain("r2v-selflift", "r2v", dict(r2v_common), refs,
                              dict(r2v_common, seed=20261118), refs)
            report["chains"].append(chain)
        except Exception as exc:
            report["chains"].append({"tag": "r2v-selflift", "fatal": str(exc)})
            print(f"r2v 二采链异常: {exc}", flush=True)

    path = out_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n证据: {path}", flush=True)

    # 摘要
    print("\n" + "=" * 70)
    for chain in report["chains"]:
        print(f"[{chain.get('tag')}] 段1={chain.get('job1_status')} "
              f"段2={chain.get('job2_status')}")
        grid = chain.get("grid") or {}
        if grid:
            print(f"   网格: fc={grid.get('frame_count')} "
                  f"T={grid.get('actual_latent_T')}/"
                  f"{grid.get('expected_latent_T')} "
                  f"对齐={grid.get('aligned')}")
        print(f"   接续上下文 ok={chain.get('continuation_ctx_ok')} "
              f"error={chain.get('continuation_ctx_error') or ''}")
        print(f"   段2 读回段1={chain.get('job2_reads_upstream')}")
        for m in chain.get("seam_metrics") or []:
            print(f"   接缝: seam/inner={m.get('seam_vs_inner_ratio')} "
                  f"sharp首={m.get('first_frame_sharpness')} "
                  f"sharp尾={m.get('last_frame_sharpness')}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
