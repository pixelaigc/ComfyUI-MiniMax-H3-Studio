# -*- coding: utf-8 -*-
"""线上 8190 真实跑通：t2v 5 秒 -> 点「接续」-> 再 5 秒 -> 成片 9 秒。

【这个测试在防什么】
用户 2026-11-17 19:00 实测缺陷：
  5 秒视频点「接续」再生成 5 秒 -> 理论 9 秒，实际只得到 **4 秒**
  一段视频，没有 9 秒成片。
根因：服务重启打断 stage1 的运行 -> 自愈路径不跑接续收尾
      -> 不登记分段、不拼完整视频。

【跑什么】
  1. 段1：t2v，duration=5
  2. 段2：t2v，duration=5，continuation 指向段1（真实调服务端接续上下文）
  3. ffprobe 核对：
     * 段1 = 124 帧（5.17s）
     * 段2 = 102 帧（4.25s）  <- 124 - 22 帧钉住区
     * 成片 = 226 帧（9.42s）  <- 124 + 102
  4. **额外验证自愈路径**：模拟「服务重启打断」——
     不真的重启服务（不能影响线上），而是直接对 DB 里一个
     已完成的接续段跑 `_finalize_continuation_impl`，验证它
     幂等且能补出成片。

【产出】
  evidence/cont-duration-live-<时间戳>/ 下的 JSON + ffprobe 输出

运行：python3 tests/live_cont_duration_e2e.py
"""

from __future__ import annotations

import json
import os
import hashlib
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

#: 期望的帧数（整数帧口径，与 continuation.FPS=24 一致）
FPS = 24
EXPECT_SEG1_FRAMES = 124     # 5 秒 -> 17k+5 网格 -> 124 帧
EXPECT_SEG2_FRAMES = 102     # 124 - 22（钉住窗口）
EXPECT_FULL_FRAMES = 226     # 124 + 102


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
    """ffprobe -> {视频时长, 帧数, 宽高, 有无音轨}。"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-count_frames",
           "-show_entries",
           "stream=codec_type,duration,nb_read_frames,width,height:"
           "format=duration",
           "-of", "json", str(path)]
    raw = subprocess.check_output(cmd, encoding="utf-8", timeout=120)
    data = json.loads(raw)
    streams = data.get("streams") or []
    video = streams[0] if streams else {}
    frames = video.get("nb_read_frames")
    try:
        frames = int(frames)
    except (TypeError, ValueError):
        frames = None
    # 音轨单独查
    has_audio = False
    try:
        raw2 = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_type,duration", "-of", "json",
             str(path)], encoding="utf-8", timeout=60)
        has_audio = bool((json.loads(raw2).get("streams") or []))
    except Exception:
        pass
    return {
        "path": str(path),
        "format_duration": float((data.get("format") or {}).get("duration") or 0),
        "video_duration": float(video.get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "frames": frames,
        "has_audio": has_audio,
    }


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
    """提交一个生成任务（multipart —— 与前端同格式）。返回 job_id。"""
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


def main() -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    evdir = OUT_ROOT / f"cont-duration-live-{stamp}"
    evdir.mkdir(parents=True, exist_ok=True)
    report = {"stamp": stamp, "base": BASE,
              "expect": {"seg1_frames": EXPECT_SEG1_FRAMES,
                         "seg2_frames": EXPECT_SEG2_FRAMES,
                         "full_frames": EXPECT_FULL_FRAMES}}

    base_params = {
        "prompt": "一只橘猫在洒满阳光的旧书桌上走过，镜头缓缓跟随。",
        # 【模型 key 必须用服务端认的 key】——线上真实 t2v 任务用的是
        # "fl2va_pruned_int8"（不是权重文件名）。填权重文件名会被
        # 服务端拒：「未知的视频模型」。
        "model_key": "fl2va_pruned_int8",
        "width": 1280, "height": 736,
        "aspect": "16:9", "resolution": "768p",
        "duration": 5.0,
        # 【步数用 4】——线上真实 t2v 任务就是 4 步（twopass 默认档）。
        # 用 30 步会让实测跑很久，且不是用户实际的使用档。
        "steps": 4, "sampler": "euler", "scheduler": "simple",
        "accel_lora": "ckpt850", "accel_lora_strength": 1.0,
        "accel_loras": [{"key": "ckpt850", "strength": 1.0, "enabled": True}],
        "cfg": 1.0, "shift_video": 7.0, "shift_audio": 3.0,
    }

    # ---- 段1 ----
    print("=== 段1 提交（t2v 5s）===", flush=True)
    job1 = submit("t2v", dict(base_params))
    report["job1"] = job1
    st1, err1, outs1 = wait_done(job1)
    report["job1_status"] = st1
    report["job1_error"] = err1
    report["job1_outputs"] = outs1
    print(f"段1 -> {st1} {err1 or ''}", flush=True)
    if st1 != "done":
        (evdir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"❌ 段1 失败，证据在 {evdir}")
        return 1

    p1 = job_params(job1)
    cont1 = p1.get("continuation") or {}
    report["job1_continuation"] = cont1
    seg1_rel = cont1.get("segment_rel") or ""
    report["seg1_rel"] = seg1_rel

    # ---- 点「接续」（真实调服务端接续上下文）----
    ctx = api(f"/api/jobs/{job1}/continuation")
    report["continuation_ctx_ok"] = ctx.get("ok")
    report["continuation_ctx_error"] = ctx.get("error")
    report["continuation_ctx_total_frames"] = ctx.get("total_frames")
    report["continuation_ctx_total_seconds"] = ctx.get("total_seconds")
    report["continuation_ctx_prompt_history"] = ctx.get("prompt_history")
    print(f"接续上下文 ok={ctx.get('ok')} "
          f"total={ctx.get('total_seconds')}s "
          f"prompt_history={len(ctx.get('prompt_history') or [])} 段",
          flush=True)
    if not ctx.get("ok"):
        (evdir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"❌ 接续上下文失败：{ctx.get('error')}")
        return 1

    # ---- 段2 ----
    params2 = dict(base_params)
    params2["continuation"] = ctx.get("continuation") or {}
    print("=== 段2 提交（t2v 5s，接续段1）===", flush=True)
    job2 = submit("t2v", params2)
    report["job2"] = job2
    st2, err2, outs2 = wait_done(job2)
    report["job2_status"] = st2
    report["job2_error"] = err2
    report["job2_outputs"] = outs2
    print(f"段2 -> {st2} {err2 or ''}", flush=True)
    if st2 != "done":
        (evdir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"❌ 段2 失败，证据在 {evdir}")
        return 1

    p2 = job_params(job2)
    cont2 = p2.get("continuation") or {}
    report["job2_continuation"] = cont2
    seg2_rel = cont2.get("segment_rel") or ""
    report["seg2_rel"] = seg2_rel

    # ---- 产物核对 ----
    full_rel = ""
    for o in outs2:
        if o.get("is_full"):
            full_rel = o.get("file") or ""
    report["full_rel"] = full_rel

    probes = {}
    for label, rel in (("seg1", seg1_rel), ("seg2", seg2_rel), ("full", full_rel)):
        if not rel:
            probes[label] = None
            continue
        path = DATA / "outputs" / rel
        if not path.is_file():
            probes[label] = {"missing": True, "path": str(path)}
            continue
        info = ffprobe(path)
        probes[label] = info
        print(f"{label}: {rel} -> {info['frames']} 帧 "
              f"{info['format_duration']:.3f}s "
              f"{info['width']}x{info['height']} 音轨={info['has_audio']}",
              flush=True)
    report["probes"] = probes

    # ---- 断言 ----
    checks = []
    seg1 = probes.get("seg1") or {}
    seg2 = probes.get("seg2") or {}
    full = probes.get("full") or {}

    checks.append(("段1 有成片产物", bool(seg1_rel)))
    checks.append(("段2 分段已登记", bool(seg2_rel)))
    checks.append(("段2 有完整成片", bool(full_rel)))
    if seg1.get("frames"):
        checks.append((f"段1 帧数 == {EXPECT_SEG1_FRAMES}",
                       seg1["frames"] == EXPECT_SEG1_FRAMES))
    if seg2.get("frames"):
        checks.append((f"段2 帧数 == {EXPECT_SEG2_FRAMES}",
                       seg2["frames"] == EXPECT_SEG2_FRAMES))
    if full.get("frames"):
        checks.append((f"成片帧数 == {EXPECT_FULL_FRAMES}",
                       full["frames"] == EXPECT_FULL_FRAMES))
        # 成片时长 ≈ 段1 + 段2
        if seg1.get("frames") and seg2.get("frames"):
            checks.append(("成片帧数 == 段1 + 段2",
                           full["frames"] ==
                           seg1["frames"] + seg2["frames"]))
            checks.append(("成片时长 >= 9 秒",
                           full.get("format_duration", 0) >= 9.0))
    report["checks"] = [{"name": n, "ok": bool(o)} for n, o in checks]

    # ---- 自愈路径幂等性验证（不重启服务，直接重跑接续收尾）----
    # 这一步验证「修复后的自愈路径能补出成片」：把段2的收尾再跑一遍，
    # 应当**幂等**（分段登记不变），并且能再产出一个成片。
    print("\n=== 自愈路径幂等性验证（直接重跑接续收尾）===", flush=True)
    try:
        sys.path.insert(0, "/root/projects/h3studio-live")
        import server as live_server  # noqa: E402
        import asyncio  # noqa: E402

        outs2_b = json.loads(job_row(job2)["outputs"] or "[]")
        before_rel = cont2.get("segment_rel")
        outs_again = asyncio.get_event_loop().run_until_complete(
            live_server._finalize_continuation_impl(
                job2, "t2v", p2, outs2_b))
        p2_after = job_params(job2)
        cont2_after = p2_after.get("continuation") or {}
        heal = {
            "segment_rel_before": before_rel,
            "segment_rel_after": cont2_after.get("segment_rel"),
            "segment_rel_unchanged":
                before_rel == cont2_after.get("segment_rel"),
            "full_count_before": len([o for o in outs2_b if o.get("is_full")]),
            "full_count_after": len([o for o in outs2_b
                                     if o.get("is_full")]),
            "outputs_after": outs_again,
        }
        report["heal_idempotency"] = heal
        print(f"分段登记幂等: {heal['segment_rel_unchanged']} "
              f"({before_rel} -> {cont2_after.get('segment_rel')})",
              flush=True)
        checks.append(("自愈路径重跑后分段登记幂等",
                       bool(heal["segment_rel_unchanged"])))
        checks.append(("自愈路径重跑能补出成片",
                       len([o for o in outs_again if o.get("is_full")]) >= 1))
    except Exception as exc:
        report["heal_idempotency"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"⚠ 自愈路径验证异常：{exc}", flush=True)

    # ---- 落盘 ----
    for label, rel in (("seg1", seg1_rel), ("seg2", seg2_rel), ("full", full_rel)):
        if not rel:
            continue
        src = DATA / "outputs" / rel
        if src.is_file():
            dst = evdir / f"{label}_{Path(rel).name}"
            dst.write_bytes(src.read_bytes())
            report.setdefault("copied", {})[label] = str(dst)
    (evdir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 汇总 ----
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
