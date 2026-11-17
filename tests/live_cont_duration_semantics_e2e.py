# -*- coding: utf-8 -*-
"""线上 8190 真实跑通：接续**成片时长**（用户更正口径，2026-11-17）。

【跑什么 —— 用户给的两个场景】
  场景 A：5 秒 -> 接续 5 秒 -> 成片
           用户口径 9 秒；实际帧数 124 + 102 = 226 帧 = 9.4167 秒
  场景 B：5 秒 -> 接续 5 秒 -> 再接续 8 秒 -> 成片
           用户口径 16 秒；实际帧数 124 + 102 + 170 = 396 帧 = 16.5 秒

【为什么实测值与用户口径有差 —— 必须一起核对】
  H3 两条硬网格约束（不是我们的选择）：
    * 段长只能落 17k+5 帧网格（请求 5s = 120 帧 -> 实际 124 帧）；
    * 拷贝桥只接受 5+17k 合法钉住窗口（24 帧会被 relay_core.py:411
      raise），22 帧 = 0.9167s 是最接近 1 秒的合法值。
  所以「用户口径」与「实测帧数」是两个数，脚本**两个都断言**，
  并断言它们的差符合预期（8 帧网格余量 + 2 帧重叠差 = 10 帧）。

【产出】
  evidence/cont-duration-semantics-live-<时间戳>/ 下的 JSON + 产物副本

运行：python3 tests/live_cont_duration_semantics_e2e.py
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
REPO = Path("/root/projects/comfyui-minimax-h3-studio")
DATA = REPO / "data"
OUT_ROOT = Path("/root/projects/h3studio-live/evidence")

FPS = 24
#: H3 帧网格常量（与 h3studio/continuation.py 同值）
WINDOW = 22                    # 合法钉住窗口（帧）= 0.9167s


def align_frame_count(length: int) -> int:
    """段长 -> 17k+5 网格帧数（与 continuation.align_frame_count 同公式）。"""
    n = max(5, int(length))
    if n % 17 == 5:
        return n
    return n + (5 - (n % 17)) % 17


def expected(durations: list[float]) -> dict:
    """按帧网格算预期（与 cont.chain_duration_account 同口径）。"""
    segs = []
    total = 0
    for i, d in enumerate(durations):
        want = int(round(d * FPS))
        fc = align_frame_count(want)
        delivered = fc if i == 0 else fc - WINDOW
        total += delivered
        segs.append({"requested_seconds": d, "frame_count": fc,
                     "delivered_frames": delivered})
    formula = sum(durations) - max(0, len(durations) - 1) * 1.0
    return {"segments": segs, "total_frames": total,
            "total_seconds": total / FPS,
            "formula_seconds": formula,
            "delta_seconds": total / FPS - formula}


def api(path, method="GET", payload=None, timeout=60):
    url = f"{BASE}{path}"
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
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
           "-show_entries",
           "stream=codec_type,duration,nb_read_frames,width,height:"
           "format=duration", "-of", "json", str(path)]
    raw = subprocess.check_output(cmd, encoding="utf-8", timeout=120)
    data = json.loads(raw)
    streams = data.get("streams") or []
    video = streams[0] if streams else {}
    frames = video.get("nb_read_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    has_audio = False
    try:
        raw2 = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type", "-of", "json", str(path)],
            encoding="utf-8", timeout=60)
        has_audio = bool((json.loads(raw2).get("streams") or []))
    except Exception:
        pass
    return {"path": str(path),
            "format_duration": float((data.get("format") or {}).get("duration") or 0),
            "video_duration": float(video.get("duration") or 0),
            "width": video.get("width"), "height": video.get("height"),
            "frames": frames, "has_audio": has_audio}


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
        if not row:
            time.sleep(2)
            continue
        if row["status"] in ("done", "failed", "canceled"):
            return (row["status"], row["error"],
                    json.loads(row["outputs"] or "[]"))
        last = f"{row['status']} {row.get('progress_text') or ''}"
        time.sleep(4)
    return "timeout", last, []


def submit(mode, params, asset_refs=None):
    boundary = "----h3studioDur" + hashlib.sha1(
        f"{mode}{time.time()}".encode()).hexdigest()[:16]
    parts = []

    def field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))

    field("mode", mode)
    field("params", json.dumps(params, ensure_ascii=False))
    if asset_refs:
        field("asset_refs", json.dumps(asset_refs, ensure_ascii=False))
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/jobs", data=body, method="POST",
        headers={"Content-Type":
                 f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"提交失败 HTTP {exc.code}: {detail}") from exc
    if not data.get("id"):
        raise RuntimeError(f"提交失败: {data}")
    return data["id"]


BASE_PARAMS = {
    "prompt": "一只橘猫在洒满阳光的旧书桌上走过，镜头缓缓跟随。",
    "model_key": "fl2va_pruned_int8",
    "width": 1280, "height": 736,
    "aspect": "16:9", "resolution": "768p",
    "steps": 4, "sampler": "euler", "scheduler": "simple",
    "cfg": 1.0, "shift_video": 7.0, "shift_audio": 3.0,
}


def run_chain(durations: list[float], tag: str, report: dict, checks: list) -> None:
    """跑一条多段接续链，逐段核对帧数与成片。"""
    print(f"\n=== [{tag}] 链：请求时长 {durations} ===", flush=True)
    exp = expected(durations)
    report[tag] = {"durations": durations, "expected": exp, "jobs": []}

    prev_job = ""
    cont = None
    for index, dur in enumerate(durations):
        params = dict(BASE_PARAMS)
        params["duration"] = dur
        if cont is not None:
            params["continuation"] = cont
        job = submit("t2v", params)
        report[tag]["jobs"].append(job)
        print(f"  段{index + 1}: job={job} 请求 {dur}s 提交完成", flush=True)
        status, error, outs = wait_done(job)
        print(f"    -> {status} {error or ''}", flush=True)
        if status != "done":
            checks.append((f"[{tag}] 段{index + 1} 生成成功", False))
            return
        p = job_params(job)
        info = p.get("continuation") or {}
        seg_rel = info.get("segment_rel") or ""
        seg_path = DATA / "outputs" / seg_rel if seg_rel else None
        probe = ffprobe(seg_path) if (seg_path and seg_path.is_file()) else None
        want_frames = exp["segments"][index]["delivered_frames"]
        print(f"    段{index + 1} 分段: {seg_rel} "
              f"{probe['frames'] if probe else '无'} 帧（预期 {want_frames}）",
              flush=True)
        checks.append((f"[{tag}] 段{index + 1} 交付 {want_frames} 帧",
                       bool(probe) and probe["frames"] == want_frames))
        report[tag]["jobs"][-1] = {"job": job, "seg_rel": seg_rel,
                                   "probe": probe}

        # 取下一段的接续上下文（带下一段的请求时长 -> 服务端算成片时长账）
        #
        # 【为什么最后一段不取】最后一段没有「下一段」了 ——
        # 取不到 durations[index + 1] 会越界。成片的核对在链尾做。
        if index + 1 < len(durations):
            ctx = api(f"/api/jobs/{job}/continuation"
                      f"?duration={durations[index + 1]}")
            plan = ctx.get("duration_plan") or {}
            report[tag]["jobs"][-1]["duration_plan"] = plan
            # 【口径值要按「到本段为止的累计」算】
            # 服务端返回的是「接上这一段之后」的账 —— 那是
            # Σ(前 index+2 段的请求时长) - (index+1) × 1 秒。
            # 不能拿整条链的最终口径值来比。
            upto = durations[: index + 2]
            want_formula = (sum(upto) - max(0, len(upto) - 1) * 1.0)
            want_frames = expected(upto)["total_frames"]
            print(f"    服务端时长账: 口径 {plan.get('formula_seconds')}s / "
                  f"实际 {plan.get('total_seconds')}s / "
                  f"差 {plan.get('delta_seconds')}s", flush=True)
            checks.append((f"[{tag}] 段{index + 1} 之后的口径值 == {want_formula}",
                           abs(float(plan.get("formula_seconds") or 0)
                               - want_formula) < 1e-6))
            checks.append((f"[{tag}] 段{index + 1} 之后的帧数 == {want_frames}",
                           int(plan.get("total_frames") or 0) == want_frames))
            cont = ctx.get("continuation") or {}
            prev_job = job

    # ---- 成片 ----
    last = report[tag]["jobs"][-1]
    outs = json.loads(job_row(last["job"])["outputs"] or "[]")
    full_rel = next((o.get("file") for o in outs if o.get("is_full")), "")
    report[tag]["full_rel"] = full_rel
    full_path = DATA / "outputs" / full_rel if full_rel else None
    probe = ffprobe(full_path) if (full_path and full_path.is_file()) else None
    report[tag]["full_probe"] = probe
    print(f"  成片: {full_rel} "
          f"{probe['frames'] if probe else '无'} 帧 "
          f"{probe['format_duration'] if probe else 0:.4f}s", flush=True)
    checks.append((f"[{tag}] 成片存在", bool(full_rel)))
    if probe:
        checks.append((f"[{tag}] 成片帧数 == {exp['total_frames']}",
                       probe["frames"] == exp["total_frames"]))
        # 【为什么断言 video_duration 而不是 format_duration】
        # format_duration 是**容器**时长（音轨比视频长时它更长 ——
        # 成片音轨用原始参考音频整条 + apad，长度按容器算）。
        # 「成片多长」说的是**画面**长度 -> 用 video_duration。
        checks.append((f"[{tag}] 成片画面时长 ≈ {exp['total_seconds']:.4f}s",
                       abs(probe["video_duration"] - exp["total_seconds"])
                       < 0.02))
        print(f"  用户口径 {exp['formula_seconds']}s / 实际画面 "
              f"{probe['video_duration']:.4f}s（容器 {probe['format_duration']:.4f}s）"
              f" / 差 {probe['video_duration'] - exp['formula_seconds']:+.4f}s",
              flush=True)


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evdir = OUT_ROOT / f"cont-duration-semantics-live-{stamp}"
    evdir.mkdir(parents=True, exist_ok=True)
    report = {"stamp": stamp, "base": BASE,
              "window_frames": WINDOW, "fps": FPS}
    checks: list[tuple[str, bool]] = []

    # 【可以只跑一个场景】环境变量 H3STUDIO_DUR_SCENARIO=A / B
    # （并发跑 E2E 时，ComfyUI 队列会被别的任务打断 —— 那种失败是
    #   「队列被抢」，不是「时长算错」。分开跑更稳。）
    scenario = (os.environ.get("H3STUDIO_DUR_SCENARIO") or "AB").upper()
    # 场景 A：5 秒 -> 接续 5 秒
    if "A" in scenario:
        run_chain([5.0, 5.0], "A_5s_then_5s", report, checks)
    # 场景 B：5 秒 -> 接续 5 秒 -> 再接续 8 秒
    if "B" in scenario:
        run_chain([5.0, 5.0, 8.0], "B_5s_5s_8s", report, checks)

    report["checks"] = [{"name": n, "ok": bool(o)} for n, o in checks]
    (evdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    ok = True
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'} {name}")
        ok = ok and passed
    print("=" * 68)
    print(f"证据目录：{evdir}")
    if not ok:
        print("❌ 有检查项未通过")
        return 1
    print("✅ 全部检查项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
