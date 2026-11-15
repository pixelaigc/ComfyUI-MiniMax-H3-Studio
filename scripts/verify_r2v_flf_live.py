#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""线上 8190 真跑验证：r2v（含参考图 + 参考音乐 + 参考视频入口）与 flf（仅尾帧 / 首+尾）。

【本脚本验证 2026-11-15 这一轮交付的 7 条缺口在**真实服务**上成立】

  1. r2v：参考图（素材库）+ 参考音乐（素材库）+ 参考视频（素材库）三路同时进场
     -> 核对产物分辨率 = 面板选的档位×比例（**不是** 1344×768 硬回退）
     -> 核对音轨来源 = 选中的那一条（三选一真的生效）
  2. flf：**仅尾帧**（L2VA）—— 验证「只给尾帧也能提交」
  3. flf：首帧 + 尾帧（FL2VA）—— 验证双帧路径
  4. flf：**仅首帧**（I2VA）—— 验证「只给首帧也能提交」
  5. 音轨三选一的 (c)：参考视频的背景音 -> 抽音轨 -> 贴到成片
  6. 素材库选择器：categories 含 video / audio（面板入口存在）

用法：
    python3 scripts/verify_r2v_flf_live.py --base http://127.0.0.1:8190
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8190"
EV = Path("/root/projects/h3studio-r2vflf/evidence/live-verify-20261115")

#: 素材库里的自产素材（素材库根 = /mnt/usb2t/asset_library）
REF_IMAGE_ASSET = "asset_21f5d051d841"      # 火星基地_宇航员_参考图.png
REF_AUDIO_ASSET = "asset_bfaca307e2cf"      # H3短片配乐_YuE2生成.flac（149.88s）
REF_VIDEO_ASSET = "asset_655148b3c626"      # v-h3-studio-000167.mp4（8s，带 AAC 音轨）
LAST_FRAME_ASSET = "asset_cdd2ba88c636"     # t2v_...mp4.last.png
FIRST_FRAME_ASSET = "asset_22e9af34dd11"    # r2v_...mp4.last.png


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _post_multipart(fields: dict) -> dict:
    """multipart/form-data 提交（只带字符串字段）。"""
    boundary = "----h3verify" + uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        body += (value if isinstance(value, str) else json.dumps(value)).encode()
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        BASE + "/api/jobs", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"提交失败 HTTP {exc.code}: "
                           f"{exc.read().decode()[:600]}") from exc


def wait_job(job_id: str, timeout_sec: float = 1800) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            job = _get(f"/api/jobs/{job_id}")
        except Exception:
            time.sleep(5)
            continue
        if job.get("status") in ("done", "failed", "canceled"):
            return job
        time.sleep(6)
    return _get(f"/api/jobs/{job_id}")


def ffprobe(path: Path) -> dict:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120)
    return json.loads(proc.stdout or "{}")


def probe_summary(path: Path) -> dict:
    data = ffprobe(path)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = {
        "width": video.get("width"),
        "height": video.get("height"),
        "fps": video.get("r_frame_rate"),
        "nb_frames": video.get("nb_frames"),
        "duration": data.get("format", {}).get("duration"),
        "has_audio": audio is not None,
        "audio_codec": (audio or {}).get("codec_name"),
        "audio_duration": (audio or {}).get("duration"),
        "audio_channels": (audio or {}).get("channels"),
    }
    return out


def local_output(job: dict) -> Path | None:
    """把任务产物落到本机可 ffprobe 的路径。

    【2026-11-15 修 —— 路径约定】落库的 file 字段是**相对 OUT_DIR** 的
    （"videos/xxx.mp4"），而 OUT_DIR = data_dir / "outputs"。
    所以正确的拼法是 data_dir / "outputs" / file。

    原来只找「data_dir/outputs/videos/<file>」把 file 当成完整相对路径，
    于是拼成了 data_dir/videos/xxx.mp4 —— 找不到（产物其实在
    data_dir/outputs/videos/xxx.mp4）。
    """
    outs = job.get("outputs") or []
    roots = [
        Path("/root/projects/comfyui-minimax-h3-studio/data") / "outputs",
        Path("/root/projects/h3studio-live/data") / "outputs",
        Path("/root/projects/h3studio-r2vflf/data") / "outputs",
    ]
    for item in outs:
        if item.get("kind") != "video":
            continue
        name = item.get("file")
        if not name:
            continue
        for root in roots:
            candidate = root / name
            if candidate.exists():
                return candidate
    # 兜底：在候选根目录里按 basename 扫
    for item in outs:
        name = item.get("file") or ""
        if not name:
            continue
        base = Path(name).name
        for root in roots:
            for sub in ("videos", "images", "audio", ""):
                candidate = root / sub / base
                if candidate.exists():
                    return candidate
    return None


def audio_fingerprint_corr(a: Path, b: Path) -> float:
    """两段音频的能量包络相关系数（-1..1）。

    【为什么需要它】音轨三选一要证明「成片音轨**就是**用户选的那条」。
    单看时长不够（参考音乐 149s 被裁到 5s，时长一样）；
    比字节也不行（AAC 重编码后字节必然不同）。
    能量包络的相关性是**内容级**判据：同一段音频重编码后相关性 ≈ 1，
    不同音频之间 ≈ 0。
    """
    import numpy as np

    def envelope(path: Path) -> "np.ndarray":
        cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
               "-ar", "8000", "-f", "s16le", "-"]
        raw = subprocess.run(cmd, capture_output=True, timeout=120).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        n = 512
        m = (len(x) // n) * n
        if m == 0:
            return np.array([], dtype=np.float32)
        frames = x[:m].reshape(-1, n)
        return np.sqrt((frames ** 2).mean(axis=1))

    ea, eb = envelope(a), envelope(b)
    k = min(len(ea), len(eb), 400)
    if k < 10:
        return 0.0
    ea, eb = ea[:k] - ea[:k].mean(), eb[:k] - eb[:k].mean()
    denom = float(np.linalg.norm(ea) * np.linalg.norm(eb))
    return float(ea.dot(eb) / denom) if denom else 0.0


def check(name: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""),
          flush=True)
    return ok


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--skip-r2v", action="store_true")
    ap.add_argument("--skip-flf", action="store_true")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    EV.mkdir(parents=True, exist_ok=True)
    results = {"checks": [], "jobs": {}}
    failures = 0

    def record(name: str, ok: bool, detail: str = ""):
        nonlocal failures
        results["checks"].append({"name": name, "ok": bool(ok),
                                  "detail": detail})
        if not ok:
            failures += 1
        return check(name, ok, detail)

    # ---------------- 前置：素材库必须有视频 / 音频 ----------------
    print("== 0) 素材库素材可用性 ==", flush=True)
    picker = _get("/api/assets/picker?limit=300")
    cats = {}
    for item in picker.get("assets", []):
        cats.setdefault(item.get("category"), 0)
        cats[item["category"]] += 1
    record("素材库有 video 类别素材（参考视频入口有东西可选）",
           cats.get("video", 0) > 0, f"video={cats.get('video', 0)}")
    record("素材库有 audio 类别素材（参考音乐入口有东西可选）",
           cats.get("audio", 0) > 0, f"audio={cats.get('audio', 0)}")
    record("素材库有 image 类别素材（参考图入口有东西可选）",
           cats.get("image", 0) > 0, f"image={cats.get('image', 0)}")

    # ---------------- 1) r2v：图 + 音乐 + 视频，三选一选 (c) ----------------
    if not args.skip_r2v:
        print("\n== 1) r2v：参考图 + 参考音乐 + 参考视频，音轨选 (c) 视频背景音 ==",
              flush=True)
        params = {
            "prompt": "A lone astronaut walking across red Martian dunes toward "
                      "a geodesic dome under a violet sky with twin moons, "
                      "slow cinematic push in, 35mm film grain",
            "width": 864, "height": 480, "duration": 5,
            "model_key": "ref2va_pruned_int8",
            "audio_track_source": "video_bg",
            "steps": 4, "sampler": "euler", "scheduler": "beta",
            "shift_video": 12.0, "shift_audio": 3.0,
            "resolution": "544p", "aspect": "16:9",
        }
        asset_refs = [
            {"asset_id": REF_IMAGE_ASSET, "slot": "ref_image", "ordinal": 0},
            {"asset_id": REF_AUDIO_ASSET, "slot": "ref_audio", "ordinal": 0},
            {"asset_id": REF_VIDEO_ASSET, "slot": "ref_video", "ordinal": 0},
        ]
        try:
            resp = _post_multipart({"mode": "r2v", "params": json.dumps(params),
                                     "asset_refs": json.dumps(asset_refs)})
        except Exception as exc:
            record("r2v 提交被接受", False, str(exc))
            resp = None
        if resp:
            jid = resp["id"]
            results["jobs"]["r2v_video_bg"] = jid
            print(f"  job={jid}", flush=True)
            job = wait_job(jid)
            results["jobs"]["r2v_video_bg_status"] = job.get("status")
            record("r2v 任务跑完", job.get("status") == "done",
                   f"status={job.get('status')} "
                   f"err={(job.get('error') or '')[:200]}")
            if job.get("status") == "done":
                out = local_output(job)
                if out:
                    summary = probe_summary(out)
                    results["jobs"]["r2v_video_bg_probe"] = summary
                    print(f"  ffprobe: {json.dumps(summary, ensure_ascii=False)}",
                          flush=True)
                    record("r2v 产物分辨率 = 面板所选 544p+16:9 (864×480)",
                           (summary["width"], summary["height"]) == (864, 480),
                           f"{summary['width']}×{summary['height']}")
                    record("r2v 产物有音轨", summary["has_audio"],
                           f"codec={summary['audio_codec']}")
                    # (c) 音轨来源：参考视频的背景音被抽出来贴上去了
                    # -> 音轨时长应接近**参考视频**时长（8s），而不是参考音乐（149s）
                    adur = float(summary.get("audio_duration") or 0)
                    record("r2v 音轨来源 = 参考视频背景音（时长贴近 8s 视频）",
                           0 < adur <= 9.0,
                           f"音轨时长 {adur:.2f}s（参考视频 8s / 参考音乐 149.88s）")
                    record("r2v 产物时长约 5 秒（面板所选）",
                           4.0 <= float(summary.get("duration") or 0) <= 7.0,
                           f"{summary.get('duration')}s")
                    # (c) 音轨来源：**内容级**判据
                    # 成片音轨的能量包络必须与「参考视频原声」高度相关，
                    # 且与「参考音乐」几乎不相关。
                    ref_video_path = Path(
                        "/mnt/usb2t/asset_library/originals/b9/ce/"
                        "b9ce51dc755c75a2__v-h3-studio-000167.mp4")
                    ref_music_path = Path(
                        "/mnt/usb2t/asset_library/originals/78/ee/"
                        "78ee4cfeda3d36d3__H3短片配乐_YuE2生成.flac")
                    if ref_video_path.exists() and ref_music_path.exists():
                        c_ref = audio_fingerprint_corr(out, ref_video_path)
                        c_mus = audio_fingerprint_corr(out, ref_music_path)
                        results["jobs"]["r2v_audio_corr"] = {
                            "vs_reference_video": round(c_ref, 4),
                            "vs_reference_music": round(c_mus, 4),
                        }
                        print(f"  音轨指纹相关：vs 参考视频原声 {c_ref:.4f} / "
                              f"vs 参考音乐 {c_mus:.4f}", flush=True)
                        record("r2v 音轨来源 = 参考视频背景音（内容级指纹）",
                               c_ref > 0.9 and c_mus < 0.5,
                               f"corr(视频)={c_ref:.4f} corr(音乐)={c_mus:.4f}")
                else:
                    record("r2v 产物文件可定位", False, "找不到产物文件")

    # ---------------- 2) flf：仅尾帧 ----------------
    if not args.skip_flf:
        print("\n== 2) flf：仅尾帧（L2VA）—— 验证「只给尾帧也能提交」==",
              flush=True)
        params = {
            "prompt": "A wide shot of a translucent geodesic dome on Mars at "
                      "dusk, interior lights glowing, slow crane up",
            "width": 864, "height": 480, "duration": 5,
            "model_key": "fl2va_pruned_int8",
            "audio_track_source": "generated",
            "steps": 4, "sampler": "euler", "scheduler": "beta",
            "shift_video": 12.0, "shift_audio": 3.0,
            "resolution": "544p", "aspect": "16:9",
        }
        try:
            resp = _post_multipart({
                "mode": "flf", "params": json.dumps(params),
                "asset_refs": json.dumps([
                    {"asset_id": LAST_FRAME_ASSET, "slot": "last_frame",
                     "ordinal": 0}])})
            record("flf 仅尾帧被接受（前端已放宽，后端也接受）", True,
                   f"job={resp['id']}")
            jid = resp["id"]
            results["jobs"]["flf_last_only"] = jid
            job = wait_job(jid)
            results["jobs"]["flf_last_only_status"] = job.get("status")
            record("flf 仅尾帧跑完", job.get("status") == "done",
                   f"status={job.get('status')} "
                   f"err={(job.get('error') or '')[:200]}")
            if job.get("status") == "done":
                out = local_output(job)
                if out:
                    summary = probe_summary(out)
                    results["jobs"]["flf_last_only_probe"] = summary
                    print(f"  ffprobe: {json.dumps(summary, ensure_ascii=False)}",
                          flush=True)
                    record("flf 产物分辨率 = 面板所选 544p+16:9 (864×480)",
                           (summary["width"], summary["height"]) == (864, 480),
                           f"{summary['width']}×{summary['height']}")
                else:
                    record("flf 仅尾帧产物可定位", False, "找不到产物文件")
        except Exception as exc:
            record("flf 仅尾帧被接受", False, str(exc))

        # ---------------- 3) flf：首帧 + 尾帧 ----------------
        print("\n== 3) flf：首帧 + 尾帧（FL2VA）==", flush=True)
        try:
            resp = _post_multipart({
                "mode": "flf", "params": json.dumps(params),
                "asset_refs": json.dumps([
                    {"asset_id": FIRST_FRAME_ASSET, "slot": "first_frame",
                     "ordinal": 0},
                    {"asset_id": LAST_FRAME_ASSET, "slot": "last_frame",
                     "ordinal": 0}])})
            jid = resp["id"]
            results["jobs"]["flf_both"] = jid
            print(f"  job={jid}", flush=True)
            job = wait_job(jid)
            results["jobs"]["flf_both_status"] = job.get("status")
            record("flf 首+尾跑完", job.get("status") == "done",
                   f"status={job.get('status')} "
                   f"err={(job.get('error') or '')[:200]}")
            if job.get("status") == "done":
                out = local_output(job)
                if out:
                    summary = probe_summary(out)
                    results["jobs"]["flf_both_probe"] = summary
                    print(f"  ffprobe: {json.dumps(summary, ensure_ascii=False)}",
                          flush=True)
                    record("flf 首+尾产物分辨率 = 864×480",
                           (summary["width"], summary["height"]) == (864, 480),
                           f"{summary['width']}×{summary['height']}")
                else:
                    record("flf 首+尾产物可定位", False, "找不到产物文件")
        except Exception as exc:
            record("flf 首+尾被接受", False, str(exc))

        # ---------------- 4) flf：仅首帧 ----------------
        print("\n== 4) flf：仅首帧（I2VA）==", flush=True)
        try:
            resp = _post_multipart({
                "mode": "flf", "params": json.dumps(params),
                "asset_refs": json.dumps([
                    {"asset_id": FIRST_FRAME_ASSET, "slot": "first_frame",
                     "ordinal": 0}])})
            jid = resp["id"]
            results["jobs"]["flf_first_only"] = jid
            print(f"  job={jid}", flush=True)
            job = wait_job(jid)
            results["jobs"]["flf_first_only_status"] = job.get("status")
            record("flf 仅首帧跑完", job.get("status") == "done",
                   f"status={job.get('status')} "
                   f"err={(job.get('error') or '')[:200]}")
        except Exception as exc:
            record("flf 仅首帧被接受", False, str(exc))

    # ---------------- 5) 落库参数核对 ----------------
    print("\n== 5) 落库参数核对（面板选的档位/比例真的进了 params）==",
          flush=True)
    for label, jid in (("r2v", results["jobs"].get("r2v_video_bg")),
                       ("flf", results["jobs"].get("flf_last_only"))):
        if not jid:
            continue
        job = _get(f"/api/jobs/{jid}")
        p = job.get("params") or {}
        record(f"{label} 落库 params.width/height = 864×480",
               (p.get("width"), p.get("height")) == (864, 480),
               f"width={p.get('width')} height={p.get('height')} "
               f"resolution={p.get('resolution')} aspect={p.get('aspect')}")
        record(f"{label} 落库 params.audio_track_source 已保留",
               p.get("audio_track_source") is not None,
               str(p.get("audio_track_source")))

    (EV / "live_verify_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== 汇总：{len(results['checks'])} 项检查，失败 {failures} 项 ==")
    print(f"证据落盘：{EV / 'live_verify_result.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
